"""Verify a proposed repair by rebuilding the processor and re-running it.

An agent's claim that it fixed something is not evidence. This module recreates
the defective design, applies the agent's patch on top of it, rebuilds, and runs
the whole stimulus ladder under Spike lockstep. A repair counts only if every
stimulus the UNMUTATED design passes also passes now, with the co-simulation
oracle confirmed running.

Three false-positive routes are closed explicitly, because each one would let a
benchmark report repairs that never happened:

**Disabling the oracle.** An assertion kill could be "fixed" by deleting the
assertion. That is rejected outright (§ ORACLE_TAMPERING), and it would fail
anyway: with the assert gone a hung design simply runs to the cycle bound and
reports `(timeout)`, which is still not a pass.

**A patch that lands somewhere else.** The patch contract requires its anchor to
be unique in the file (see waveql.agent.source), so an edit either applies where
the agent meant or is refused.

**Passing a weaker test than the baseline did.** The ladder here is the same
twelve stimuli the clean design was verified against, not a subset, and
`cosim_active` is required on every one.

A repair that differs textually from reverting the mutation is still a repair:
the verdict records whether it was an exact revert or a functionally different
fix, and both count. What is being measured is whether the processor works, not
whether the agent guessed our diff.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Sequence

from waveql.harness.chipyard import ChipyardEnv, build_simulator, run_simulator
from waveql.mutator.engine import held_edit, mutated
from waveql.mutator.operators import MutationSite

VERDICTS = ("fixed", "not-fixed", "build-failed", "no-proposal", "rejected")

# Edits that would repair the ORACLE rather than the design. Pre-registered:
# fixed before any fix-rate run, so a rule cannot be added after seeing which
# way it cuts.
ORACLE_TAMPERING = (
    (re.compile(r"\bassert\s*\("), "removes or weakens a Chisel assertion"),
    (re.compile(r"\brequire\s*\("), "removes an elaboration-time contract"),
    (re.compile(r"\bprintf\s*\("), "edits the commit-log printf the harness reads"),
)


@dataclass
class FixResult:
    task_id: str
    arm: str
    seed: int
    verdict: str
    exact_revert: bool = False
    patched_path: str | None = None
    patched_line: int | None = None
    same_file_as_bug: bool = False
    same_line_as_bug: bool = False
    build_seconds: float = 0.0
    run_seconds: float = 0.0
    stimuli_passed: int = 0
    stimuli_total: int = 0
    first_failure: str | None = None
    reason: str | None = None
    proposal: dict | None = None
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True, default=str)


def _tampering(old: str, new: str) -> str | None:
    """Reject a patch that repairs the oracle instead of the design."""
    for rx, why in ORACLE_TAMPERING:
        before, after = len(rx.findall(old)), len(rx.findall(new))
        if after < before:
            return why
    return None


def verify_fix(
    cy: ChipyardEnv,
    site: MutationSite,
    proposal: dict | None,
    stimuli: Sequence[Path],
    *,
    task_id: str,
    arm: str,
    seed: int = 0,
    config: str = "WaveQLMediumBoomV3Config",
    jobs: int = 11,
    work_dir: Path | None = None,
    run_workers: int = 12,
    build_timeout: int = 7200,
    run_timeout: int = 900,
) -> FixResult:
    """Recreate the defect, apply the patch, rebuild, and re-run everything."""
    from concurrent.futures import ThreadPoolExecutor, as_completed

    res = FixResult(task_id=task_id, arm=arm, seed=seed, verdict="no-proposal",
                    stimuli_total=len(stimuli), proposal=proposal)
    if not proposal:
        res.reason = "the agent never called propose_fix"
        return res

    why = _tampering(proposal["old"], proposal["new"])
    if why:
        res.verdict, res.reason = "rejected", why
        return res

    work = Path(work_dir or (cy.root.parents[1] / "var" / "fix")) / f"{task_id}-{arm}-s{seed}"
    work.mkdir(parents=True, exist_ok=True)

    with mutated(cy.root, site, seed=seed):
        return verify_applied(cy, site, proposal, stimuli, res=res, work=work,
                              config=config, jobs=jobs, run_workers=run_workers,
                              build_timeout=build_timeout, run_timeout=run_timeout)


def verify_applied(
    cy: ChipyardEnv,
    site: MutationSite,
    proposal: dict,
    stimuli: Sequence[Path],
    *,
    res: "FixResult",
    work: Path,
    config: str = "WaveQLMediumBoomV3Config",
    jobs: int = 11,
    run_workers: int = 12,
    build_timeout: int = 7200,
    run_timeout: int = 900,
) -> "FixResult":
    """Verify a patch against a checkout that is ALREADY mutated.

    Split out because the repair episode has to hold the mutation for its whole
    life -- the agent reads the defective source -- and re-entering mutated()
    from inside that context would block on its own exclusive lock.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # verify_applied can be called directly (the repair runner holds the mutation
    # itself), so it cannot rely on the wrapper having made this.
    work.mkdir(parents=True, exist_ok=True)
    if True:
        target = (cy.root / proposal["path"]).resolve()
        if not target.is_file():
            res.verdict, res.reason = "rejected", f"no such file: {proposal['path']}"
            return res
        before = target.read_text()
        # The anchor is re-checked against the MUTATED tree, which is the tree the
        # agent read. A patch that no longer applies here means the agent quoted
        # something that was never in front of it.
        if before.count(proposal["old"]) != 1:
            res.verdict = "rejected"
            res.reason = (f"anchor occurs {before.count(proposal['old'])} times in the "
                          "mutated source; it must be unique")
            return res

        patched = before.replace(proposal["old"], proposal["new"], 1)
        res.patched_path = proposal["path"]
        res.patched_line = before[: before.index(proposal["old"])].count("\n") + 1
        res.same_file_as_bug = proposal["path"] == site.path
        res.same_line_as_bug = (res.same_file_as_bug
                                and abs(res.patched_line - site.line) <= 2)
        # Did the patch put the original file back, byte for byte?
        #
        # Compared against the whole file, not by substring. The substring form
        # -- `site.before in patched and site.after not in patched` -- cannot ever
        # be true when the mutated text is a SUBSTRING of the original, which is
        # exactly what dropping a wrapper produces:
        #     before  RegNext(RegNext(csr.io.evec))
        #     after          (RegNext(csr.io.evec))
        # It is only meaningful for an edit in the SAME file as the defect.
        original = before[: site.start] + site.before + before[site.start + len(site.after):]
        res.exact_revert = res.same_file_as_bug and patched == original

        with held_edit(target, before):
            try:
                target.write_text(patched)
                (work / "proposal.json").write_text(json.dumps(proposal, indent=2))

                build = build_simulator(cy, config, jobs=jobs, debug=True,
                                        log_path=work / "build.log", timeout=build_timeout)
                res.build_seconds = build.seconds
                if not build.ok:
                    res.verdict = "build-failed"
                    res.reason = f"the patched design did not elaborate (rc={build.returncode})"
                    return res

                t0 = time.monotonic()
                with ThreadPoolExecutor(max_workers=min(len(stimuli), run_workers)) as pool:
                    futs = {pool.submit(run_simulator, cy, build.binary, e, work,
                                        stem=f"fix-{e.name}", cosim=True, verbose=True,
                                        vcd=False, timeout=run_timeout): i
                            for i, e in enumerate(stimuli)}
                    done = {}
                    for f in as_completed(futs):
                        done[futs[f]] = f.result()
                outs = [done[i] for i in sorted(done)]
                res.run_seconds = time.monotonic() - t0
                res.stimuli_passed = sum(1 for o in outs if o.passed and o.cosim_active)

                for elf, o in zip(stimuli, outs):
                    if not o.cosim_active:
                        res.verdict = "not-fixed"
                        res.first_failure = Path(elf).name
                        res.reason = "cosim did not run: the verdict cannot be trusted"
                        return res
                    if not o.passed:
                        res.verdict = "not-fixed"
                        res.first_failure = Path(elf).name
                        res.reason = (o.divergence.line if o.divergence else
                                      o.assertion or "failed without a stated reason")
                        return res
                res.verdict = "fixed"
                return res
            finally:
                target.write_text(before)
