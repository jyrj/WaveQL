"""Tests for the WaveQL store.

The theme is that this store's dangerous failures are all *quiet*: a query that
returns zero rows because the semantics are wrong looks exactly like a query that
returns zero rows because nothing happened, and a truncated answer looks exactly
like a complete one. Each test below pins one of those apart.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from waveql.store.store import WaveQLStore, DEFAULT_ROW_LIMIT


class Meta:
    def __init__(self, sid, path, width=1, vt="Wire"):
        self.signal_id = sid
        self.full_path = path
        self.scope, _, self.name = path.rpartition(".")
        self.bitwidth = width
        self.var_type = vt


class Cmt:
    def __init__(self, cycle, pc, priv=3, insn=0, rd=None, wdata=None, regfile=None):
        self.cycle, self.pc, self.priv, self.insn = cycle, pc, priv, insn
        self.rd, self.wdata, self.regfile = rd, wdata, regfile


def wave_table(rows):
    return pa.table({
        "signal_id": pa.array([r[0] for r in rows], pa.int64()),
        "signal_path": pa.array([None] * len(rows), pa.string()),
        "time_ps": pa.array([r[1] for r in rows], pa.int64()),
        "cycle": pa.array([r[2] for r in rows], pa.int64()),
        "value_u64": pa.array([r[3] for r in rows], pa.uint64()),
        "value_str": pa.array([None] * len(rows), pa.string()),
    })


@pytest.fixture
def store():
    s = WaveQLStore()
    s.put_signals([Meta(1, "top.core.rob.rob_state", 2),
                   Meta(2, "top.core.rob.com_idx", 7),
                   Meta(3, "top.core.lsu.busy", 1)])
    # rob_state changes once, at cycle 10, then holds for the rest of the window.
    s.put_wave(wave_table([
        (1, 1000, 10, 1),
        (2, 1000, 10, 5), (2, 3000, 12, 6), (2, 5000, 14, 7),
        (3, 1000, 10, 0), (3, 7000, 16, 1),
    ]))
    s.put_commits([Cmt(10, 0x80000000), Cmt(12, 0x80000004, rd=1, wdata=0x800000000014112D),
                   Cmt(14, 0x80000008), Cmt(16, 0x8000000C)])
    return s


# --- the asof semantics ------------------------------------------------------

def test_stable_signal_is_not_reported_as_absent(store):
    """rob_state last changed at cycle 10 and holds. Asking about 12..20 must
    return its value, not nothing. A range filter returns zero rows here, which
    reads as "no data" when the truth is "it held its value"."""
    r = store.trace_signal("top.core.rob.rob_state", 12, 20)
    assert len(r) == 1
    assert r.to_dicts()[0]["entering"] is True
    assert r.to_dicts()[0]["value_u64"] == 1


def test_state_at_returns_the_value_in_effect_not_only_changes(store):
    r = store.state_at(13, signals=["top.core.rob.rob_state", "top.core.rob.com_idx"])
    d = {row["full_path"]: row for row in r.to_dicts()}
    assert d["top.core.rob.rob_state"]["value_u64"] == 1
    assert d["top.core.rob.rob_state"]["last_change_cycle"] == 10
    # com_idx last changed at 12, not 14: asof must not look forward.
    assert d["top.core.rob.com_idx"]["value_u64"] == 6
    assert d["top.core.rob.com_idx"]["last_change_cycle"] == 12


def test_state_at_does_not_look_into_the_future(store):
    r = store.state_at(11, signals=["top.core.rob.com_idx"])
    assert r.to_dicts()[0]["value_u64"] == 5


def test_changed_only_narrows_to_that_cycle(store):
    assert len(store.state_at(12, scopes=["top.core.rob"], changed_only=True)) == 1
    assert len(store.state_at(13, scopes=["top.core.rob"], changed_only=True)) == 0


# --- cost bounds -------------------------------------------------------------

def test_truncation_is_detected_not_assumed(store):
    r = store.window(0, 100, limit=2)
    assert len(r) == 2 and r.truncated is True
    assert "TRUNCATED" in r.render()


def test_a_complete_answer_is_not_flagged_truncated(store):
    r = store.window(0, 100, limit=DEFAULT_ROW_LIMIT)
    assert r.truncated is False and "TRUNCATED" not in r.render()


def test_every_query_is_logged(store):
    before = store.query_count
    store.commits_near(12)
    store.state_at(12)
    assert store.query_count == before + 2
    ops = [r[0] for r in store.db.execute("SELECT op FROM query_log ORDER BY seq").fetchall()]
    assert ops[-2:] == ["commits_near", "state_at"]


# --- scope filtering ---------------------------------------------------------

def test_scope_filter_is_prefix_not_substring(store):
    r = store.window(0, 100, scopes=["top.core.rob"])
    paths = {row["full_path"] for row in r.to_dicts()}
    assert all(p.startswith("top.core.rob.") for p in paths)
    assert "top.core.lsu.busy" not in paths


# --- read-only enforcement ---------------------------------------------------

@pytest.mark.parametrize("bad", [
    "DELETE FROM wave",
    "INSERT INTO wave VALUES (1,1,1,1,NULL)",
    "DROP TABLE wave",
    "SELECT 1; DROP TABLE wave",
    "UPDATE commit_log SET pc = 0",
    "CREATE TABLE evil (x INT)",
])
def test_mutating_sql_is_refused(store, bad):
    """The store is evidence about a failing run. An agent that could write to it
    could manufacture the ground truth it is being scored against."""
    with pytest.raises(ValueError):
        store.sql(bad)


def test_a_column_named_like_a_keyword_is_still_allowed(store):
    """Whole-word matching, so `updated_at` is not mistaken for an UPDATE."""
    r = store.sql("SELECT 1 AS updated_at, 2 AS created_at")
    assert r.rows == [(1, 2)]


def test_select_and_with_are_allowed(store):
    assert store.sql("SELECT count(*) FROM commit_log").rows == [(4,)]
    assert store.sql("WITH x AS (SELECT 1 AS a) SELECT a FROM x").rows == [(1,)]


# --- unsigned 64-bit ---------------------------------------------------------

def test_full_width_register_writes_survive(store):
    """0x800000000014112D exceeds signed int64. BOOM really does write it (it is
    a misa/CSR read), so the column must be unsigned or the value is corrupted."""
    r = store.sql("SELECT wdata FROM commit_log WHERE rd = 1")
    assert r.rows[0][0] == 0x800000000014112D


def test_aliased_names_are_all_addressable():
    """A VCD aliases identical nets onto one id; every NAME must survive ingest.

    Keying the signal table on the waveform id dropped 44% of a real BOOM tile's
    names -- `rob_head` among them -- and a name an agent cannot look up is
    indistinguishable to it from a signal that never moved.
    """
    from waveql.ingest.wave import SignalMeta
    st = WaveQLStore()
    st.put_signals([
        SignalMeta(signal_id=7, full_path="top.core.rob_head", scope="top.core",
                   name="rob_head", bitwidth=6, var_type="wire"),
        SignalMeta(signal_id=7, full_path="top.core.rob_head_alias", scope="top.core",
                   name="rob_head_alias", bitwidth=6, var_type="wire"),
    ])
    paths = {r[0] for r in st.db.execute("SELECT full_path FROM signal").fetchall()}
    assert paths == {"top.core.rob_head", "top.core.rob_head_alias"}


def test_state_at_reports_every_aliased_name():
    """Both names share one waveform id, so both must report the same value --
    partitioning the asof by id instead of by path would report only one."""
    import pyarrow as pa

    from waveql.ingest.wave import SignalMeta
    st = WaveQLStore()
    st.put_signals([
        SignalMeta(signal_id=7, full_path="top.a", scope="top", name="a",
                   bitwidth=1, var_type="wire"),
        SignalMeta(signal_id=7, full_path="top.b", scope="top", name="b",
                   bitwidth=1, var_type="wire"),
    ])
    st.put_wave(pa.table({
        "signal_id": pa.array([7, 7], pa.int64()),
        "time_ps": pa.array([10, 20], pa.int64()),
        "cycle": pa.array([1, 2], pa.int64()),
        "value_u64": pa.array([0, 1], pa.uint64()),
        "value_str": pa.array([None, None], pa.string()),
    }))
    got = {r[0]: r[2] for r in st.state_at(5).rows}
    assert got == {"top.a": 1, "top.b": 1}
