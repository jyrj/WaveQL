"""Tests for the agent-facing tool surface.

The tool surface is the independent variable of the ablation, so its failure
modes matter more than its features. Every test here pins a way the surface could
mislead an agent while appearing to work.
"""

from __future__ import annotations

import pyarrow as pa
import pytest

from waveql.store.store import WaveQLStore
from waveql.tool.api import BOOM_CORE, ToolError, WaveQLTool, declarations, dispatch


class Meta:
    def __init__(self, sid, path, width=1):
        self.signal_id, self.full_path, self.bitwidth = sid, path, width
        self.scope, _, self.name = path.rpartition(".")
        self.var_type = "Wire"


class Cmt:
    def __init__(self, cycle, pc):
        self.cycle, self.pc, self.priv, self.insn = cycle, pc, 3, 0x13
        self.rd = self.wdata = self.regfile = None


@pytest.fixture
def tool():
    st = WaveQLStore()
    rob = f"{BOOM_CORE}.rob"
    st.put_signals([Meta(1, f"{rob}.rob_state", 2), Meta(2, f"{rob}.rob_val_0"),
                    Meta(3, f"{rob}.rob_val_1"), Meta(4, f"{rob}.rob_head", 6)])
    st.put_wave(pa.table({
        "signal_id": pa.array([1, 2, 3, 4], pa.int64()),
        "signal_path": pa.array([None] * 4, pa.string()),
        "time_ps": pa.array([1000] * 4, pa.int64()),
        "cycle": pa.array([10, 10, 10, 10], pa.int64()),
        "value_u64": pa.array([1, 1, 0, 7], pa.uint64()),
        "value_str": pa.array([None] * 4, pa.string()),
    }))
    st.put_commits([Cmt(10, 0x80000000), Cmt(12, 0x80000004)])
    return WaveQLTool(store=st)


def test_core_relative_names_are_expanded(tool):
    r = tool.trace_signal("rob.rob_state", 10, 20)
    assert len(r) == 1


def test_absolute_names_still_work(tool):
    assert len(tool.trace_signal(f"{BOOM_CORE}.rob.rob_state", 10, 20)) == 1


def test_unknown_signal_raises_with_a_hint(tool):
    """pywellen answers an unknown path with zero changes and no error, so a typo
    would otherwise read as 'the signal was quiet'."""
    with pytest.raises(ToolError, match="find_signals"):
        tool.trace_signal("rob.rob_stat", 10, 20)


def test_near_miss_suggests_the_real_path(tool):
    with pytest.raises(ToolError, match="rob_state"):
        tool.trace_signal("lsu.rob_state", 10, 20)


def test_find_signals_makes_the_namespace_searchable(tool):
    r = tool.find_signals("rob_val_[0-9]$")
    assert len(r) == 2


def test_inflight_reports_only_valid_entries_plus_pointers(tool):
    rows = {row["name"]: row["value_u64"] for row in tool.inflight(10).to_dicts()}
    assert "rob_val_0" in rows and rows["rob_val_0"] == 1
    assert "rob_val_1" not in rows, "an invalid ROB entry is noise, not state"
    assert rows["rob_head"] == 7 and rows["rob_state"] == 1


def test_dispatch_returns_tool_errors_as_text(tool):
    """A tool error is information the agent should act on, not an episode end."""
    out = dispatch(tool, "trace_signal",
                   {"signal": "rob.nope", "cycle_lo": 0, "cycle_hi": 1})
    assert out.startswith("ERROR:") and "find_signals" in out


def test_dispatch_rejects_unknown_tools(tool):
    assert dispatch(tool, "rm_rf", {}).startswith("ERROR: no such tool")


def test_dispatch_reports_bad_arguments(tool):
    assert "bad arguments" in dispatch(tool, "commits", {"nope": 1})


def test_writes_are_refused_through_the_tool(tool):
    assert "ERROR" in dispatch(tool, "sql", {"query": "DROP TABLE wave"})


def test_window_rejects_an_inverted_range(tool):
    with pytest.raises(ToolError):
        tool.window(20, 10)


def test_every_declared_tool_is_dispatchable(tool):
    """A declared-but-unrouted tool is a model-visible dead end."""
    for d in declarations():
        assert not dispatch(tool, d["name"], {}).startswith("ERROR: no such tool")


def test_declarations_have_descriptions_and_types(tool):
    for d in declarations():
        assert d["description"] and d["parameters"]["type"] == "OBJECT"
        for prop in d["parameters"].get("properties", {}).values():
            assert "type" in prop


def test_every_call_is_logged(tool):
    before = tool.store.query_count
    tool.find_signals("rob")
    tool.commits(10)
    assert tool.store.query_count == before + 2


# --- cycle scoring in either base ------------------------------------------

def test_hex_token_from_the_log_is_scored_generously():
    """Cospike prints the divergence cycle in HEX. On four of fifteen tasks the
    text arm answered that token verbatim -- 161 for true cycle 353 -- having
    found the right event without converting the base. Both scorings are
    reported because the headline's size depends on which is used."""
    from waveql.analysis.metrics import cycle_hit_any_base, score_blame

    answer = "MODULE: Rob\nSIGNAL: x\nCYCLE: 161"
    assert cycle_hit_any_base(answer, 353) is True        # 0x161 == 353
    # ...and the primary decimal scorer still calls it a miss.
    assert score_blame(answer, true_module="Rob", true_context="x",
                       true_before="x", true_cycle=353).cycle is False


def test_decimal_answer_still_counts_in_either_base():
    from waveql.analysis.metrics import cycle_hit_any_base
    assert cycle_hit_any_base("CYCLE: 11113", 11113) is True


def test_a_wrong_cycle_fails_in_both_bases():
    from waveql.analysis.metrics import cycle_hit_any_base
    assert cycle_hit_any_base("CYCLE: 99999", 353) is False
    assert cycle_hit_any_base("CYCLE: UNKNOWN", 353) is False


def test_signal_paths_are_reported_core_relative_and_round_trip():
    """Every absolute path carries the same ~100-character prefix. On a 50-row
    discovery query that is 5KB of boilerplate, and context is what the agent
    runs out of -- one episode ended after 42 queries having never proposed a
    fix. The short form must be accepted back, or the saving costs correctness.
    """
    import pyarrow as pa

    from waveql.ingest.wave import SignalMeta
    from waveql.store.store import WaveQLStore
    from waveql.tool.api import BOOM_CORE, WaveQLTool

    st = WaveQLStore()
    full = f"{BOOM_CORE}.rob.rob_head"
    st.put_signals([SignalMeta(signal_id=1, full_path=full,
                               scope=f"{BOOM_CORE}.rob", name="rob_head",
                               bitwidth=5, var_type="Reg")])
    st.put_wave(pa.table({
        "signal_id": pa.array([1], pa.int64()), "time_ps": pa.array([10], pa.int64()),
        "cycle": pa.array([5], pa.int64()), "value_u64": pa.array([8], pa.uint64()),
        "value_str": pa.array([None], pa.string())}))
    tool = WaveQLTool(store=st)

    found = tool.find_signals("rob_head")
    assert found.columns[0] == "signal"
    assert found.rows[0][0] == "rob.rob_head"

    # The name it printed must be a name it accepts.
    assert tool.trace_signal(found.rows[0][0], 0, 10).rows
    assert tool.state_at(9).rows[0][0] == "rob.rob_head"
