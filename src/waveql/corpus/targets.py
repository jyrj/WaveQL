"""Which parts of BOOM a Spike lockstep oracle can actually see.

The screening oracle is *architectural*: Spike checks committed instructions.
That makes a large fraction of a real out-of-order core structurally
un-killable, and sampling uniformly across the source wastes most of a campaign
discovering it. The first three mutants of our first real campaign were all
correct survivals, all for this reason:

    parameters.scala:265  useLHist = localHistoryNSets > 1 && localHistoryLength > 1
                          branch-predictor CONFIG -- a wrong prediction is
                          recovered by the pipeline; it costs cycles, not
                          correctness.
    core.scala:1132       tma_ctr_retire_width_3 := ... (retire_count === 3.U)
                          a TMA performance counter. Pure telemetry.
    core.scala:1639       saturating_loads_counter := saturating_loads_counter + 1.U
                          a throttle heuristic for store-drain forward progress.
                          Changes WHEN the pipeline pauses, not what it commits.

Each cost ~7.3 minutes (140 s build + ~300 s for the full stimulus ladder, which
a survivor must run in full). Three in a row is 22 minutes to learn nothing.

This module does NOT claim to decide architectural visibility statically -- that
is undecidable in general, and the honest treatment is that the screen decides.
What it does is bias sampling toward the structures where a defect *can* reach
architectural state, and record the exclusions so the survival rate of the
remainder stays a reportable number rather than an artefact of where we happened
to look.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Sequence

# Directories whose defects are, by construction, invisible to an architectural
# oracle. A branch predictor that predicts badly is still correct.
PERF_ONLY_DIRS: tuple[tuple[str, str], ...] = (
    ("ifu/bpd/", "branch predictors: a misprediction is recovered by the pipeline, "
                 "so a BP defect costs cycles and never changes committed state"),
    ("common/BoomPerfCounterDevice", "performance-counter device: telemetry"),
)

# Identifiers that mark performance/telemetry logic living *inside* an otherwise
# architecturally load-bearing file (core.scala carries both).
PERF_ONLY_IDENTS: tuple[tuple[str, str], ...] = (
    # The whole TMA (top-down microarchitecture analysis) subsystem, not just the
    # counters. The chia_artifact branch adds ~200 lines of it to core.scala:
    # tma_ctr_*, tma_memory_stall, tma_in_recovery, tma_fetch_valid, tma_slots_*.
    # An earlier regex matched only `tma_ctr\w*` and let the rest through, which
    # is how two of the first three campaign survivors turned out to be counter
    # logic: real mutations, in real hardware, that an architectural oracle can
    # never observe.
    (r"\btma_\w*", "TMA top-down analysis counters and their control logic"),
    (r"\bperf_?count\w*", "performance counters"),
    (r"\bsaturating_loads_counter\b", "store-drain throttle heuristic"),
    (r"\bdebug_tsc_reg\b", "the cycle counter this project joins on: mutating it "
                           "corrupts the measuring instrument"),
    (r"\bbr_?mispredict\w*_ctr\b", "misprediction counters"),
    (r"\bfrontend_slots_this_cycle\b", "TMA slot accounting"),
    (r"\b\w*_perf_\w*", "performance-event plumbing"),
    (r"\bcsr\.io\.(?:counters|inst)\b", "HPM counter plumbing"),
)

# The structures where a defect reaches committed architectural state. Ordered
# roughly by how often repo-level agents are reported to fail on them.
ARCH_TARGETS: tuple[tuple[str, str], ...] = (
    ("generators/boom/src/main/scala/v3/lsu",        "loads, stores, atomics, MSHRs, TLB"),
    ("generators/boom/src/main/scala/v3/exu/rob.scala", "reorder buffer: the commit point itself"),
    ("generators/boom/src/main/scala/v3/exu/rename", "register renaming and the free list"),
    ("generators/boom/src/main/scala/v3/exu/issue-units", "issue and wakeup ordering"),
    ("generators/boom/src/main/scala/v3/exu/execution-units", "ALU/mul/div/FPU results"),
    ("generators/boom/src/main/scala/v3/exu/decode.scala", "instruction decode"),
    ("generators/boom/src/main/scala/v3/exu/dispatch.scala", "dispatch into the issue queues"),
    ("generators/boom/src/main/scala/v3/exu/register-read", "operand read and bypass"),
    ("generators/boom/src/main/scala/v3/exu/fp-pipeline.scala", "FP register file and pipeline"),
    ("generators/boom/src/main/scala/v3/exu/core.scala",  "the pipeline that wires them together"),
)

_PERF_RX = re.compile("|".join(p for p, _ in PERF_ONLY_IDENTS))


@dataclass(frozen=True)
class Exclusion:
    path: str
    line: int
    reason: str


def is_perf_only(path: str, context: str) -> str | None:
    """Reason this site cannot reach architectural state, or None."""
    for frag, why in PERF_ONLY_DIRS:
        if frag in path:
            return why
    m = _PERF_RX.search(context or "")
    if m:
        for pat, why in PERF_ONLY_IDENTS:
            if re.search(pat, context):
                return why
        return "performance/telemetry identifier"
    return None


def filter_sites(sites: Sequence, *, record: list[Exclusion] | None = None) -> list:
    """Drop sites an architectural oracle structurally cannot kill."""
    kept = []
    for s in sites:
        why = is_perf_only(s.path, s.context)
        if why is None:
            kept.append(s)
        elif record is not None:
            record.append(Exclusion(s.path, s.line, why))
    return kept
