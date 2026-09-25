"""Fan-in extraction from firtool-generated SystemVerilog.

A waveform tells you a signal is wrong. It does not tell you what made it wrong.
That gap is why cycle-accurate localization was not converting into repair: the
agent could see the symptom and could not walk back to the cause.

firtool's output is regular enough to recover the dataflow directly, without a
Verilog elaborator. Every signal has exactly one definition, in one of three
forms, and each carries the Chisel locators that produced it:

    wire [5:0] rob_head_idx = {rob_head, rob_head_lsb};   // @[rob.scala:227:29, ...]
    assign io_rob_tail_idx = rob_tail_idx;                // @[rob.scala:215:7, ...]
    always @(posedge clock) begin
      if (cond) rob_val_0 <= next;                        // @[rob.scala:315:32]
    end

For the sequential form the enclosing `if` conditions matter as much as the
right-hand side -- a register that is stuck is usually stuck because its enable
is false, not because its data is wrong -- so conditions are recorded as fan-in
too, flagged as control rather than data.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

from .srcmap import SourceRef, parse_locators

# Verilog identifiers, minus sized literals (2'h3), bare numbers, and keywords.
_IDENT = re.compile(r"(?<![\w'])[A-Za-z_][A-Za-z0-9_$]*")
_SIZED = re.compile(r"\b\d+'[bodhBODH][0-9a-fA-FxzXZ_?]+")
_KEYWORDS = frozenset("""
    wire reg logic assign always posedge negedge begin end if else case casez
    endcase default module endmodule input output inout parameter localparam
    initial final function endfunction task endtask signed unsigned automatic
    for while do return break continue struct union enum typedef const
""".split())

_DEF = re.compile(
    r"^\s*(?:(?P<kind>wire|logic|reg|assign)\s+)?"
    r"(?:(?P<sign>signed|unsigned)\s+)?"
    r"(?P<range>(?:\[[^\]]*\]\s*)*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*=\s*(?P<expr>.*?);\s*(?://\s*@\[(?P<loc>.*)\])?\s*$")
_DECL_ONLY = re.compile(
    r"^\s*(?P<kind>wire|logic|reg)\s+(?:signed\s+|unsigned\s+)?"
    r"(?P<range>(?:\[[^\]]*\]\s*)*)"
    r"(?P<name>[A-Za-z_][A-Za-z0-9_$]*)\s*;")
_RANGE = re.compile(r"\[\s*(\d+)\s*:\s*(\d+)\s*\]")


def _dims_of(range_text: str) -> tuple[int, int]:
    """(total bits, bits per index step) for a declaration's ranges.

    firtool emits packed multi-dimensional wires -- `wire [31:0][6:0] _GEN_68` is
    32 elements of 7 bits, and `_GEN_68[i]` selects an ELEMENT, not a bit. Read as
    a single 32-bit vector, every such index is silently wrong; that was 14 of 14
    evaluation mismatches when this was first measured.
    """
    dims = [abs(int(a) - int(b)) + 1 for a, b in _RANGE.findall(range_text or "")]
    if not dims:
        return 1, 1
    total = 1
    for d in dims:
        total *= d
    elem = dims[-1] if len(dims) > 1 else 1
    return total, elem
_NBA = re.compile(
    r"^\s*(?P<name>[A-Za-z_][A-Za-z0-9_$]*)"
    r"(?P<sel>(?:\s*\[[^\]]*\])*)\s*<=\s*(?P<expr>.*?);\s*(?://\s*@\[(?P<loc>.*)\])?\s*$")
_IF = re.compile(r"^\s*(?:end\s+)?(?:else\s+)?if\s*\((?P<cond>.*)\)\s*(?:begin)?\s*(?://.*)?$")
_ELSE = re.compile(r"^\s*(?:end\s*)?else\s*(?:begin)?\s*(?://.*)?$")
_ALWAYS = re.compile(r"^\s*always\s*@")
_COMMENT = re.compile(r"//.*$")


def identifiers(expr: str) -> list[str]:
    """Signal names referenced by a Verilog expression, in order, deduplicated."""
    expr = _SIZED.sub(" ", _COMMENT.sub("", expr))
    out, seen = [], set()
    for m in _IDENT.finditer(expr):
        n = m.group(0)
        if n in _KEYWORDS or n in seen:
            continue
        seen.add(n)
        out.append(n)
    return out


@dataclass(frozen=True)
class Definition:
    """The single place a signal gets its value, and what that value depends on."""
    name: str
    kind: str                       # wire | assign | reg
    expr: str
    data: tuple[str, ...]           # fan-in through the right-hand side
    control: tuple[str, ...] = ()   # fan-in through enclosing conditions
    refs: tuple[SourceRef, ...] = ()
    line: int = 0
    width: int = 1
    elem_width: int = 1       # bits selected by one index; >1 for packed arrays

    @property
    def fanin(self) -> tuple[str, ...]:
        seen, out = set(), []
        for n in self.data + self.control:
            if n not in seen:
                seen.add(n); out.append(n)
        return tuple(out)


def parse_module(sv_path: Path) -> dict[str, Definition]:
    """Every signal definition in one generated module.

    A signal assigned in several branches of an always block is merged: the
    union of the right-hand sides and the union of the conditions, because at
    this granularity the question is "what can affect this", not "what did".
    """
    text = sv_path.read_text(errors="replace")
    defs: dict[str, Definition] = {}
    cond_stack: list[str] = []          # (condition text, brace depth) as parallel lists
    depth_stack: list[int] = []
    depth = 0
    in_always = False
    skip_until = 0                      # `ifdef nesting level to skip back down to

    widths: dict[str, tuple[int, int]] = {}

    def merge(name: str, kind: str, expr: str, loc: str | None, line: int,
              control: list[str], dims: tuple[int, int] | None = None) -> None:
        data = identifiers(expr)
        refs = tuple(parse_locators(f"// @[{loc}]")) if loc else ()
        prev = defs.get(name)
        if prev is None:
            w, e = dims or widths.get(name, (1, 1))
            defs[name] = Definition(name, kind, expr, tuple(data), tuple(control),
                                    refs, line, w, e)
            return
        d = list(prev.data) + [x for x in data if x not in prev.data]
        c = list(prev.control) + [x for x in control if x not in prev.control]
        r = list(prev.refs) + [x for x in refs if x not in prev.refs]
        defs[name] = Definition(name, prev.kind, prev.expr + " ; " + expr,
                                tuple(d), tuple(c), tuple(r), prev.line, prev.width,
                                prev.elem_width)

    for i, raw in enumerate(text.splitlines(), 1):
        line = raw.rstrip()
        if not line.strip():
            continue
        # Register/memory randomization is simulation scaffolding, not dataflow.
        # Left in, every reg picks up a fan-in on `_RANDOM` and the slice fills
        # with edges that do not exist in hardware.
        st = line.strip()
        if skip_until:
            if st.startswith("`ifdef") or st.startswith("`ifndef"):
                skip_until += 1
            elif st.startswith("`endif"):
                skip_until -= 1
            continue
        if st.startswith("`ifdef ENABLE_INITIAL_"):
            skip_until = 1
            continue
        if _ALWAYS.search(line):
            in_always = True
            depth = 0
            cond_stack.clear(); depth_stack.clear()

        m = _NBA.match(line)
        if m:
            ctrl: list[str] = []
            for c in cond_stack:
                ctrl += [x for x in identifiers(c) if x not in ctrl]
            ctrl += [x for x in identifiers(m.group("sel") or "") if x not in ctrl]
            merge(m.group("name"), "reg", m.group("expr"), m.group("loc"), i, ctrl)
        elif not in_always:
            m = _DEF.match(line)
            if m and m.group("name") not in _KEYWORDS:
                merge(m.group("name"), m.group("kind") or "wire", m.group("expr"),
                      m.group("loc"), i, [], _dims_of(m.group("range")))
            else:
                md = _DECL_ONLY.match(line)
                if md:                       # declared here, driven elsewhere
                    widths[md.group("name")] = _dims_of(md.group("range"))

        if in_always:
            mi = _IF.match(line)
            me = _ELSE.match(line)
            if mi:
                while depth_stack and depth_stack[-1] >= depth:
                    depth_stack.pop(); cond_stack.pop()
                cond_stack.append(mi.group("cond")); depth_stack.append(depth)
            elif me:
                while depth_stack and depth_stack[-1] >= depth:
                    depth_stack.pop(); cond_stack.pop()
            bare = _COMMENT.sub("", line)
            depth += bare.count("begin") - bare.count("end")
            if depth <= 0 and "end" in bare and "always" not in bare:
                in_always = False
                cond_stack.clear(); depth_stack.clear()
    return defs


@dataclass
class SliceStep:
    """One signal on a backward slice, with how it was reached."""
    name: str
    depth: int
    via: str                    # data | control | root
    parent: str | None
    refs: tuple[SourceRef, ...] = field(default=())
    kind: str = ""


class Netlist:
    """Fan-in queries over one generated-source tree."""

    def __init__(self, gen_src: Path):
        self.collateral = Path(gen_src) / "gen-collateral"
        if not self.collateral.is_dir():
            raise FileNotFoundError(f"no gen-collateral under {gen_src}")

    @lru_cache(maxsize=256)
    def module(self, name: str) -> dict[str, Definition]:
        p = self.collateral / f"{name}.sv"
        return parse_module(p) if p.is_file() else {}

    def define(self, module: str, signal: str) -> Definition | None:
        return self.module(module).get(signal)

    def backward_slice(self, module: str, signal: str, *, max_depth: int = 6,
                       max_nodes: int = 400, keep: set[str] | None = None,
                       follow_control: bool = True) -> list[SliceStep]:
        """Signals reachable backwards from `signal`, breadth first.

        `keep`, when given, restricts traversal to signals in that set -- the
        point of intersecting with the waveform is that a slice through signals
        that never went wrong is a slice through irrelevant logic.
        """
        defs = self.module(module)
        out = [SliceStep(signal, 0, "root", None, (), defs.get(signal).kind
                         if signal in defs else "")]
        seen = {signal}
        frontier = [(signal, 0)]
        while frontier and len(out) < max_nodes:
            cur, d = frontier.pop(0)
            if d >= max_depth:
                continue
            dfn = defs.get(cur)
            if dfn is None:
                continue
            nxt = [(n, "data") for n in dfn.data]
            if follow_control:
                nxt += [(n, "control") for n in dfn.control]
            for name, via in nxt:
                if name in seen or name not in defs:
                    continue
                if keep is not None and name not in keep:
                    continue
                seen.add(name)
                sub = defs[name]
                out.append(SliceStep(name, d + 1, via, cur, sub.refs, sub.kind))
                frontier.append((name, d + 1))
                if len(out) >= max_nodes:
                    break
        return out


# --------------------------------------------------------------------------
# Crossing module boundaries.
#
# A single-module slice is not enough: in this corpus the failure surfaces in
# the ROB or the commit log, and the defect is in the LSU, the dcache, the FPU,
# the rename stage or the issue unit. The slice has to walk out of the module it
# starts in, which means resolving ports in both directions:
#
#   UP    a signal that is an INPUT port of M is really the expression the
#         parent connected to it, so the slice continues in the parent
#   DOWN   a signal in M with no local definition, connected to a child
#         instance's OUTPUT port, continues inside that child
# --------------------------------------------------------------------------

_MODULE = re.compile(r"^module\s+(?P<name>[A-Za-z_][\w$]*)\s*\(")
_PORT = re.compile(
    r"^\s*(?P<dir>input|output|inout)\s+(?:wire\s+|reg\s+|logic\s+)?"
    r"(?:\[[^\]]*\]\s*)*(?P<name>[A-Za-z_][\w$]*)\s*(?:,|;|//|$)")
_INST_OPEN = re.compile(
    r"^\s{0,4}(?P<mod>[A-Za-z_][\w$]*)\s+(?P<inst>[A-Za-z_][\w$]*)\s*\(\s*(?://.*)?$")
_CONN = re.compile(r"^\s*\.(?P<port>[A-Za-z_][\w$]*)\s*\((?P<expr>.*?)\)\s*,?\s*(?://.*)?$")


@dataclass(frozen=True)
class Instance:
    name: str
    module: str
    conns: dict[str, str]           # port -> expression in the PARENT


def parse_ports(sv_path: Path) -> dict[str, str]:
    """Port name -> direction for the module declared in this file."""
    out: dict[str, str] = {}
    started = False
    for raw in sv_path.read_text(errors="replace").splitlines():
        if not started:
            if _MODULE.match(raw):
                started = True
            continue
        if raw.startswith(");") or raw.strip() == ");":
            break
        m = _PORT.match(raw)
        if m:
            out[m.group("name")] = m.group("dir")
    return out


def parse_instances(sv_path: Path) -> list[Instance]:
    """Child instances of the module in this file, with their port wiring."""
    lines = sv_path.read_text(errors="replace").splitlines()
    out: list[Instance] = []
    i = 0
    while i < len(lines):
        m = _INST_OPEN.match(lines[i])
        if not m or m.group("mod") in _KEYWORDS:
            i += 1
            continue
        conns: dict[str, str] = {}
        j = i + 1
        ok = False
        while j < len(lines) and j - i < 4000:
            s = lines[j]
            if re.match(r"^\s*\);", s):
                ok = True
                break
            c = _CONN.match(s)
            if c:
                conns[c.group("port")] = c.group("expr").strip()
            elif s.strip() and not s.strip().startswith("//"):
                break                       # not an instance body after all
            j += 1
        if ok and conns:
            out.append(Instance(m.group("inst"), m.group("mod"), conns))
            i = j + 1
        else:
            i += 1
    return out


@dataclass
class HierStep:
    """One node of a hierarchical backward slice."""
    path: str                       # instance path, e.g. ...core.rob
    signal: str
    depth: int
    via: str                        # data | control | port-up | port-down | root
    parent: str | None
    refs: tuple[SourceRef, ...] = ()
    module: str = ""

    @property
    def full(self) -> str:
        return f"{self.path}.{self.signal}"


class HierNetlist(Netlist):
    """Backward slicing that follows signals across module boundaries."""

    def __init__(self, gen_src: Path, inst2mod: dict[str, str]):
        super().__init__(gen_src)
        self.inst2mod = dict(inst2mod)
        # A hierarchy path in the waveform is rooted differently from the JSON,
        # so index by suffix exactly as SourceMap does.
        self._suffix: dict[str, str | None] = {}
        for p, mod in self.inst2mod.items():
            parts = p.split(".")
            for i in range(len(parts)):
                s = ".".join(parts[i:])
                if s in self._suffix and self._suffix[s] != mod:
                    self._suffix[s] = None
                else:
                    self._suffix.setdefault(s, mod)

    def module_at(self, path: str) -> str | None:
        parts = path.split(".")
        for i in range(len(parts)):
            m = self._suffix.get(".".join(parts[i:]))
            if m:
                return m
        return None

    @lru_cache(maxsize=256)
    def ports(self, module: str) -> dict[str, str]:
        p = self.collateral / f"{module}.sv"
        return parse_ports(p) if p.is_file() else {}

    @lru_cache(maxsize=256)
    def instances(self, module: str) -> tuple[Instance, ...]:
        p = self.collateral / f"{module}.sv"
        return tuple(parse_instances(p)) if p.is_file() else ()

    @lru_cache(maxsize=256)
    def _driven_by_child(self, module: str) -> dict[str, tuple[str, str]]:
        """Parent signal -> (instance, port) for every child OUTPUT connection."""
        out: dict[str, tuple[str, str]] = {}
        for inst in self.instances(module):
            pr = self.ports(inst.module)
            for port, expr in inst.conns.items():
                if pr.get(port) != "output":
                    continue
                ids = identifiers(expr)
                if len(ids) == 1 and ids[0] == expr.strip():
                    out.setdefault(ids[0], (inst.name, port))
        return out

    def boundary(self, path: str, signal: str) -> tuple[str, str, str] | None:
        """Where a signal with no local definition actually gets its value.

        Returns ``(kind, path, text)``: ``("up", parent, expr)`` for an input port,
        whose value is the parent's connection expression evaluated in the parent;
        ``("down", child_path, port)`` for a signal driven by a child's output.
        """
        mod = self.module_at(path)
        if not mod:
            return None
        if self.ports(mod).get(signal) == "input" and "." in path:
            parent, inst = path.rsplit(".", 1)
            pmod = self.module_at(parent)
            if pmod:
                for i in self.instances(pmod):
                    if i.name == inst and signal in i.conns:
                        return ("up", parent, i.conns[signal])
            return None
        child = self._driven_by_child(mod).get(signal)
        if child:
            return ("down", f"{path}.{child[0]}", child[1])
        return None

    def step(self, path: str, signal: str) -> list[tuple[str, str, str]]:
        """One backward step: the (path, signal, via) that can affect this one."""
        mod = self.module_at(path)
        if not mod:
            return []
        out: list[tuple[str, str, str]] = []
        dfn = self.define(mod, signal)
        if dfn is not None:
            for n in dfn.data:
                out.append((path, n, "data"))
            for n in dfn.control:
                out.append((path, n, "control"))
            return out
        # No local definition: it is a boundary.
        if self.ports(mod).get(signal) == "input" and "." in path:
            parent = path.rsplit(".", 1)[0]
            inst = path.rsplit(".", 1)[1]
            pmod = self.module_at(parent)
            if pmod:
                for i in self.instances(pmod):
                    if i.name == inst and signal in i.conns:
                        for n in identifiers(i.conns[signal]):
                            out.append((parent, n, "port-up"))
                        break
            return out
        child = self._driven_by_child(mod).get(signal)
        if child:
            out.append((f"{path}.{child[0]}", child[1], "port-down"))
        return out

    def slice(self, path: str, signal: str, *, max_depth: int = 8,
              max_nodes: int = 600, keep: set[str] | None = None) -> list[HierStep]:
        """Hierarchical backward slice from one waveform signal.

        `keep` is a set of FULL waveform paths; when given, the walk only enters
        signals that the waveform actually recorded, so the slice is restricted
        to logic the run exercised rather than to logic that merely exists.
        """
        root = HierStep(path, signal, 0, "root", None, (), self.module_at(path) or "")
        out = [root]
        seen = {(path, signal)}
        q = [(path, signal, 0)]
        while q and len(out) < max_nodes:
            p, s, d = q.pop(0)
            if d >= max_depth:
                continue
            for np_, ns, via in self.step(p, s):
                if (np_, ns) in seen:
                    continue
                if keep is not None and via in ("data", "control") \
                        and f"{np_}.{ns}" not in keep:
                    continue
                seen.add((np_, ns))
                m = self.module_at(np_) or ""
                dfn = self.define(m, ns) if m else None
                out.append(HierStep(np_, ns, d + 1, via, f"{p}.{s}",
                                    dfn.refs if dfn else (), m))
                q.append((np_, ns, d + 1))
                if len(out) >= max_nodes:
                    break
        return out
