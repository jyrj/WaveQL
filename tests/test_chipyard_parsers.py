"""Tests for the log parsers that turn simulator output into the join.

Every case here is a real shape observed in real output from a real MediumBoom
run, not an invented one. Three of them are regressions for bugs that silently
*lost data* rather than raising -- the failure mode that matters most in this
project, because a parser that returns fewer rows still looks like it worked.
"""

from __future__ import annotations

import textwrap

import pytest

from waveql.harness.chipyard import (
    Divergence,
    WaveWindow,
    _parse_divergence,
    parse_commit_log,
)


# Verbatim from var/smoke/cycled.out (MediumBoom, WithBoomHumanReadableCommitLog,
# stderr piped through spike-dasm).
REAL_LOG = (
    "C                  27: 3 0x0000000000010000 (0x00000517) auipc   a0, 0x0 x10 0x0000000000010000\n"
    "C                  39: 3 0x0000000000010008 (0x30551073) csrw    mtvec, a0\n"
    "C                  51: 3 0x000000000001000c (0x301022f3) csrr    t0, misa x 5 0x800000000014112d\n"
    "Cosim: Configuring spike cosim\n"
    "Cosim: isa string: rv64imafdczicsr_zifencei_zihpm_zicntr\n"
    "C               49200: 0 0x0000000080000048 (0xfc0f2023) sd      t3, -64(t5)\n"
)


def test_parses_every_commit_line(tmp_path):
    p = tmp_path / "run.out"
    p.write_text(REAL_LOG)
    rows = parse_commit_log(p)
    assert len(rows) == 4, "a Cosim: banner line was mistaken for a commit, or a commit was dropped"


def test_cycle_prefix_is_space_padded(tmp_path):
    """Chisel's printf("C%d: ") pads %d to the signal width: "C     27: ".

    A regex written as `C(\\d+):` matches nothing here. Because the group is
    optional, it does not raise -- it reports every commit as cycle-less, and the
    whole PC-to-cycle join silently disappears.
    """
    p = tmp_path / "run.out"
    p.write_text(REAL_LOG)
    rows = parse_commit_log(p)
    assert [r.cycle for r in rows] == [27, 39, 51, 49200]


def test_instructions_without_writeback_are_not_dropped(tmp_path):
    """Stores, branches and fences write no register and end in disassembly.

    Anchoring the write-back group to end-of-line dropped all of them: 317 of 832
    rows on a single rv64ui-p-add run, and precisely the traffic an LSU bug lives
    in.
    """
    p = tmp_path / "run.out"
    p.write_text(REAL_LOG)
    rows = parse_commit_log(p)
    stores = [r for r in rows if r.rd is None]
    assert len(stores) == 2                       # the csrw and the sd
    assert stores[-1].pc == 0x80000048
    assert stores[-1].cycle == 49200


def test_writeback_is_not_confused_by_the_disassembly(tmp_path):
    """`auipc a0, 0x0 x10 0x...` -- the real write-back is the LAST such group."""
    p = tmp_path / "run.out"
    p.write_text(REAL_LOG)
    rows = parse_commit_log(p)
    assert rows[0].rd == 10 and rows[0].wdata == 0x10000
    assert rows[2].rd == 5 and rows[2].wdata == 0x800000000014112D


def test_privilege_level_is_not_a_cycle(tmp_path):
    """The plain mixin's first field is priv, and only ever takes 0/1/3.

    This is the bug that motivated switching config: treating it as a cycle
    produced a "monotonic" join key whose entire range was {0, 3}.
    """
    p = tmp_path / "run.out"
    p.write_text("3 0x0000000000010000 (0x00000517) x10 0x0000000000010000\n")
    rows = parse_commit_log(p)
    assert rows[0].priv == 3
    assert rows[0].cycle is None, "no cycle exists without the human-readable mixin"


# --- divergence --------------------------------------------------------------

def test_pc_mismatch_carries_spike_and_dut():
    d = _parse_divergence("Cosim: 1c PC mismatch spike 10000 != DUT 0\n")
    assert d is not None and d.kind == "pc"
    assert d.spike == "0x10000" and d.dut == "0x0"


def test_pc_mismatch_cycle_radix_is_reported_both_ways():
    """cospike prints this cycle with %PRIx64 (hex) at cospike_impl.cc:678, but
    prints the same variable with %PRIu64 elsewhere and %lld at :786. The two
    wdata messages are textually identical in different bases, so the parser must
    not pretend to know which."""
    d = _parse_divergence("Cosim: 1c PC mismatch spike 10000 != DUT 0\n")
    assert d.cycle_token == "1c"
    assert d.cycle_hex == 28
    assert d.cycle_dec is None            # "1c" is not decimal, and we do not guess


def test_wdata_mismatch():
    d = _parse_divergence("Cosim: ff wdata mismatch reg 5 deadbeef != cafef00d\n")
    assert d.kind == "wdata" and d.reg == 5
    assert d.spike == "0xdeadbeef" and d.dut == "0xcafef00d"


def test_no_divergence_returns_none():
    assert _parse_divergence("Cosim: Configuring spike cosim\n*** PASSED ***\n") is None


# --- wave windows ------------------------------------------------------------

def test_window_plusargs_use_bare_hex():
    """The PC slot is read by a raw plusarg_reader with FORMAT="%h"; a 0x prefix
    parses as 0, which disables the slot rather than erroring."""
    assert WaveWindow(pc=0x80000050, cycles=2000).plusargs(0) == [
        "+wf_pc_0=80000050", "+wf_n_0=1", "+wf_cyc_0=2000",
    ]


@pytest.mark.parametrize("kw", [{"pc": 0, "cycles": 10}, {"pc": 1, "cycles": 0}, {"pc": 1, "cycles": 1, "n": 0}])
def test_silently_disabling_windows_are_rejected(kw):
    """`enabled = pcs(i) =/= 0.U && cyc(i) =/= 0.U` -- these produce no waveform
    and no error, so the harness refuses them up front."""
    with pytest.raises(ValueError):
        WaveWindow(**kw)


# --- assertion / hang detection ---------------------------------------------

def test_fired_assertion_is_detected_with_its_source():
    """BOOM's liveness assert is a SECOND oracle, and the one that catches a
    deadlock. A hung mutant never commits anything wrong, so Spike never reports
    a divergence -- which is exactly how two of the first ten screened mutants
    were recorded as SURVIVORS."""
    from waveql.harness.chipyard import _ASSERTION

    log = (
        "[17249000] %Error: BoomCore.sv:2116: Assertion failed in "
        "TOP.TestDriver.testHarness.chiptop0.system.core: "
        "Assertion failed: Pipeline has hung.\n"
        '    at core.scala:2059 assert (!(idle_cycles.value(13)), "Pipeline has hung.")\n'
        "%Error: BoomCore.sv:2116: Verilog $stop\nAborting...\n"
    )
    m = _ASSERTION.search(log)
    assert m is not None
    assert "Pipeline has hung" in m.group("msg")
    assert m.group("src") == "core.scala:2059"


def test_clean_run_has_no_assertion():
    from waveql.harness.chipyard import _ASSERTION

    assert _ASSERTION.search("[UART] UART0 is here\n*** PASSED ***\n") is None
