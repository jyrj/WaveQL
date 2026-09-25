"""FixVerifyNode — rebuild the processor and decide whether it is actually fixed.

An agent's claim that it repaired something is not evidence. This node recreates
the defective design, applies the agent's patch, rebuilds, and re-runs the whole
stimulus ladder under Spike lockstep. A repair counts only if every stimulus the
UNMUTATED design passes also passes now, with the oracle confirmed running.

A repair that differs textually from reverting our mutation still counts: what is
measured is whether the processor works, not whether the agent guessed our diff.
The verdict records which it was.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from chia.base.ChiaFunction import ChiaFunction

from chia_waveql.state_def import MutantArtifact, RepairArtifact
from waveql.corpus.campaign import DEFAULT_STIMULI
from waveql.corpus.verify import verify_fix
from waveql.harness.chipyard import ChipyardEnv
from waveql.mutator.operators import enumerate_sites


class FixVerifyNode:
    logging_name = "FixVerifyNode"

    def __init__(self, config: str = "WaveQLMediumBoomV3Config",
                 logging_level: int = logging.INFO):
        self.config = config
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction(resources={"verilator_run": 1})
    def verify(self, chipyard_dir: str, mutant: MutantArtifact, proposal: dict | None,
               arm: str = "waveql", seed: int = 0, jobs: int = 11,
               stimuli: Sequence[str] = DEFAULT_STIMULI) -> RepairArtifact:
        cy = ChipyardEnv.load(chipyard_dir)
        src = (cy.root / mutant.path).read_text()
        site = next(s for s in enumerate_sites(mutant.path, src)
                    if s.line == mutant.line and s.operator == mutant.operator
                    and s.before == mutant.before)
        base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
        elfs = [base / s for s in stimuli]
        r = verify_fix(cy, site, proposal, elfs, task_id=mutant.mutant_id,
                       arm=arm, seed=seed, config=self.config, jobs=jobs)
        self.logger.info(f"{mutant.mutant_id} [{arm}]: {r.verdict} "
                         f"({r.stimuli_passed}/{r.stimuli_total})")
        return RepairArtifact(
            mutant_id=r.task_id, arm=r.arm, seed=r.seed, verdict=r.verdict,
            exact_revert=r.exact_revert, same_file_as_bug=r.same_file_as_bug,
            same_line_as_bug=r.same_line_as_bug, stimuli_passed=r.stimuli_passed,
            stimuli_total=r.stimuli_total, first_failure=r.first_failure,
            reason=r.reason, proposal=r.proposal,
            build_seconds=r.build_seconds, run_seconds=r.run_seconds)
