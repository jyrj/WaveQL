"""ChiselMutator — mutation of Chisel *Scala source*, not of generated Verilog.

Every published RTL mutation tool we could find operates on Verilog text. That is
the wrong level for a Chisel design: a Verilog mutation has no Chisel `file:line`
to blame, so it cannot be scored against a source-level localization metric, and
it can express defects that no Chisel author could have written.

This package mutates the source a human actually wrote, and records the exact
(file, line, column, before, after) tuple that constitutes ground truth.
"""

from waveql.mutator.scala_lex import Region, RegionKind, classify, operator_tokens
from waveql.mutator.operators import CLASSES, OPERATORS, MutationSite, enumerate_sites
from waveql.mutator.engine import (
    MutationError,
    MutationRecord,
    collect_sites,
    enclosing_module,
    mutated,
    sample_class_balanced,
)

__all__ = [
    "Region",
    "RegionKind",
    "classify",
    "operator_tokens",
    "CLASSES",
    "OPERATORS",
    "MutationSite",
    "enumerate_sites",
    "MutationError",
    "MutationRecord",
    "collect_sites",
    "enclosing_module",
    "mutated",
    "sample_class_balanced",
]
