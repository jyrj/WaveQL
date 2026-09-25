"""Capture the debug collateral for a killed mutant.

Screening answers *whether* a mutant is detectable. Capture produces the evidence
an agent is later given: a waveform window centred on the divergence, the DUT
commit log, the golden stream, and the divergence tuple, all in one store.

Two things here are easy to get wrong and expensive to discover late.

**It must be a second run, not the screening run.** Cospike calls ``exit(rval)``
from inside its DPI callback the moment it sees a divergence
(``cospike.cc:67``), so the simulator dies at exactly the cycle of interest and
the VCD is truncated -- or never flushed. The capture pass therefore runs with
``+cospike-enable=0`` so the program runs to completion and the dump closes
cleanly. The divergence is already known from pass one; we are not re-detecting
it, we are photographing it.

**The window must be aimed at the right loop iteration.** The trigger is a PC,
and riscv-tests are loops: in one `rv64ui-p-add` run the PC ``0x80000048``
retires 58 times. Triggering on the PC alone captures the *first* pass, which is
almost never the failing one. The harness exposes ``+wf_n_<i>`` for exactly this,
and the occurrence count is recoverable from the commit log -- count how many
times that PC had already retired before the divergence. Getting this wrong
produces a window full of perfectly healthy execution, which looks like a working
capture.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from waveql.harness.chipyard import (
    ChipyardEnv,
    Commit,
    CosimOutcome,
    Divergence,
    WaveWindow,
    build_simulator,
    parse_commit_log,
    run_simulator,
)
from waveql.mutator.engine import MutationRecord, mutated
from waveql.mutator.operators import MutationSite

# The BOOM core scope. Everything WaveQL joins on lives under it.
BOOM_CORE = ("TOP.TestDriver.testHarness.chiptop0.system.tile_prci_domain"
             ".element_reset_domain_boom_tile.core")


@dataclass(frozen=True)
class Trigger:
    """Where to aim the capture window, and why."""

    pc: int
    occurrence: int          # the Nth retirement of that PC (>=1)
    cycles: int
    divergence_cycle: int | None
    rationale: str

    def window(self) -> WaveWindow:
        return WaveWindow(pc=self.pc, cycles=self.cycles, n=self.occurrence)


# BOOM asserts "Pipeline has hung" once idle_cycles reaches 2^13
# (v3/exu/core.scala:2059), i.e. 8,192 cycles after the machine stops retiring.
HANG_IDLE_CYCLES = 8192

# How far before the failure to start a HANG window, in retired instructions.
# A stall is only legible against the machine working: "this signal toggled every
# cycle and then stopped" is a fact, while "this signal is 0" is not. The default
# lead of 64 commits gave about 40 cycles of healthy execution before the stall,
# which is not enough to establish what normal looked like.
HANG_LEAD_COMMITS = 512


def plan_trigger(commits: Sequence[Commit], divergence: Divergence | None,
                 *, lead: int = 64, cycles: int = 512,
                 kill_kind: str | None = None) -> Trigger:
    """Choose the PC, occurrence and LENGTH a capture window should fire on.

    The window has to *begin before* the divergence, because the interesting
    state is what led to it. So the trigger is not the diverging instruction but
    one `lead` commits earlier, and the occurrence count for that PC is counted
    from the start of the run.

    THE LENGTH DEPENDS ON HOW THE MUTANT DIED, and getting this wrong silently
    hands the agent a dump that does not contain the failure. For a divergence,
    the interesting moment is the last commit, and a few hundred cycles around it
    suffice. For a HANG it is 8,192 cycles LATER: the machine stops retiring and
    BOOM's liveness assert fires only once idle_cycles saturates. We measured
    this on all eight assertion kills in the corpus -- every single window ended
    ~500 cycles after the last commit while the assert fired at +8,192, so the
    stall was outside the dump in 8 of 15 tasks and the agent was handed a
    recording of healthy execution.

    Extending the window is close to free, and for a pleasing reason: a stalled
    pipeline emits almost no value changes. Measured, 512 -> 10,000 cycles took
    the dump from 10.7 MB / 2.0 s to 48.0 MB / 2.8 s.
    """
    if not commits:
        raise ValueError("no DUT commits: nothing to aim a window at")
    if kill_kind == "assertion":
        lead = max(lead, HANG_LEAD_COMMITS)

    # Pass one ran under cosim and aborted at the divergence, so the commit log
    # ends there. The last commit is the closest thing to the failing one.
    end = len(commits) - 1
    anchor_idx = max(0, end - lead)
    anchor = commits[anchor_idx]

    # Which retirement of this PC is it? The harness counts from 1.
    occurrence = sum(1 for c in commits[: anchor_idx + 1] if c.pc == anchor.pc)

    div_cycle = commits[end].cycle
    span = cycles
    if kill_kind == "assertion":
        # Reach past the last commit, through the idle period, to the assert.
        lead_cycles = (div_cycle - (anchor.cycle or div_cycle)) if div_cycle else 0
        span = max(cycles, lead_cycles + HANG_IDLE_CYCLES + 1024)
    return Trigger(
        pc=anchor.pc,
        occurrence=occurrence,
        cycles=span,
        divergence_cycle=div_cycle,
        rationale=(
            f"anchor {lead} commits before the divergence: pc=0x{anchor.pc:x} "
            f"occurrence #{occurrence} (cycle {anchor.cycle}); divergence at "
            f"cycle {div_cycle}; capturing {span} cycles"
            + (" (extended to reach the hang assert)" if span != cycles else "")
        ),
    )


@dataclass
class CaptureResult:
    mutant_id: str
    ok: bool
    trigger: dict | None = None
    vcd_path: str | None = None
    vcd_bytes: int = 0
    out_path: str | None = None
    commits_captured: int = 0
    window_cycles: tuple[int, int] | None = None
    covered_divergence: bool = False
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


def capture_mutant(
    cy: ChipyardEnv,
    site: MutationSite,
    elf: Path,
    out_dir: Path,
    *,
    config: str = "WaveQLMediumBoomV3Config",
    jobs: int = 20,
    seed: int = 0,
    lead: int = 64,
    cycles: int = 512,
    rebuild: bool = True,
    run_timeout: int = 1800,
) -> CaptureResult:
    """Rebuild the mutant and photograph the failure."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    with mutated(cy.root, site, seed=seed) as rec:
        if rebuild:
            build = build_simulator(cy, config, jobs=jobs, debug=True,
                                    log_path=out_dir / "build.log")
            if not build.ok:
                return CaptureResult(rec.mutant_id, False,
                                     notes=[f"rebuild failed rc={build.returncode}"])
        sim = cy.simulator_path(config, debug=True)

        # Pass 1: cosim on, no waveform. Establish where it goes wrong.
        detect = run_simulator(cy, sim, elf, out_dir, stem="detect",
                               cosim=True, verbose=True, vcd=False, timeout=run_timeout)
        if not detect.cosim_active:
            return CaptureResult(rec.mutant_id, False, notes=detect.notes)
        if not detect.diverged:
            return CaptureResult(rec.mutant_id, False,
                                 notes=["no divergence on this stimulus; nothing to capture"])

        commits = parse_commit_log(detect.out_path)
        trig = plan_trigger(commits, detect.divergence, lead=lead, cycles=cycles)

        # Pass 2: cosim OFF so the run completes and the dump closes cleanly.
        cap = run_simulator(cy, sim, elf, out_dir, stem="capture",
                            cosim=False, verbose=True, vcd=True,
                            windows=[trig.window()], timeout=run_timeout)
        if cap.vcd_path is None:
            return CaptureResult(rec.mutant_id, False, trigger=asdict(trig),
                                 notes=["no VCD produced; did the trigger ever fire?"])

        # Did we actually catch the moment? A window that fired on the wrong
        # iteration produces a healthy-looking capture, so this is checked rather
        # than assumed.
        lo, hi, covered = _window_coverage(cap.vcd_path, trig.divergence_cycle)
        notes = []
        if trig.divergence_cycle is not None and not covered:
            notes.append(
                f"WINDOW MISSED THE DIVERGENCE: captured cycles {lo}..{hi} but the "
                f"divergence is at {trig.divergence_cycle}. Increase --cycles or "
                f"--lead, or the trigger fired on the wrong occurrence.")
        return CaptureResult(
            mutant_id=rec.mutant_id, ok=covered or trig.divergence_cycle is None,
            trigger=asdict(trig), vcd_path=str(cap.vcd_path),
            vcd_bytes=cap.vcd_path.stat().st_size, out_path=str(cap.out_path),
            commits_captured=cap.commits, window_cycles=(lo, hi),
            covered_divergence=covered, notes=notes,
        )


def _window_coverage(vcd: Path, divergence_cycle: int | None) -> tuple[int | None, int | None, bool]:
    """Cycle range the dump actually covers, from the DUT's own counter."""
    from waveql.ingest.wave import WaveReader

    try:
        r = WaveReader(vcd)
        idx = r.cycle_index(BOOM_CORE + ".clock", BOOM_CORE + ".debug_tsc_reg")
    except Exception:                                              # noqa: BLE001
        return None, None, False
    lo, hi = idx.first_cycle, idx.last_cycle
    if divergence_cycle is None or lo is None or hi is None:
        return lo, hi, False
    return lo, hi, lo <= divergence_cycle <= hi
