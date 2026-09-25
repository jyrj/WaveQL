"""Mutation operators over Chisel source.

Each operator answers "where could a competent engineer plausibly have made this
mistake?", not "what character can I flip?". That distinction is the whole
argument for source-level mutation: a defect that no author could have written
teaches an agent nothing, and inflates a benchmark with unrealistic tasks.

Every operator is required to:

* fire only in live code (the :mod:`waveql.mutator.scala_lex` mask);
* rewrite a *complete syntactic unit*, never a fragment -- a rewrite that only
  half-applies produces a mutant that does not elaborate, which costs a full
  build to discover;
* carry a ``mutation_class`` label chosen before generation, so per-class
  results cannot be re-cut after the fact;
* be exactly reversible, because ground truth is the diff.

Operators are deliberately *not* weighted here. Selection and weighting live in
the engine, so the taxonomy stays a description of the design space rather than
a description of one sampling run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Sequence

from waveql.mutator.scala_lex import (
    code_mask,
    find_call,
    line_col,
    match_paren,
    operator_tokens,
    split_args,
)

# The fixed taxonomy. Labels are chosen to line up with the bug families that
# repo-level hardware-repair work reports agents actually failing on, so that a
# per-class table is comparable rather than idiosyncratic.
CLASSES = (
    "comparison-inversion",    # the condition is right, the sense is wrong
    "boundary-off-by-one",     # < vs <=: the classic full/empty queue defect
    "condition-logic",         # && vs ||: an over- or under-constrained guard
    "select-inversion",        # the mux picks the wrong side
    "handshake-protocol",      # ready/valid contract violated
    "pipeline-depth",          # a register removed: a timing/ordering bug
    "index-arithmetic",        # pointer/index off by one
    "constant-flip",           # a hardcoded enable/disable inverted
)


# Scala that is NOT a circuit. A mutation inside one of these can never produce
# a detectable hardware defect, and screening one still costs a full build plus a
# run of every stimulus -- about eight minutes each.
#
# Found by running the corpus generator: the very first mutant it selected was
#
#     require(x.dispatchWidth <= coreWidth && x.dispatchWidth > 0)   ->  >= 0
#
# an ELABORATION-TIME contract in parameters.scala. It survived all eight stimuli,
# correctly, after eight minutes of compute, because it does not describe hardware
# at all.
#
# Each entry is excluded for its own reason:
#   require/assume  Scala contracts, checked during elaboration, not emitted.
#   println/print   elaboration-time output.
#   printf          a Chisel simulation print. Inert architecturally -- AND the
#                   DUT commit log this project joins on is a printf, so mutating
#                   one corrupts our own measuring instrument.
#   assert/cover    Chisel simulation assertions. A mutated assertion fires
#                   spuriously and aborts the run, which SCREENS AS KILLED while
#                   the "bug" is unfindable in the design: a corpus task nobody
#                   can solve, and worse than a useless one.
#   dontTouch       an annotation, not logic.
NON_HARDWARE = ("require", "assume", "println", "print", "printf",
                "assert", "cover", "dontTouch")


def non_hardware_spans(src: str, mask: bytearray) -> list[tuple[int, int]]:
    """Byte ranges of `NON_HARDWARE` call arguments, for exclusion."""
    spans: list[tuple[int, int]] = []
    for name in NON_HARDWARE:
        for _ns, lp, rp in find_call(src, name, mask):
            spans.append((lp, rp))
    return spans


def _enclosing_construct(offset: int, spans: Sequence[tuple[int, int]],
                         names: Sequence[str]) -> str | None:
    for (lo, hi), nm in zip(spans, names):
        if lo <= offset < hi:
            return nm
    return None


@dataclass(frozen=True)
class MutationSite:
    """One applicable mutation. Applying it is a pure text splice."""

    path: str
    start: int              # byte offset, inclusive
    end: int                # byte offset, exclusive
    before: str
    after: str
    operator: str
    mutation_class: str
    line: int
    col: int
    context: str = ""       # the source line, for the ground-truth record
    in_construct: str | None = None   # enclosing non-hardware call, if any

    @property
    def is_hardware(self) -> bool:
        """False when this site cannot change the circuit (see NON_HARDWARE)."""
        return self.in_construct is None

    def apply(self, src: str) -> str:
        assert src[self.start:self.end] == self.before, "site does not match source"
        return src[: self.start] + self.after + src[self.end :]

    @property
    def site_id(self) -> str:
        return f"{self.path}:{self.line}:{self.col}:{self.operator}"


def _site(path: str, src: str, start: int, end: int, after: str, operator: str, cls: str) -> MutationSite:
    line, col = line_col(src, start)
    bol = src.rfind("\n", 0, start) + 1
    eol = src.find("\n", start)
    eol = len(src) if eol == -1 else eol
    return MutationSite(
        path=path, start=start, end=end, before=src[start:end], after=after,
        operator=operator, mutation_class=cls, line=line, col=col,
        context=src[bol:eol].strip(),
    )


# --- operator-token rewrites -------------------------------------------------
# These match a COMPLETE maximal-munch operator token, which is what keeps `<`
# from firing inside `<<`, `<>`, `<-` or `<=`, and `==` from firing inside
# Chisel's `===`.

_TOKEN_SWAPS: dict[str, tuple[str, str, str]] = {
    # token : (replacement, operator name, class)
    "===": ("=/=", "eq_to_ne", "comparison-inversion"),
    "=/=": ("===", "ne_to_eq", "comparison-inversion"),
    "<":   ("<=", "lt_to_le", "boundary-off-by-one"),
    "<=":  ("<",  "le_to_lt", "boundary-off-by-one"),
    ">":   (">=", "gt_to_ge", "boundary-off-by-one"),
    ">=":  (">",  "ge_to_gt", "boundary-off-by-one"),
    "&&":  ("||", "and_to_or", "condition-logic"),
    "||":  ("&&", "or_to_and", "condition-logic"),
}


def op_token_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    out = []
    for t in operator_tokens(src, mask):
        swap = _TOKEN_SWAPS.get(t.text)
        if swap is None:
            continue
        after, name, cls = swap
        out.append(_site(path, src, t.start, t.end, after, name, cls))
    return out


# --- structural rewrites -----------------------------------------------------

def mux_arm_swap_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``Mux(c, a, b)`` -> ``Mux(c, b, a)``: the select picks the wrong side.

    Only 3-argument Mux is touched. ``Mux1H`` / ``MuxCase`` / ``MuxLookup`` have
    different arities and are excluded by the whole-word match in ``find_call``.
    """
    out = []
    for _name_start, lp, rp in find_call(src, "Mux", mask):
        args = split_args(src, lp, rp, mask)
        if len(args) != 3:
            continue
        (_cs, _ce), (as_, ae), (bs, be) = args
        a, b = src[as_:ae], src[bs:be]
        # Splice the two arms, preserving each arm's own leading whitespace so a
        # multi-line Mux keeps its indentation and the diff stays readable.
        after = _swap_preserving_pad(a, b)
        if after is None:
            continue
        out.append(_site(path, src, as_, be, after, "mux_arm_swap", "select-inversion"))
    return out


def _swap_preserving_pad(a: str, b: str) -> str | None:
    """Swap two Mux arms, keeping each slot's original leading whitespace.

    Chisel muxes are routinely written across several lines and aligned by hand.
    Swapping the raw spans would drag one arm's indentation onto the other and
    produce a diff whose noise swamps the one-token semantic change -- which
    matters because that diff IS the ground truth a human reviews.
    """
    la, ra = _split_pad(a)
    lb, rb = _split_pad(b)
    if not ra.strip() or not rb.strip():
        return None
    if ra.strip() == rb.strip():
        return None                     # swapping identical arms is a no-op mutant
    return f"{la}{rb.strip()},{lb}{ra.strip()}"


def _split_pad(s: str) -> tuple[str, str]:
    """Split leading whitespace from the rest, so indentation can be reattached."""
    stripped = s.lstrip()
    return s[: len(s) - len(stripped)], stripped


def when_negate_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``when (c)`` -> ``when (!(c))``: the guard fires on exactly the wrong states.

    The condition is wrapped rather than textually negated, because Chisel
    conditions are frequently compound (``a && b``) and ``!a && b`` is a
    *different* mutation with different semantics -- one we would not be able to
    describe correctly in the ground-truth record.
    """
    out = []
    for name in ("when", "elsewhen"):
        for _ns, lp, rp in find_call(src, name, mask):
            inner = src[lp + 1 : rp]
            if not inner.strip():
                continue
            out.append(
                _site(path, src, lp + 1, rp, f"!({inner})", f"{name}_negate", "comparison-inversion")
            )
    return out


def regnext_drop_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``RegNext(x)`` -> ``(x)``: a pipeline stage disappears.

    Only the single-argument form. ``RegNext(x, init)`` also carries a reset
    value, and dropping the call there would leave ``(x, init)``, which is not a
    Chisel expression -- a mutant that fails to elaborate, i.e. a wasted build.
    """
    out = []
    for ns, lp, rp in find_call(src, "RegNext", mask):
        if len(split_args(src, lp, rp, mask)) != 1:
            continue
        out.append(_site(path, src, ns, rp + 1, src[lp : rp + 1], "regnext_drop", "pipeline-depth"))
    return out


_IDENT = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_$")


def _path_start(src: str, dot_ready: int, mask: bytearray) -> int | None:
    """Walk left from the ``.`` of ``.ready`` to the start of its selector path.

    A regex cannot do this job. Real BOOM backpressure reads like

        metaReadArb.io.in(4).ready && dataReadArb.io.in(2).ready

    where the path contains parenthesised indices, and those indices can nest
    (``io.req(w).ready``, ``arb.io.in(idx(j)).ready``). Walking the structure
    backwards -- balanced group, identifier, dot, repeat -- gets the whole path;
    a character-class regex silently stops at the first ``(`` and yields a
    fragment, which is how an earlier version of this operator found 9 sites in
    a file set that actually contains dozens.
    """
    i = dot_ready
    while True:
        # a balanced (...) index immediately left of the dot
        if i > 0 and src[i - 1] == ")" and mask[i - 1]:
            depth = 0
            j = i - 1
            while j >= 0:
                if mask[j]:
                    if src[j] == ")":
                        depth += 1
                    elif src[j] == "(":
                        depth -= 1
                        if depth == 0:
                            break
                j -= 1
            if j < 0:
                return None
            i = j
        # the identifier itself
        j = i
        while j > 0 and mask[j - 1] and src[j - 1] in _IDENT:
            j -= 1
        if j == i:
            return None                      # no identifier: not a selector path
        i = j
        # another selector level?
        if i > 0 and src[i - 1] == "." and mask[i - 1]:
            i -= 1
            continue
        return i


def _ready_occurrences(src: str, mask: bytearray) -> list[tuple[int, int]]:
    """``(path_start, path_end)`` for every live-code selector path ending ``.ready``."""
    out = []
    start = 0
    while True:
        k = src.find(".ready", start)
        if k == -1:
            return out
        start = k + 1
        if not mask[k]:
            continue
        end = k + len(".ready")
        if end < len(src) and src[end] in _IDENT:
            continue                          # .readyN, .ready_foo: a different signal
        ps = _path_start(src, k, mask)
        if ps is not None:
            out.append((ps, end))


def _prev_tok(src: str, i: int, mask: bytearray) -> tuple[str, int, int] | None:
    j = i
    while j > 0 and (src[j - 1].isspace() or not mask[j - 1]):
        j -= 1
    if j == 0:
        return None
    from waveql.mutator.scala_lex import OP_CHARS
    if src[j - 1] not in OP_CHARS:
        return None
    k = j
    while k > 0 and mask[k - 1] and src[k - 1] in OP_CHARS:
        k -= 1
    return src[k:j], k, j


def _next_tok(src: str, i: int, mask: bytearray) -> tuple[str, int, int] | None:
    j = i
    while j < len(src) and (src[j].isspace() or not mask[j]):
        j += 1
    if j >= len(src):
        return None
    from waveql.mutator.scala_lex import OP_CHARS
    if src[j] not in OP_CHARS:
        return None
    k = j
    while k < len(src) and mask[k] and src[k] in OP_CHARS:
        k += 1
    return src[j:k], j, k


def handshake_drop_ready_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``a.ready && b.ready`` -> ``a.ready``: one consumer's backpressure is ignored.

    This is the defect family that repo-level agents are reported to fail on most
    often, and it is invisible in a single-cycle view: the design only misbehaves
    once the dropped consumer actually stalls, which is exactly why diagnosing it
    needs a *window* of waveform rather than one signal value.

    Both operand positions are handled -- dropping ``&& rhs`` and dropping
    ``lhs &&`` are different defects in different modules, and BOOM writes more of
    the second than the first.
    """
    out = []
    for ps, pe in _ready_occurrences(src, mask):
        prev = _prev_tok(src, ps, mask)
        if prev and prev[0] == "&&":
            # `... && <path>.ready`  ->  drop the operator and this operand
            out.append(_site(path, src, prev[1], pe, "", "handshake_drop_ready_rhs", "handshake-protocol"))
            continue
        nxt = _next_tok(src, pe, mask)
        if nxt and nxt[0] == "&&":
            # `<path>.ready && ...`  ->  drop this operand and the operator
            out.append(_site(path, src, ps, nxt[2], "", "handshake_drop_ready_lhs", "handshake-protocol"))
    return out


def fire_to_valid_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``x.fire`` -> ``x.valid``: backpressure dropped from a transaction guard.

    Chisel's ``.fire`` is exactly ``valid && ready``. Replacing it with ``.valid``
    makes the design act on a transaction the consumer never accepted -- a
    duplicate enqueue, a lost dequeue, a pointer that advances while the data
    stands still. It is a one-token edit that a tired engineer genuinely makes,
    and it is the single most realistic handshake defect available in this source.
    """
    out = []
    start = 0
    while True:
        k = src.find(".fire", start)
        if k == -1:
            return out
        start = k + 1
        if not mask[k]:
            continue
        end = k + len(".fire")
        if end < len(src) and src[end] in _IDENT:
            continue                          # .fired, .fire_count: not the method
        # `.fire()` was the older spelling; consume the empty parens if present.
        e2 = end
        if src.startswith("()", e2):
            e2 += 2
        out.append(_site(path, src, k, e2, ".valid", "fire_to_valid", "handshake-protocol"))


_INC = re.compile(r"\+\s*1\.U")
_DEC = re.compile(r"-\s*1\.U")


def index_offby_one_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``+ 1.U`` <-> ``- 1.U``: a queue pointer or index walks the wrong way."""
    out = []
    for rx, after, name in ((_INC, "- 1.U", "inc_to_dec"), (_DEC, "+ 1.U", "dec_to_inc")):
        for m in rx.finditer(src):
            if not mask[m.start()]:
                continue
            out.append(_site(path, src, m.start(), m.end(), after, name, "index-arithmetic"))
    return out


_TRUE_B = re.compile(r"\btrue\.B\b")
_FALSE_B = re.compile(r"\bfalse\.B\b")


def bool_const_flip_sites(path: str, src: str, mask: bytearray) -> list[MutationSite]:
    """``true.B`` <-> ``false.B``: a hardcoded enable inverted."""
    out = []
    for rx, after, name in ((_TRUE_B, "false.B", "true_to_false"), (_FALSE_B, "true.B", "false_to_true")):
        for m in rx.finditer(src):
            if not mask[m.start()]:
                continue
            out.append(_site(path, src, m.start(), m.end(), after, name, "constant-flip"))
    return out


OPERATORS: tuple[Callable[[str, str, bytearray], list[MutationSite]], ...] = (
    op_token_sites,
    mux_arm_swap_sites,
    when_negate_sites,
    regnext_drop_sites,
    handshake_drop_ready_sites,
    fire_to_valid_sites,
    index_offby_one_sites,
    bool_const_flip_sites,
)


def enumerate_sites(path: str, src: str, *,
                    include_non_hardware: bool = False) -> list[MutationSite]:
    """Every mutation this file admits, in deterministic source order.

    Sites inside `NON_HARDWARE` constructs are annotated and, by default,
    dropped. They are still *enumerable* (pass ``include_non_hardware=True``) so
    the fraction of syntactic sites that are not hardware can be reported as a
    property of the operator set rather than quietly disappearing.
    """
    mask = code_mask(src)
    spans: list[tuple[int, int]] = []
    names: list[str] = []
    for nm in NON_HARDWARE:
        for _ns, lp, rp in find_call(src, nm, mask):
            spans.append((lp, rp))
            names.append(nm)

    sites: list[MutationSite] = []
    for op in OPERATORS:
        for s in op(path, src, mask):
            ctor = _enclosing_construct(s.start, spans, names)
            if ctor is not None:
                s = replace(s, in_construct=ctor)
            sites.append(s)
    if not include_non_hardware:
        sites = [s for s in sites if s.is_hardware]
    sites.sort(key=lambda s: (s.start, s.operator))
    return sites
