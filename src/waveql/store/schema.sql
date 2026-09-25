-- WaveQL store: waveform, DUT commit log, golden ISA trace and source map,
-- in one place, joinable on cycle.
--
-- The design decision that matters: `cycle` is a first-class column on the
-- waveform table, not something derived at query time. It is written once at
-- ingest from the DUT's own counter (core.debug_tsc_reg), which we verified
-- reproduces the commit log exactly (235/235, and 0/235 at either neighbouring
-- cycle). Deriving it per query from a timescale and a clock period would be
-- both slower and less trustworthy: the derivation has to be right for the join
-- to mean anything, so it is done once, checked once, and stored.

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- One row per traced signal NAME actually ingested (not per signal in the VCD).
--
-- The key is the path, not the waveform id. A VCD aliases identical nets onto
-- one id -- in a BOOM tile dump, 47,656 names share 26,567 ids -- so keying on
-- the id drops 44% of the names, including ones an agent will certainly ask for
-- by name (`rob_head` was one). A dropped name is exactly the failure this tool
-- surface exists to prevent: the lookup returns nothing and reads as "the signal
-- was quiet". Joins to `wave` still go through signal_id, which correctly
-- reports one row per real name.
CREATE TABLE IF NOT EXISTS signal (
    signal_id  BIGINT NOT NULL,
    full_path  TEXT PRIMARY KEY,
    scope      TEXT NOT NULL,
    name       TEXT NOT NULL,
    bitwidth   INTEGER NOT NULL,
    var_type   TEXT
);
CREATE INDEX IF NOT EXISTS idx_signal_sid ON signal (signal_id);
CREATE INDEX IF NOT EXISTS idx_signal_scope ON signal (scope);

-- Value CHANGES, not per-cycle samples. A per-cycle materialisation of a BOOM
-- window would be ~72k signals x thousands of cycles; the change list is what
-- the simulator actually emitted and is two orders of magnitude smaller.
-- `cycle` is NULL for changes that precede the first clock edge in the dump.
CREATE TABLE IF NOT EXISTS wave (
    signal_id  BIGINT NOT NULL,
    time_ps    BIGINT NOT NULL,
    cycle      BIGINT,
    value_u64  UBIGINT,   -- unsigned: a 64-bit register write of 0x8000... is not negative
    value_str  TEXT
);
CREATE INDEX IF NOT EXISTS idx_wave_sig_cycle ON wave (signal_id, cycle);
CREATE INDEX IF NOT EXISTS idx_wave_cycle ON wave (cycle);

-- What the DUT says it retired. `cycle` comes from debug_tsc_reg and is the
-- join key to `wave`.
CREATE TABLE IF NOT EXISTS commit_log (
    seq      BIGINT,
    cycle    BIGINT,
    priv     INTEGER,
    pc       UBIGINT NOT NULL,
    insn     UBIGINT,
    rd       INTEGER,
    wdata    UBIGINT,
    regfile  TEXT
);
CREATE INDEX IF NOT EXISTS idx_commit_cycle ON commit_log (cycle);
CREATE INDEX IF NOT EXISTS idx_commit_pc ON commit_log (pc);

-- What the golden ISA model says should have happened. Has no cycle: an ISS has
-- no notion of one. It is ordered by sequence number, and the bridge to the
-- microarchitecture is the DUT commit log, which has both.
CREATE TABLE IF NOT EXISTS spike_log (
    seq    BIGINT PRIMARY KEY,
    priv   INTEGER,
    pc     UBIGINT NOT NULL,
    insn   UBIGINT,
    rd     INTEGER,
    wdata  UBIGINT,
    regfile TEXT
);
CREATE INDEX IF NOT EXISTS idx_spike_pc ON spike_log (pc);

-- Where the two disagreed. `tolerated` marks a divergence class that is a known
-- DUT/Spike semantic difference rather than a bug; the list is fixed before the
-- headline run (PREREGISTRATION.md 4.1) and applied identically to every arm.
CREATE TABLE IF NOT EXISTS divergence (
    kind         TEXT NOT NULL,      -- pc | wdata | unknown
    cycle        BIGINT,             -- authoritative, from the commit log
    cycle_token  TEXT,               -- cospike's own field, radix-ambiguous
    pc           UBIGINT,
    spike        TEXT,
    dut          TEXT,
    reg          INTEGER,
    tolerated    BOOLEAN DEFAULT FALSE,
    raw          TEXT
);

-- signal -> Chisel source. Populated from firtool's emitted locators.
CREATE TABLE IF NOT EXISTS signal_src (
    full_path    TEXT,
    chisel_file  TEXT,
    chisel_line  INTEGER,
    module       TEXT
);
CREATE INDEX IF NOT EXISTS idx_src_signal ON signal_src (full_path);

-- Every tool call an agent makes. "Queries per fix" is a headline metric, so the
-- log is part of the store rather than an optional side-channel.
CREATE TABLE IF NOT EXISTS query_log (
    seq        BIGINT,
    op         TEXT NOT NULL,
    args_json  TEXT,
    rows_out   INTEGER,
    truncated  BOOLEAN,
    ms         DOUBLE
);
