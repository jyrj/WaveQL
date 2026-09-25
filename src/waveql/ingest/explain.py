"""Why is this signal this value?

A backward cone says what *could* have affected a signal. On a BOOM core that is
thousands of nodes and it does not distinguish the one operand that forced the
result from the hundreds that are merely connected -- measured on this corpus,
the buggy file ranked 26th of 28 that way.

The question worth asking is narrower and has a much shorter answer: given that
`will_commit_0` is 0 at the cycle the pipeline stopped, WHICH operand made it 0?
An AND is 0 because of its false operands, not its true ones; a mux takes one arm
and the other cannot be blamed. Following only the operands that account for the
value turns a cone into a chain, and the chain ends at the logic responsible.

This needs values for firtool's anonymous temporaries, which Verilator does not
trace -- 76% of a cone is `_GEN_*`. They are all combinational, so they are
evaluated from their definitions, recursing until the traced registers and ports
underneath them.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Callable

from .netlist import Definition, HierNetlist
from .srcmap import SourceRef

# --------------------------------------------------------------------------
# A parser for the Verilog subset firtool emits.
# --------------------------------------------------------------------------

_TOK = re.compile(r"""
    (?P<space>\s+)
  | (?P<sized>\d+'[bodhBODH][0-9a-fA-FxzXZ_?]+)
  | (?P<num>\d+)
  | (?P<ident>[A-Za-z_][A-Za-z0-9_$]*)
  | (?P<op><<<|>>>|<<|>>|<=|>=|==|!=|&&|\|\||[-+*/%&|^~!?:<>(){}\[\],.])
""", re.X)


def tokenize(src: str) -> list[str]:
    out, i = [], 0
    while i < len(src):
        m = _TOK.match(src, i)
        if not m:
            i += 1
            continue
        i = m.end()
        if m.lastgroup != "space":
            out.append(m.group(0))
    return out


def _sized_value(tok: str) -> int | None:
    n, _, rest = tok.partition("'")
    base, digits = rest[0].lower(), rest[1:].replace("_", "")
    if any(c in "xzXZ?" for c in digits):
        return None
    try:
        return int(digits, {"b": 2, "o": 8, "d": 10, "h": 16}[base])
    except (ValueError, KeyError):
        return None


def _sized_width(tok: str) -> int:
    n = tok.split("'", 1)[0]
    return int(n) if n.isdigit() else 1


def _asr(value: int, amount: int, width: int) -> int:
    """Arithmetic shift right within `width` bits, sign-extending."""
    amount = min(amount, 256)
    mask = (1 << width) - 1
    value &= mask
    if width and (value >> (width - 1)) & 1:                # negative
        return ((value >> amount) | (~0 << max(width - amount, 0))) & mask
    return value >> amount


# Binding powers, loosest first. Matches Verilog precedence closely enough for
# the shapes firtool emits; parentheses carry the rest.
_BP = {
    "||": 2, "&&": 3, "|": 4, "^": 5, "&": 6,
    "==": 7, "!=": 7, "<": 8, ">": 8, "<=": 8, ">=": 8,
    "<<": 9, ">>": 9, "<<<": 9, ">>>": 9,
    "+": 10, "-": 10, "*": 11, "/": 11, "%": 11,
}


@dataclass
class Node:
    kind: str                       # num | id | unop | binop | mux | concat | repeat | index | slice
    text: str = ""
    args: list["Node"] = field(default_factory=list)
    value: int | None = None
    width: int = 1
    elem: int = 1                   # bits per index step, for packed arrays


class _Parser:
    def __init__(self, toks: list[str]):
        self.t, self.i = toks, 0

    def peek(self) -> str | None:
        return self.t[self.i] if self.i < len(self.t) else None

    def take(self) -> str:
        tok = self.t[self.i]
        self.i += 1
        return tok

    def parse(self, bp: int = 0) -> Node:
        left = self.unary()
        while True:
            op = self.peek()
            if op == "?":
                self.take()
                a = self.parse(0)
                if self.peek() == ":":
                    self.take()
                b = self.parse(0)
                left = Node("mux", "?:", [left, a, b])
                continue
            if op is None or op not in _BP or _BP[op] <= bp:
                return left
            self.take()
            right = self.parse(_BP[op])
            left = Node("binop", op, [left, right])

    def unary(self) -> Node:
        tok = self.peek()
        if tok is None:
            return Node("num", "0", value=0)
        if tok in ("~", "!", "-", "&", "|", "^"):
            self.take()
            return Node("unop", tok, [self.unary()])
        if tok == "(":
            self.take()
            n = self.parse(0)
            if self.peek() == ")":
                self.take()
            return self.postfix(n)
        if tok == "{":
            return self.postfix(self.braces())
        self.take()
        if "'" in tok:
            return self.postfix(Node("num", tok, value=_sized_value(tok),
                                     width=_sized_width(tok)))
        if tok.isdigit():
            return self.postfix(Node("num", tok, value=int(tok), width=32))
        return self.postfix(Node("id", tok))

    def braces(self) -> Node:
        self.take()                                  # {
        items: list[Node] = []
        first = self.parse(0)
        if self.peek() == "{":                       # replication {n{x}}
            inner = self.braces()
            if self.peek() == "}":
                self.take()
            return Node("repeat", "{{}}", [first, inner])
        items.append(first)
        while self.peek() == ",":
            self.take()
            items.append(self.parse(0))
        if self.peek() == "}":
            self.take()
        return Node("concat", "{}", items)

    def postfix(self, n: Node) -> Node:
        while self.peek() == "[":
            self.take()
            a = self.parse(0)
            if self.peek() == ":":
                self.take()
                b = self.parse(0)
                if self.peek() == "]":
                    self.take()
                n = Node("slice", "[:]", [n, a, b])
            else:
                if self.peek() == "]":
                    self.take()
                n = Node("index", "[]", [n, a])
        return n


def parse_expr(src: str) -> Node:
    return _Parser(tokenize(src)).parse(0)


# --------------------------------------------------------------------------
# Evaluation against a waveform, falling through to the netlist.
# --------------------------------------------------------------------------

UNKNOWN = None


class Evaluator:
    """Values for signals in one module instance at one cycle.

    ``traced`` is the waveform: it answers for registers, ports and any named
    wire Verilator dumped. Everything else is recomputed from its definition,
    which is what makes firtool's temporaries usable at all.
    """

    def __init__(self, nl: HierNetlist, traced: Callable[[str, str], int | None],
                 max_depth: int = 64):
        self.nl = nl
        self.traced = traced
        self.max_depth = max_depth
        self._memo: dict[tuple[str, str], int | None] = {}
        self._active: set[tuple[str, str]] = set()

    def signal(self, path: str, name: str, depth: int = 0) -> int | None:
        key = (path, name)
        if key in self._memo:
            return self._memo[key]
        if key in self._active or depth > self.max_depth:
            return UNKNOWN                      # a combinational loop, or too deep
        v = self.traced(path, name)
        if v is not None:
            self._memo[key] = v
            return v
        mod = self.nl.module_at(path)
        dfn = self.nl.define(mod, name) if mod else None
        if dfn is None:
            # No definition here means a module boundary, not a dead end. Most of
            # a parent module's untraced wires are child outputs; stopping at them
            # left 69% of BoomCore unevaluable.
            b = self.nl.boundary(path, name)
            if b is None:
                self._memo[key] = UNKNOWN
                return UNKNOWN
            self._active.add(key)
            try:
                if b[0] == "up":
                    val = self.eval(parse_expr(b[2]), b[1], depth + 1)
                else:
                    val = self.signal(b[1], b[2], depth + 1)
            finally:
                self._active.discard(key)
            self._memo[key] = val
            return val
        # A register's value is state: if it was not traced, it is genuinely not
        # recoverable from the netlist alone.
        if dfn.kind == "reg":
            self._memo[key] = UNKNOWN
            return UNKNOWN
        self._active.add(key)
        try:
            val = self.eval(parse_expr(dfn.expr), path, depth + 1)
        finally:
            self._active.discard(key)
        self._memo[key] = val
        return val

    def width(self, path: str, name: str) -> int:
        mod = self.nl.module_at(path)
        d = self.nl.define(mod, name) if mod else None
        return d.width if d else 1

    def dims(self, path: str, name: str) -> tuple[int, int]:
        mod = self.nl.module_at(path)
        d = self.nl.define(mod, name) if mod else None
        if d:
            return d.width, d.elem_width
        b = self.nl.boundary(path, name)
        if b and b[0] == "down":
            return self.dims(b[1], b[2])
        return 1, 1

    def eval(self, n: Node, path: str, depth: int = 0) -> int | None:
        """Evaluate and RECORD the result on the node.

        Recording is not incidental: `responsible` apportions blame by comparing
        each operand's value against its parent's, so an interior node with no
        value recorded falls through to blaming every operand -- which quietly
        turns the whole walk back into the undirected cone it exists to replace.
        """
        v = self._eval(n, path, depth)
        n.value = v
        return v

    def _eval(self, n: Node, path: str, depth: int = 0) -> int | None:
        if n.kind == "num":
            return n.value
        if n.kind == "id":
            n.width, n.elem = self.dims(path, n.text)
            v = self.signal(path, n.text, depth)
            n.value = v
            return v
        if n.kind == "unop":
            a = self.eval(n.args[0], path, depth)
            if a is None:
                return None
            w = n.args[0].width or 1
            return {"~": lambda x: (~x) & ((1 << w) - 1),
                    "!": lambda x: int(x == 0),
                    "-": lambda x: (-x) & ((1 << w) - 1),
                    "&": lambda x: int(x == (1 << w) - 1),
                    "|": lambda x: int(x != 0),
                    "^": lambda x: bin(x).count("1") & 1}[n.text](a)
        if n.kind == "binop":
            a = self.eval(n.args[0], path, depth)
            b = self.eval(n.args[1], path, depth)
            if a is None or b is None:
                return None
            op = n.text
            try:
                return {"&": lambda: a & b, "|": lambda: a | b, "^": lambda: a ^ b,
                        "&&": lambda: int(bool(a) and bool(b)),
                        "||": lambda: int(bool(a) or bool(b)),
                        "==": lambda: int(a == b), "!=": lambda: int(a != b),
                        "<": lambda: int(a < b), ">": lambda: int(a > b),
                        "<=": lambda: int(a <= b), ">=": lambda: int(a >= b),
                        "<<": lambda: a << min(b, 256), ">>": lambda: a >> min(b, 256),
                        "<<<": lambda: a << min(b, 256),
                        # Arithmetic shift right sign-extends. Treating it as a
                        # logical shift gives a plausible wrong number rather than
                        # an error -- 0xF0 >>> 4 as int8 is 0xFF, not 0x0F. firtool
                        # emits no >>> at all in this design (0 occurrences in
                        # 44,722 lines), so this is latent, but a silently wrong
                        # value is the exact failure mode this evaluator exists to
                        # avoid.
                        ">>>": lambda: _asr(a, b, n.args[0].width or 1),
                        "+": lambda: a + b, "-": lambda: a - b, "*": lambda: a * b,
                        "/": lambda: a // b if b else 0,
                        "%": lambda: a % b if b else 0}[op]()
            except KeyError:
                return None
        if n.kind == "mux":
            c = self.eval(n.args[0], path, depth)
            if c is None:
                return None
            return self.eval(n.args[1] if c else n.args[2], path, depth)
        if n.kind == "concat":
            out, w = 0, 0
            for a in reversed(n.args):
                v = self.eval(a, path, depth)
                if v is None:
                    return None
                aw = a.width or 1
                out |= (v & ((1 << aw) - 1)) << w
                w += aw
            n.width = w
            return out
        if n.kind == "repeat":
            cnt = self.eval(n.args[0], path, depth)
            v = self.eval(n.args[1], path, depth)
            if cnt is None or v is None:
                return None
            aw = n.args[1].width or 1
            out = 0
            for _ in range(min(cnt, 4096)):
                out = (out << aw) | (v & ((1 << aw) - 1))
            n.width = aw * (cnt or 0)
            return out
        if n.kind == "index":
            a = self.eval(n.args[0], path, depth)
            i = self.eval(n.args[1], path, depth)
            if a is None or i is None:
                return None
            e = n.args[0].elem or 1
            n.width = e
            return (a >> (i * e)) & ((1 << e) - 1)
        if n.kind == "slice":
            a = self.eval(n.args[0], path, depth)
            hi = self.eval(n.args[1], path, depth)
            lo = self.eval(n.args[2], path, depth)
            if a is None or hi is None or lo is None:
                return None
            hi, lo = max(hi, lo), min(hi, lo)
            n.width = hi - lo + 1
            return (a >> lo) & ((1 << (hi - lo + 1)) - 1)
        return None


# --------------------------------------------------------------------------
# Responsibility: which operands account for the value.
# --------------------------------------------------------------------------

def responsible(n: Node) -> list[Node]:
    """The children that explain this node's value, not merely feed it."""
    v = n.value
    if n.kind == "binop" and n.text in ("&", "&&"):
        # 0 because something was 0; 1 needs all of them.
        return [a for a in n.args if a.value == 0] if v == 0 else list(n.args)
    if n.kind == "binop" and n.text in ("|", "||"):
        return [a for a in n.args if a.value not in (0, None)] if v else list(n.args)
    if n.kind == "mux":
        c = n.args[0]
        return [c, n.args[1] if c.value else n.args[2]]
    if n.kind == "unop" and n.text in ("~", "!"):
        return [n.args[0]]
    return list(n.args)


@dataclass
class Why:
    """One link in the causal chain, with where it lives in Chisel."""
    path: str
    signal: str
    value: int | None
    depth: int
    expr: str = ""
    refs: tuple[SourceRef, ...] = ()
    module: str = ""
    reason: str = ""
    parent: str | None = None      # the link this one was reached from

    @property
    def rel(self) -> str:
        return f"{self.path}.{self.signal}"


class Explainer:
    """Value-directed backward walk: follow only what accounts for the value."""

    def __init__(self, nl: HierNetlist, ev: Evaluator):
        self.nl, self.ev = nl, ev

    def _element(self, path: str, base: str, i: int) -> str | None:
        """Resolve ``base[i]`` to the one signal it selects.

        firtool builds per-entry state into packed arrays -- `_GEN` is
        `{{rob_val_31}, ..., {rob_val_0}}` -- so blaming `_GEN[rob_head]` as a
        whole drags in all 32 entries when exactly one was read. On a ROB that
        alone exhausted the walk's budget before it ever left the module.
        """
        mod = self.nl.module_at(path)
        d = self.nl.define(mod, base) if mod else None
        if d is None:
            return None
        tree = parse_expr(d.expr.split(" ; ")[0])
        if tree.kind != "concat":
            return None
        items = tree.args
        if not (0 <= i < len(items)):
            return None
        node = items[len(items) - 1 - i]          # concat lists MSB first
        while node.kind == "concat" and len(node.args) == 1:
            node = node.args[0]
        return node.text if node.kind == "id" else None

    def _leaves(self, node: Node, path: str, out: list[str]) -> None:
        """Signal names reachable through responsible operands only."""
        if node.kind == "id":
            out.append(node.text)
            return
        if node.kind == "index" and node.args[0].kind == "id":
            base, iv = node.args[0].text, node.args[1].value
            if iv is not None:
                elem = self._element(path, base, iv)
                if elem is not None:
                    out.append(elem)
                    self._leaves(node.args[1], path, out)   # and why that index
                    return
        for c in responsible(node):
            self._leaves(c, path, out)

    def why(self, path: str, signal: str, *, max_depth: int = 12,
            max_nodes: int = 60) -> list[Why]:
        mod = self.nl.module_at(path)
        if not mod:
            return []
        root_v = self.ev.signal(path, signal)
        dfn = self.nl.define(mod, signal)
        out = [Why(path, signal, root_v, 0, dfn.expr if dfn else "",
                   dfn.refs if dfn else (), mod, "the signal in question")]
        root_rel = f"{path}.{signal}"
        seen = {(path, signal)}
        q = [(path, signal, 0)]
        while q and len(out) < max_nodes:
            p, s, d = q.pop(0)
            if d >= max_depth:
                continue
            m = self.nl.module_at(p)
            dfn = self.nl.define(m, s) if m else None
            if dfn is None:
                # A boundary: cross it structurally, since there is no expression
                # here to apportion blame with.
                for np_, ns, via in self.nl.step(p, s):
                    if (np_, ns) in seen:
                        continue
                    seen.add((np_, ns))
                    m2 = self.nl.module_at(np_)
                    d2 = self.nl.define(m2, ns) if m2 else None
                    out.append(Why(np_, ns, self.ev.signal(np_, ns), d + 1,
                                   d2.expr if d2 else "", d2.refs if d2 else (),
                                   m2 or "", via, f"{p}.{s}"))
                    q.append((np_, ns, d + 1))
                continue
            tree = parse_expr(dfn.expr.split(" ; ")[0] if dfn.kind == "reg" else dfn.expr)
            self.ev.eval(tree, p)
            names: list[str] = []
            self._leaves(tree, p, names)
            # A register that is holding is holding because its enable is false,
            # so the control terms matter as much as the data ones.
            if dfn.kind == "reg":
                names += [c for c in dfn.control if c not in names]
            for ns in names:
                if (p, ns) in seen:
                    continue
                seen.add((p, ns))
                d2 = self.nl.define(m, ns)
                v = self.ev.signal(p, ns)
                why = "accounts for the value" if d2 else "boundary"
                out.append(Why(p, ns, v, d + 1, d2.expr if d2 else "",
                               d2.refs if d2 else (), m, why, f"{p}.{s}"))
                q.append((p, ns, d + 1))
                if len(out) >= max_nodes:
                    break
        return out
