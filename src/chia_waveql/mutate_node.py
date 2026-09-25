"""ChiselMutateNode — inject a defect into Chisel *source*.

Every published RTL mutation tool we could find operates on generated Verilog.
That is the wrong level for a Chisel design: a Verilog mutation has no Chisel
`file:line` to blame, so it cannot be scored against a source-level localization
metric, and it can express defects no Chisel author could have written.

The node enumerates mutation sites, filters out Scala that is not a circuit
(`assert`, `require`, `printf`, and the TMA performance-counter subsystem -- all
of which are real code that an architectural oracle can never observe), and
samples class-balanced so per-class results are a property of the design rather
than of where the sampler happened to look.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Sequence

from chia.base.ChiaFunction import ChiaFunction

from chia_waveql.state_def import MutantArtifact
from waveql.corpus.targets import ARCH_TARGETS, filter_sites
from waveql.mutator.engine import collect_sites, default_resolver, mutated, sample_class_balanced
from waveql.mutator.operators import CLASSES


class ChiselMutateNode:
    """Select and materialise Chisel-source mutants."""

    logging_name = "ChiselMutateNode"

    def __init__(self, subtree: str = "generators/boom/src/main/scala/v3",
                 logging_level: int = logging.INFO):
        self.subtree = subtree
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @ChiaFunction()
    def select(self, chipyard_dir: str, per_class: int = 5, seed: int = 0,
               targets: Sequence[str] | None = None,
               classes: Sequence[str] = CLASSES) -> list[MutantArtifact]:
        """Choose a class-balanced sample of architecturally-reachable mutants.

        Balanced rather than proportional: raw site counts in BOOM are wildly
        skewed -- boolean-operator flips alone supply ~40% of all sites, the
        handshake family about 2% -- so a proportional sample would produce a
        corpus that is mostly `&&`/`||` and would have no power exactly where the
        interesting failures live.
        """
        root = Path(chipyard_dir).resolve()
        tgts = list(targets) if targets else [t for t, _ in ARCH_TARGETS]
        sites = filter_sites(collect_sites(root, tgts))
        chosen = sample_class_balanced(sites, per_class=per_class, seed=seed,
                                       classes=list(classes))
        self.logger.info(f"{len(sites):,} reachable sites -> {len(chosen)} selected")
        resolver = default_resolver(root, self.subtree)
        out = []
        for s in chosen:
            # mutated() writes, yields the ground-truth record, and restores the
            # checkout byte-exactly in a finally. Selection only needs the record.
            with mutated(root, s, seed=seed, resolver=resolver) as rec:
                out.append(MutantArtifact(
                    mutant_id=rec.mutant_id, path=rec.path, line=rec.line,
                    col=rec.col, operator=rec.operator,
                    mutation_class=rec.mutation_class, module=rec.module,
                    before=rec.before, after=rec.after, context=rec.context,
                    diff=rec.diff, sha256_before=rec.sha256_before,
                    sha256_after=rec.sha256_after))
        return out
