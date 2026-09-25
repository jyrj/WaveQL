"""The detectability screen: does a mutant actually break anything?

A mutation is not a benchmark task. Most source mutations are one of:

* **non-compiling** -- caught free by elaboration, before any simulation;
* **semantically inert** -- it changes a performance counter, a debug printf, or
  a branch predictor hint, so the architectural result is identical;
* **unreachable under this stimulus** -- real, but the test never exercises it.

Only a mutant that a *machine* observes going wrong may become a task. The
oracle here is Spike lockstep co-simulation: the mutated design and the golden
ISA model execute the same program, and the run aborts at the first architectural
disagreement. That disagreement, with the cycle it happened at, is the ground
truth an agent is later scored against.

Everything else about the screen exists to stop it lying:

* the mutation is reverted whether or not anything raised, so mutant N+1 is not
  built on top of mutant N;
* a build that fails is recorded as ``build-failed``, not silently skipped -- the
  ratio of non-compiling mutants is a property of the operator set and belongs in
  the paper;
* a run whose cosimulation never actually started is ``invalid``, never
  ``survived``. This distinction is the whole reason the screen is trustworthy:
  a simulator built without SpikeCosim exits 0 on every mutant, and a screen that
  could not tell the difference would report a corpus of zero detectable bugs
  while looking perfectly healthy.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Iterable, Sequence

from waveql.corpus.capture import _window_coverage, plan_trigger
from waveql.harness.chipyard import (
    ChipyardEnv,
    CosimOutcome,
    Divergence,
    assert_cosim_present,
    build_simulator,
    parse_commit_log,
    run_simulator,
)
from waveql.mutator.engine import MutationRecord, default_resolver, mutated
from waveql.mutator.operators import MutationSite

VERDICTS = ("killed", "survived", "build-failed", "invalid")

# How a mutant was caught. Recorded because the three are different bugs with
# different fix criteria, and pooling them would hide the most interesting class.
#   divergence -- Spike lockstep saw a wrong committed instruction.
#   assertion  -- a Chisel assert fired. BOOM ships a liveness assert
#                 (core.scala:2059, "Pipeline has hung"), so this is how a
#                 DEADLOCK is caught: the design never commits anything wrong
#                 because it stops committing at all, and Spike therefore never
#                 sees a divergence; without this kind it screens as a survivor.
#   crash      -- the simulator died without either oracle speaking.
KILL_KINDS = ("divergence", "assertion", "timeout", "crash")


@dataclass
class ScreenResult:
    """One mutant's fate. Everything a task manifest needs, or a rejection reason."""

    mutant_id: str
    verdict: str
    record: MutationRecord | None
    build_seconds: float
    build_returncode: int
    killing_stimulus: str | None = None
    kill_kind: str | None = None
    vcd_path: str | None = None
    vcd_bytes: int = 0
    window: dict | None = None
    window_covers_divergence: bool = False
    assertion: str | None = None
    assertion_src: str | None = None
    first_divergence: dict | None = None
    divergence_cycle: int | None = None
    commits_before_divergence: int | None = None
    stimuli_run: list[str] = field(default_factory=list)
    run_seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


def screen_site(
    cy: ChipyardEnv,
    site: MutationSite,
    stimuli: Sequence[Path],
    *,
    config: str = "WaveQLMediumBoomV3Config",
    jobs: int = 20,
    seed: int = 0,
    work_dir: Path | None = None,
    build_timeout: int = 7200,
    run_timeout: int = 900,
    run_workers: int = 8,
    capture: bool = True,
    capture_cycles: int = 512,
    capture_lead: int = 64,
) -> ScreenResult:
    """Mutate, build, co-simulate, and classify. Always reverts the checkout."""
    root = cy.root
    work_dir = Path(work_dir or (root.parents[1] / "var" / "screen"))

    # Resolve module names against the whole BOOM subtree: inheritance crosses
    # files, so a per-file view attributes ALUUnit (and everything else that
    # inherits its module-ness) to nothing at all.
    subtree = site.path.split("/scala/")[0] + "/scala/v3" if "/scala/v3/" in site.path else ""
    resolver = default_resolver(root, subtree) if subtree else None
    with mutated(root, site, seed=seed, resolver=resolver) as rec:
        run_dir = work_dir / rec.mutant_id
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "mutation.json").write_text(rec.to_json())
        (run_dir / "mutation.diff").write_text(rec.diff)

        build = build_simulator(
            cy, config, jobs=jobs, debug=True,
            log_path=run_dir / "build.log", timeout=build_timeout,
        )
        if not build.ok:
            # Not a failure of the screen: a mutant that cannot elaborate is
            # filtered for free, and the rate at which that happens characterises
            # the operator set.
            return ScreenResult(
                mutant_id=rec.mutant_id, verdict="build-failed", record=rec,
                build_seconds=build.seconds, build_returncode=build.returncode,
                notes=[f"build log: {build.log_path}"],
            )

        # Run the whole ladder CONCURRENTLY. Each Verilator simulator is a
        # single-threaded process, so the ladder's wall-clock collapses from the
        # SUM of its stimuli (~300 s) to the SLOWEST one (~50 s).
        #
        # This trades away stop-on-first-kill, and the trade is worth making: a
        # killed mutant gets slightly slower (it waits for the slowest stimulus
        # instead of stopping at the first divergence) while a SURVIVOR -- which
        # must run every stimulus by definition, and which is the common case in
        # this corpus -- gets about six times faster.
        #
        # The killer is chosen by LADDER ORDER, never by completion order, so the
        # recorded result is identical however the threads interleave. The
        # killing stimulus is part of the task manifest, so reproducibility here
        # is not negotiable.
        t0 = time.monotonic()
        with ThreadPoolExecutor(max_workers=min(len(stimuli), run_workers)) as pool:
            futures = {
                pool.submit(
                    run_simulator, cy, build.binary, elf, run_dir,
                    stem=f"cosim-{Path(elf).name}", cosim=True, verbose=True,
                    vcd=False, timeout=run_timeout,
                ): i
                for i, elf in enumerate(stimuli)
            }
            done: dict[int, CosimOutcome] = {}
            for fut in as_completed(futures):
                done[futures[fut]] = fut.result()
        outcomes: list[CosimOutcome] = [done[i] for i in sorted(done)]

        for out in outcomes:
            # A run without the cosim banner is "invalid" -- the oracle may be
            # missing -- UNLESS the other oracle already spoke. A mutant can trip
            # a Chisel assertion ("Pipeline has hung", "Leaking physical
            # registers") before Spike prints its banner; that run is a design
            # fault, not a harness fault.
            if not out.cosim_active and not out.assertion:
                return ScreenResult(
                    mutant_id=rec.mutant_id, verdict="invalid", record=rec,
                    build_seconds=build.seconds, build_returncode=build.returncode,
                    stimuli_run=[Path(o.elf).name for o in outcomes],
                    run_seconds=time.monotonic() - t0,
                    notes=out.notes + ["screen aborted: the oracle was not running"],
                )
        def _killed(elf, out, kind: str) -> ScreenResult:
            # Capture the waveform NOW, while the simulator for this mutant is
            # still built. Doing it in a later pass would mean paying the ~200 s
            # build again for every task in the corpus; here it costs one extra
            # simulation.
            #
            # It must be a SECOND run regardless: cospike calls exit() from
            # inside its DPI callback the moment it diverges (cospike.cc:67), so
            # the VCD of the detecting run is truncated at exactly the cycle of
            # interest. The capture run therefore disables cosim and lets the
            # program finish, which closes the dump cleanly.
            vcd_path = vcd_bytes = None
            win = None
            covers = False
            if capture:
                try:
                    commits = parse_commit_log(out.out_path)
                    trig = plan_trigger(commits, out.divergence,
                                        lead=capture_lead, cycles=capture_cycles,
                                        kill_kind=kind)
                    cap = run_simulator(
                        cy, build.binary, elf, run_dir, stem="capture",
                        cosim=False, verbose=True, vcd=True,
                        windows=[trig.window()], timeout=run_timeout)
                    if cap.vcd_path is not None:
                        vcd_path = str(cap.vcd_path)
                        vcd_bytes = cap.vcd_path.stat().st_size
                        lo, hi, covers = _window_coverage(
                            cap.vcd_path, trig.divergence_cycle)
                        win = {**asdict(trig), "cycle_lo": lo, "cycle_hi": hi}
                except Exception as exc:                           # noqa: BLE001
                    win = {"error": f"{type(exc).__name__}: {exc}"}
            return ScreenResult(
                mutant_id=rec.mutant_id, verdict="killed", record=rec,
                build_seconds=build.seconds, build_returncode=build.returncode,
                killing_stimulus=Path(elf).name, kill_kind=kind,
                assertion=out.assertion, assertion_src=out.assertion_src,
                vcd_path=vcd_path, vcd_bytes=vcd_bytes or 0,
                window=win, window_covers_divergence=covers,
                first_divergence=asdict(out.divergence) if out.divergence else None,
                divergence_cycle=_divergence_cycle(out),
                commits_before_divergence=out.commits,
                stimuli_run=[Path(o.elf).name for o in outcomes],
                run_seconds=time.monotonic() - t0,
            )

        # A mutant so broken that the simulator dies before cosim even announces
        # itself is KILLED, not "invalid". `invalid` must mean "the oracle was
        # missing from the binary", which is a harness fault; a run that also
        # FAILED is a design fault and belongs in the corpus.
        if all(not o.cosim_active for o in outcomes) and all(not o.passed for o in outcomes):
            for elf, out in zip(stimuli, outcomes):
                if not out.passed:
                    # "crash" means neither oracle spoke. If an assertion fired
                    # before Spike's banner, one did: it is an assertion kill.
                    return _killed(elf, out, "assertion" if out.assertion else "crash")
        # Divergence first: it is the strongest and most localizable signal, and
        # when a mutant both diverges and trips an assertion the divergence is
        # the more useful ground truth. Within each kind, LADDER ORDER decides,
        # never completion order.
        for elf, out in zip(stimuli, outcomes):
            if out.diverged:
                return _killed(elf, out, "divergence")
        for elf, out in zip(stimuli, outcomes):
            if out.assertion:
                return _killed(elf, out, "assertion")
        for elf, out in zip(stimuli, outcomes):
            if out.timed_out:
                # Ran past the cycle budget without finishing. A livelock: the
                # design keeps making progress but never completes, so neither
                # Spike nor the hang assert ever fires.
                return _killed(elf, out, "timeout")
        for elf, out in zip(stimuli, outcomes):
            if not out.passed:
                # Neither oracle spoke, yet the run did not succeed on a stimulus
                # the UNMUTATED design passes. The mutant broke something; we just
                # cannot say what, so it is recorded as a kill of unknown kind
                # rather than quietly counted as a survivor.
                return _killed(elf, out, "crash")

        return ScreenResult(
            mutant_id=rec.mutant_id, verdict="survived", record=rec,
            build_seconds=build.seconds, build_returncode=build.returncode,
            stimuli_run=[Path(o.elf).name for o in outcomes],
            run_seconds=time.monotonic() - t0,
            notes=["no declared stimulus made the DUT disagree with Spike"],
        )


def _divergence_cycle(out: CosimOutcome) -> int | None:
    """The cycle of the last committed instruction before the abort.

    Cospike aborts *at* the diverging instruction, so the final line of the DUT
    commit log is the closest cycle-accurate coordinate we have for it. This is
    the value a capture window is later centred on.
    """
    from waveql.harness.chipyard import parse_commit_log

    try:
        rows = parse_commit_log(out.out_path)
    except OSError:
        return None
    for r in reversed(rows):
        if r.cycle is not None:
            return r.cycle
    return None


def default_stimuli(cy: ChipyardEnv, names: Iterable[str]) -> list[Path]:
    """Resolve riscv-test names to built ELFs, failing loudly on a missing one.

    A missing stimulus must never degrade into "this mutant survived": that is
    the same false-negative the ``invalid`` verdict exists to prevent.
    """
    base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
    out = []
    for n in names:
        p = base / n
        if not p.is_file():
            raise FileNotFoundError(f"stimulus {n} not built at {p}")
        out.append(p)
    return out
