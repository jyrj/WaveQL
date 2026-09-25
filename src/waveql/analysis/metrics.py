"""Scoring an agent's blame against ground truth.

Every threshold here is fixed before any run is scored: the author of the tool
is also the author of the benchmark, and a tolerance chosen after seeing the data
is not a measurement.

Localization is scored at three granularities and reported separately, never
averaged into one number: an agent that names the right module and the wrong
signal has done something real, and collapsing that into a single score would
hide it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# FIXED IN ADVANCE. A cycle is "correct" if it lands within this many cycles of
# the first observable misbehaviour. 50 is chosen because BOOM's pipeline is
# ~10 stages deep and a defect's effect typically surfaces within a few tens of
# cycles of its cause; a tolerance much tighter would score pipeline depth rather
# than localization, and much looser would accept "somewhere in this program".
CYCLE_TOLERANCE = 50

_FIELD = re.compile(r"^\s*(MODULE|SIGNAL|CYCLE)\s*:\s*(.+?)\s*$", re.M | re.I)
_IDENT = re.compile(r"[A-Za-z_$][\w$]*")
# Chisel keywords and universal names carry no localizing information: an answer
# of "io" or "val" must not score a hit on a line that happens to contain them.
_SIGNAL_STOPWORDS = frozenset({
    "val", "var", "def", "when", "elsewhen", "otherwise", "Mux", "RegNext",
    "RegInit", "Reg", "Wire", "io", "the", "is", "in", "of", "U", "B", "W",
})


@dataclass(frozen=True)
class Blame:
    """What the agent claimed."""

    module: str | None = None
    signal: str | None = None
    cycle: int | None = None

    @staticmethod
    def parse(answer: str) -> "Blame":
        got: dict[str, str] = {}
        for m in _FIELD.finditer(answer or ""):
            got.setdefault(m.group(1).upper(), m.group(2).strip())
        cyc = None
        raw = got.get("CYCLE", "")
        if raw and raw.upper() != "UNKNOWN":
            m = re.search(r"\d+", raw.replace(",", ""))
            if m:
                cyc = int(m.group(0))
        def clean(v: str | None) -> str | None:
            if not v or v.strip().upper() in ("UNKNOWN", "N/A", "NONE", ""):
                return None
            return v.strip()
        return Blame(clean(got.get("MODULE")), clean(got.get("SIGNAL")), cyc)


@dataclass(frozen=True)
class Score:
    module: bool
    signal: bool
    cycle: bool
    answered: bool

    @property
    def any_hit(self) -> bool:
        return self.module or self.signal or self.cycle


def score_blame(answer: str, *, true_module: str | None, true_context: str,
                true_before: str, true_cycle: int | None,
                tolerance: int = CYCLE_TOLERANCE) -> Score:
    """Mechanical scoring. The agent never grades itself.

    * module -- the named module matches the enclosing Chisel module of the
      mutation. Matched case-insensitively as a whole word, so "Rob" scores
      against "Rob" but not against "RobIo" or a sentence that merely mentions it.
    * signal -- the named signal appears in the mutated source line. This is
      deliberately generous: the mutation may be to any sub-expression, and an
      agent that names any identifier of the defective expression has localized
      to a signal.
    * cycle -- within `tolerance` of the first observable misbehaviour.
    """
    b = Blame.parse(answer)
    answered = any((b.module, b.signal, b.cycle is not None))

    module_hit = False
    if b.module and true_module:
        module_hit = re.search(rf"(?<![\w$]){re.escape(true_module)}(?![\w$])",
                               b.module, re.I) is not None

    signal_hit = False
    if b.signal:
        # Identifiers of the mutated expression, then of the whole source line.
        # Union, not "mutated expression only". The mutation may be to any
        # sub-expression of the line, and an agent that names the signal being
        # ASSIGNED there -- `com_idx` in
        #   val com_idx = Mux(rob_state === s_rollback, rob_tail, rob_head)
        # -- has localized to the right signal even though `com_idx` does not
        # appear in the swapped arms. Scoring that as a miss would measure
        # phrasing rather than localization.
        core = set(_IDENT.findall(true_before or ""))
        line = set(_IDENT.findall(true_context or ""))
        claimed = set(_IDENT.findall(b.signal))
        target = (core | line) - _SIGNAL_STOPWORDS
        signal_hit = bool(claimed & target) if target else False

    cycle_hit = (b.cycle is not None and true_cycle is not None
                 and abs(b.cycle - true_cycle) <= tolerance)
    return Score(module_hit, signal_hit, cycle_hit, answered)


_CYCLE_FIELD = re.compile(r"^\s*CYCLE\s*:\s*([0-9a-fA-F]+)", re.M | re.I)


def cycle_hit_any_base(answer: str, true_cycle: int | None,
                       tolerance: int = CYCLE_TOLERANCE) -> bool:
    """Cycle scored generously: right event in EITHER base counts.

    Cospike prints the divergence cycle in HEXADECIMAL, and prints the same
    variable in decimal in its other messages, so an agent can quote the hex token
    verbatim (161 for cycle 353) having found the right event. The primary scorer
    reads decimal and counts that as a miss.

    That scorer is not wrong: an answer of 161 for cycle 353 is wrong by ~1,500
    cycles. But a headline whose size depends on base conversion should not be
    sold as a result about debugging, so BOTH scorings are computed and reported,
    and the paper leads with this, the more generous one.
    """
    if true_cycle is None:
        return False
    m = _CYCLE_FIELD.search(answer or "")
    if not m:
        return False
    tok = m.group(1)
    for base in (10, 16):
        try:
            if abs(int(tok, base) - true_cycle) <= tolerance:
                return True
        except ValueError:
            continue
    return False


def bootstrap_ci(values: list[float], reps: int = 10000, alpha: float = 0.05,
                 seed: int = 0) -> tuple[float, float]:
    """Percentile bootstrap CI, the only inferential statistic reported."""
    import random

    if not values:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(reps):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * reps)]
    hi = means[min(reps - 1, int((1 - alpha / 2) * reps))]
    return (lo, hi)
