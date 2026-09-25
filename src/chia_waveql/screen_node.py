"""DetectabilityScreenNode — does a machine actually observe the defect?

A mutation is not a benchmark task. Most source mutations are non-compiling,
semantically inert, or unreachable under the stimulus. Only a mutant that an
oracle observes going wrong may become a task, and there are TWO oracles here:

* **Spike lockstep** says "you committed the wrong instruction".
* **A fired Chisel assertion** says "you broke an invariant your designers wrote
  down". BOOM ships a liveness assert, so this is how a DEADLOCK is caught -- a
  hung design never commits anything wrong, so Spike has nothing to say.

Pooling them would hide the second: a hang screened by Spike alone looks like a
survivor. In BuggyBOOM more than half of all kills are assertion kills.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from chia.base.ChiaFunction import ChiaFunction

from chia_waveql.state_def import MutantArtifact, ScreenArtifact
from waveql.corpus.campaign import DEFAULT_STIMULI
from waveql.corpus.screen import screen_site
from waveql.harness.chipyard import ChipyardEnv
from waveql.mutator.operators import enumerate_sites


class DetectabilityScreenNode:
    logging_name = "DetectabilityScreenNode"

    def __init__(self, config: str = "WaveQLMediumBoomV3Config",
                 logging_level: int = logging.INFO):
        self.config = config
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction(resources={"verilator_run": 1})
    def screen(self, chipyard_dir: str, mutant: MutantArtifact,
               stimuli: Sequence[str] = DEFAULT_STIMULI, jobs: int = 11,
               seed: int = 0, capture: bool = True) -> ScreenArtifact:
        """Build the mutant, run every stimulus, and capture the evidence if it dies.

        Capture happens HERE, while the simulator for this mutant is still built.
        Doing it in a later pass would pay the ~180 s build again for every task
        in the corpus.
        """
        cy = ChipyardEnv.load(chipyard_dir)
        src = (cy.root / mutant.path).read_text()
        site = next(s for s in enumerate_sites(mutant.path, src)
                    if s.line == mutant.line and s.operator == mutant.operator
                    and s.before == mutant.before)
        base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
        elfs = [base / s for s in stimuli]
        r = screen_site(cy, site, elfs, config=self.config, jobs=jobs, seed=seed,
                        capture=capture)
        self.logger.info(f"{mutant.mutant_id}: {r.verdict} ({r.kill_kind})")
        return ScreenArtifact(
            mutant_id=r.mutant_id, verdict=r.verdict, kill_kind=r.kill_kind,
            killing_stimulus=r.killing_stimulus, divergence=r.first_divergence,
            assertion=r.assertion, assertion_src=r.assertion_src,
            divergence_cycle=r.divergence_cycle, vcd_path=r.vcd_path,
            vcd_bytes=r.vcd_bytes, window=r.window,
            window_covers_divergence=r.window_covers_divergence,
            build_seconds=r.build_seconds, run_seconds=r.run_seconds,
            stimuli_run=r.stimuli_run)
