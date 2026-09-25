"""A Scala lexical scanner, good enough to mutate source safely.

This is not a Scala parser and does not pretend to be one. It answers exactly one
question, correctly: **which byte offsets of this file are live code, and which
are comment or string?**

That question has to be answered before any rewrite, because BOOM is full of text
that *looks* mutable and is not:

    printf("rob_head === %d\\n", rob_head)   // === inside a string literal
    /* if (a <= b) ... */                     // a commented-out design
    // when (io.req.valid && io.req.ready)    // a note, not logic

Mutating any of those produces a mutant that either does not compile or, worse,
compiles and changes nothing while being recorded as ground truth. Both outcomes
silently corrupt a benchmark.

The Scala-specific traps handled here, each of which C-style scanners get wrong:

* **Block comments nest.** ``/* a /* b */ c */`` is one comment in Scala. A
  C-style scanner stops at the first ``*/`` and treats ``c */`` as code.
* **Triple-quoted strings** have no escape processing, so ``\"\"\"a\\\"\"\"`` ends
  where a naive escape-aware scanner thinks it continues.
* **Interpolated strings** (``s"..."``, ``f"..."``) embed code in ``${...}``.
  We deliberately classify the whole literal as non-code: the ``${}`` regions are
  almost always ``printf`` arguments, and a mutation inside a debug print is a
  mutant with no logical effect -- exactly the "survived" case we must not ship.
* **Character literals vs. symbol literals.** ``'a'`` is a char; ``'sym`` (legacy)
  is not, and has no closing quote. Treating ``'`` as always-paired swallows the
  rest of the line.
* **Backquoted identifiers** ``` `type` ``` may contain anything.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RegionKind(str, Enum):
    CODE = "code"
    LINE_COMMENT = "line_comment"
    BLOCK_COMMENT = "block_comment"
    STRING = "string"
    CHAR = "char"


@dataclass(frozen=True)
class Region:
    kind: RegionKind
    start: int
    end: int   # exclusive

    def __contains__(self, offset: int) -> bool:
        return self.start <= offset < self.end


# Scala's operator characters. A run of these is lexed by maximal munch into a
# single operator token, which is what makes `===` one token and not `==` + `=`.
OP_CHARS = set("!#%&*+-/:<=>?@\\^|~")

_IDENT_START = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ_$")
_IDENT_CONT = _IDENT_START | set("0123456789")


def classify(src: str) -> list[Region]:
    """Split `src` into contiguous regions. The regions tile the whole string."""
    regions: list[Region] = []
    n = len(src)
    i = 0
    code_start = 0

    def flush_code(upto: int) -> None:
        if upto > code_start:
            regions.append(Region(RegionKind.CODE, code_start, upto))

    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        # --- line comment ---
        if c == "/" and nxt == "/":
            flush_code(i)
            j = src.find("\n", i)
            j = n if j == -1 else j
            regions.append(Region(RegionKind.LINE_COMMENT, i, j))
            i = code_start = j
            continue

        # --- block comment (NESTING) ---
        if c == "/" and nxt == "*":
            flush_code(i)
            depth = 1
            j = i + 2
            while j < n and depth:
                if src.startswith("/*", j):
                    depth += 1
                    j += 2
                elif src.startswith("*/", j):
                    depth -= 1
                    j += 2
                else:
                    j += 1
            regions.append(Region(RegionKind.BLOCK_COMMENT, i, j))
            i = code_start = j
            continue

        # --- backquoted identifier ---
        if c == "`":
            flush_code(i)
            j = src.find("`", i + 1)
            j = n if j == -1 else j + 1
            # A backquoted identifier IS code, but its interior must not be
            # scanned for operators, so it is recorded as its own CODE region
            # boundary by simply skipping it.
            regions.append(Region(RegionKind.STRING, i, j))
            i = code_start = j
            continue

        # --- strings ---
        if c == '"':
            flush_code(i)
            if src.startswith('"""', i):
                # Triple-quoted: no escapes. Scala ends the literal at the LAST
                # quote of a run of >=3, so `"""a""""` is `a"`.
                j = i + 3
                while j < n:
                    if src.startswith('"""', j):
                        j += 3
                        while j < n and src[j] == '"':
                            j += 1
                        break
                    j += 1
                else:
                    j = n
            else:
                j = i + 1
                while j < n:
                    if src[j] == "\\":
                        j += 2
                        continue
                    if src[j] == '"':
                        j += 1
                        break
                    if src[j] == "\n":   # unterminated; do not run off the file
                        break
                    j += 1
                else:
                    j = n
            regions.append(Region(RegionKind.STRING, i, j))
            i = code_start = j
            continue

        # --- char literal vs. legacy symbol literal ---
        if c == "'":
            # 'x'  or  '\n'  or  'A'  are chars; 'ident is a symbol.
            j = i + 1
            if j < n and src[j] == "\\":
                k = j + 1
                while k < n and src[k] != "'" and src[k] != "\n":
                    k += 1
                if k < n and src[k] == "'":
                    flush_code(i)
                    regions.append(Region(RegionKind.CHAR, i, k + 1))
                    i = code_start = k + 1
                    continue
            elif j + 1 < n and src[j + 1] == "'":
                flush_code(i)
                regions.append(Region(RegionKind.CHAR, i, j + 2))
                i = code_start = j + 2
                continue
            # Symbol literal or stray quote: consume just the quote as code.
            i += 1
            continue

        i += 1

    flush_code(n)
    return regions


def code_mask(src: str) -> bytearray:
    """1 where the byte is live code, 0 where it is comment/string.

    A flat mask is the right shape here: every operator wants random access
    ("is offset 12345 mutable?"), not iteration over regions.
    """
    mask = bytearray(len(src))
    for r in classify(src):
        if r.kind is RegionKind.CODE:
            for k in range(r.start, r.end):
                mask[k] = 1
    return mask


@dataclass(frozen=True)
class OpToken:
    text: str
    start: int
    end: int


def operator_tokens(src: str, mask: bytearray | None = None) -> list[OpToken]:
    """Every maximal-munch operator token in live code.

    Maximal munch is what makes this safe. Scanning for the *substring* ``<``
    would fire inside ``<<`` (shift), ``<>`` (Chisel bulk connect), ``<-`` (for
    comprehension) and ``<=``; scanning for ``==`` would fire inside Chisel's
    ``===``. Emitting the whole run as one token means an operator rewrite can
    match on the complete token and never on a fragment of a different operator.
    """
    if mask is None:
        mask = code_mask(src)
    out: list[OpToken] = []
    i, n = 0, len(src)
    while i < n:
        if mask[i] and src[i] in OP_CHARS:
            j = i
            while j < n and mask[j] and src[j] in OP_CHARS:
                j += 1
            out.append(OpToken(src[i:j], i, j))
            i = j
        else:
            i += 1
    return out


def find_call(src: str, name: str, mask: bytearray | None = None) -> list[tuple[int, int, int]]:
    """Locate calls to `name` in live code, with balanced-paren argument spans.

    Returns ``(name_start, open_paren, close_paren)`` triples. The identifier
    must be a whole word -- otherwise ``Mux`` would match inside ``MuxCase`` and
    ``RegNext`` inside ``RegNextN``, producing rewrites that do not compile.
    """
    if mask is None:
        mask = code_mask(src)
    out: list[tuple[int, int, int]] = []
    start = 0
    while True:
        k = src.find(name, start)
        if k == -1:
            return out
        start = k + 1
        if not mask[k]:
            continue
        if k > 0 and src[k - 1] in _IDENT_CONT:
            continue
        j = k + len(name)
        if j < len(src) and src[j] in _IDENT_CONT:
            continue
        # Chisel style permits `Mux (a, b, c)`; skip whitespace to the paren.
        while j < len(src) and src[j] in " \t":
            j += 1
        if j >= len(src) or src[j] != "(":
            continue
        close = match_paren(src, j, mask)
        if close is not None:
            out.append((k, j, close))


def match_paren(src: str, open_idx: int, mask: bytearray | None = None) -> int | None:
    """Index of the ``)`` closing the ``(`` at `open_idx`, or None if unbalanced.

    Only live-code parens count, so a ``(`` inside ``printf("(")`` cannot throw
    the balance off.
    """
    if mask is None:
        mask = code_mask(src)
    depth = 0
    for i in range(open_idx, len(src)):
        if not mask[i]:
            continue
        if src[i] == "(":
            depth += 1
        elif src[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return None


def split_args(src: str, open_idx: int, close_idx: int, mask: bytearray | None = None) -> list[tuple[int, int]]:
    """Top-level comma-separated argument spans inside ``(...)``.

    "Top level" means depth-1 commas only, so ``Mux(a, Cat(b, c), d)`` splits into
    three arguments and not four. Brackets and braces are tracked too, because
    ``Mux(sel, x(i), Seq(a, b).reduce(_ || _))`` nests all three.
    """
    if mask is None:
        mask = code_mask(src)
    spans: list[tuple[int, int]] = []
    depth = 0
    arg_start = open_idx + 1
    for i in range(open_idx, close_idx + 1):
        if not mask[i]:
            continue
        c = src[i]
        if c in "([{":
            depth += 1
        elif c in ")]}":
            depth -= 1
            if depth == 0:
                spans.append((arg_start, i))
                break
        elif c == "," and depth == 1:
            spans.append((arg_start, i))
            arg_start = i + 1
    return spans


def line_col(src: str, offset: int) -> tuple[int, int]:
    """1-based (line, column) of a byte offset -- the shape ground truth needs."""
    line = src.count("\n", 0, offset) + 1
    bol = src.rfind("\n", 0, offset) + 1
    return line, offset - bol + 1
