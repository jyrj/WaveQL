"""WaveQLIngestNode — a waveform, a commit log and a golden trace become a join.

This is the node the project is named after. CHIA's ``VerilatorRunNode`` already
captures PC-triggered waveform windows and ``CosimNode`` already computes the
first architectural divergence -- and then discards it into a text window. This
node keeps both, in one store, indexed by the same key.

The key is the cycle, and it is taken from the DUT's own counter
(``core.debug_tsc_reg``) rather than derived from a clock period. That is not
fastidiousness: we verified the join reproduces the commit log 235/235 exactly,
and 0/235 at either neighbouring cycle, which an arithmetic derivation did not.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Sequence

from chia.base.ChiaFunction import ChiaFunction

from chia_waveql.state_def import StoreArtifact
from waveql.harness.chipyard import parse_commit_log
from waveql.ingest.wave import WaveReader
from waveql.store.store import WaveQLStore
from waveql.tool.api import BOOM_CORE, BOOM_TILE, TILE_SCOPES

# Scopes worth materialising per task. A full-hierarchy MediumBoom window carries
# ~72k signals; these ~17k cover the commit point, rename, both issue queues,
# register read, the execution units and the memory system.
DEFAULT_SCOPES = (
    "core.rob", "core.rename_stage", "core.int_issue_unit", "core.mem_issue_unit",
    "core.iregister_read", "core.alu_exe_unit", "core.memExeUnit", "core.csr",
    "core.dispatcher", "lsu", "dcache",
    # The core's own signals: dispatch and decode stalls live there, and a hang
    # is usually visible in them. `core.*` is the scope itself, not its subtree.
    "core.*",
    # Fetch and redirect, without the branch predictor. A control-flow divergence
    # is the DUT executing from a PC Spike never reaches, and this is where that
    # is decided. `frontend.bpd` is 5,827 signals and holds no defect here.
    "frontend.*",
)


class WaveQLIngestNode:
    logging_name = "WaveQLIngestNode"

    def __init__(self, logging_level: int = logging.INFO):
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction()
    def ingest(self, mutant_id: str, vcd_path: str, commit_log_path: str,
               divergence: dict | None = None, assertion: str | None = None,
               assertion_src: str | None = None, divergence_cycle: int | None = None,
               scopes: Sequence[str] = DEFAULT_SCOPES,
               db_path: str | None = None, strict: bool = True,
               gen_src: str | None = None) -> StoreArtifact:
        """Build the joined store from one failing run's collateral.

        `strict` makes a scope that matches NO signal an error. pywellen answers
        an unmatched prefix with silence, so without this a typo produces a store
        that is empty and looks perfectly healthy -- the single worst failure mode
        available here, and one we shipped once: asking for `core.lsu` when the
        LSU is a TILE-level scope gave a ROB-only store, 2,371 signals instead of
        17,426.
        """
        t0 = time.monotonic()
        store = WaveQLStore(db_path)
        used: list[str] = []
        idx = None
        if vcd_path and Path(vcd_path).is_file():
            r = WaveReader(vcd_path)
            idx = r.cycle_index(BOOM_CORE + ".clock", BOOM_CORE + ".debug_tsc_reg")
            names: list[str] = []
            empty: list[str] = []
            for s in scopes:
                got = r.signals_under([f"{BOOM_TILE}.{s}"])
                (names.extend(got) if got else empty.append(s))
                if got:
                    used.append(s)
            if empty and strict:
                raise ValueError(
                    f"scopes matched no signal in {vcd_path}: {empty}. An unmatched "
                    "scope is silently empty, so this raises rather than producing a "
                    "store that looks healthy.")
            if names:
                store.put_signals(r.metadata(names))
                store.put_wave(r.changes(names, idx))
                if gen_src:
                    # firtool annotates every declaration in the generated
                    # Verilog, so a waveform path resolves to a Chisel file:line.
                    # Without it an agent can localize a wrong value to the cycle
                    # and the bit and still have nowhere to edit.
                    from waveql.ingest.srcmap import SourceMap
                    sm = SourceMap(Path(gen_src))
                    have = {x[0] for x in store.db.execute(
                        "SELECT full_path FROM signal").fetchall()}
                    rows = [(n_, ref.chisel_file, ref.line, sm.module_of(n_))
                            for n_ in names if n_ in have
                            for ref in sm.resolve(n_)]
                    store.put_signal_src(rows)

        if commit_log_path and Path(commit_log_path).is_file():
            store.put_commits(parse_commit_log(commit_log_path))
        if divergence:
            from waveql.harness.chipyard import _parse_divergence
            d = _parse_divergence(divergence.get("line", "")) if isinstance(divergence, dict) else None
            if d:
                store.put_divergence(d, cycle=divergence_cycle)
        if assertion:
            store.put_assertion(assertion, assertion_src, cycle=divergence_cycle)

        store.set_meta(mutant_id=mutant_id, vcd=vcd_path, gen_src=gen_src or "",
                       cycle_source=(idx.source if idx else "none"),
                       window_lo=(idx.first_cycle if idx else None),
                       window_hi=(idx.last_cycle if idx else None))
        n = lambda tbl: store.db.execute(f"SELECT count(*) FROM {tbl}").fetchone()[0]
        art = StoreArtifact(
            mutant_id=mutant_id, db_path=store.path, signals=n("signal"),
            changes=n("wave"), commits=n("commit_log"), divergences=n("divergence"),
            cycle_source=(idx.source if idx else "none"),
            window_lo=(idx.first_cycle if idx else None),
            window_hi=(idx.last_cycle if idx else None),
            scopes=used, ingest_seconds=time.monotonic() - t0)
        nsrc = n("signal_src")
        self.logger.info(f"{mutant_id}: {art.signals:,} signals, {art.changes:,} "
                         f"changes, {art.commits:,} commits, {nsrc:,} source refs "
                         f"in {art.ingest_seconds:.1f}s")
        self._store = store        # kept so a caller can query without re-ingesting
        return art

    @property
    def store(self) -> WaveQLStore:
        return self._store
