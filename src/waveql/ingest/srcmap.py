"""signal -> Chisel source. The edge that turns 'when' into 'where'.

The store joins waveform to commit log to golden trace, all keyed on cycle. That
answers *when* the machine misbehaved and *which signal* was wrong. It does not
answer **which line of Chisel to change**, and the measurements say that is
exactly where the loop breaks: on the repair tasks the tool arm localized the
failing cycle and then patched the wrong file every time.

The information was there all along. firtool annotates the SystemVerilog it
generates with the Chisel source location of every declaration --

    reg               rob_val_0;   // @[.../v3/exu/rob.scala:315:32]
    output            io_commit_valids_0,  // @[.../v3/exu/rob.scala:220:14]

-- 9,672 of them in `Rob.sv` alone, in 581 of 582 generated files. And chipyard
emits `model_module_hierarchy.json`, which maps every instance path to its
module. Together they give a total function from a waveform signal path to a
Chisel `file:line`.

THE LOCATOR GRAMMAR is small but not trivial, and getting it wrong silently
produces plausible wrong lines:

    @[a.scala:12:3]                     one location
    @[a.scala:12:3, :14:9]              continuation -- ':14:9' is still a.scala
    @[a.scala:12:3, b.scala:4:1, :9:2]  the FILE CAN CHANGE mid-list, and ':9:2'
                                        then belongs to b.scala, not a.scala
    @[a.scala:12:{3,7}]                 a set of columns on one line

412 locator lists in `Rob.sv` alone contain a second file, so the continuation
rule is load-bearing rather than a corner case.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SourceRef:
    chisel_file: str
    line: int
    col: int | None = None

    def __str__(self) -> str:
        return f"{self.chisel_file}:{self.line}" + (f":{self.col}" if self.col else "")


_LOCATOR = re.compile(r"//\s*@\[([^\]]*)\]")
# One entry: an optional file, a line, and either a column or a {set} of them.
_ENTRY = re.compile(
    r"(?:(?P<file>[A-Za-z0-9_./\-]+\.scala):)?(?P<line>\d+)"
    r"(?::(?:\{(?P<cols>[\d,]+)\}|(?P<col>\d+)))?"
)
# A declaration we can attach a locator to: a port, a wire, a reg or a logic.
_DECL = re.compile(
    r"^\s*(?:input|output|inout|wire|reg|logic)\b[^;,=]*?"
    r"(?P<name>[A-Za-z_][\w$]*)\s*(?:\[[^\]]*\])?\s*[;,=]"
)


def parse_locators(comment: str) -> list[SourceRef]:
    """Parse one `// @[...]` comment into its source references.

    The continuation rule is the whole reason this is a function rather than a
    regex: an entry with no file belongs to the most recently NAMED file, which
    is not necessarily the first one in the list.
    """
    m = _LOCATOR.search(comment)
    if not m:
        return []
    refs: list[SourceRef] = []
    current: str | None = None
    for e in _ENTRY.finditer(m.group(1)):
        f = e.group("file")
        if f:
            current = f
        if current is None:
            continue
        line = int(e.group("line"))
        if e.group("cols"):
            for c in e.group("cols").split(","):
                refs.append(SourceRef(current, line, int(c)))
        else:
            col = e.group("col")
            refs.append(SourceRef(current, line, int(col) if col else None))
    return refs


def module_signal_map(sv_path: Path) -> dict[str, list[SourceRef]]:
    """Every declared signal in one generated .sv, with its Chisel origin."""
    out: dict[str, list[SourceRef]] = {}
    for raw in sv_path.read_text(errors="replace").splitlines():
        if "@[" not in raw:
            continue
        d = _DECL.match(raw)
        if not d:
            continue
        refs = parse_locators(raw)
        if refs:
            out.setdefault(d.group("name"), refs)
    return out


def instance_to_module(hierarchy_json: Path) -> dict[str, str]:
    """Full dotted instance path -> module name, from chipyard's hierarchy."""
    root = json.loads(hierarchy_json.read_text())
    out: dict[str, str] = {}

    def walk(node: dict, prefix: str) -> None:
        inst = node.get("instance_name") or ""
        path = f"{prefix}.{inst}" if prefix else inst
        mod = node.get("module_name")
        if mod:
            out[path] = mod
        for child in node.get("instances") or []:
            walk(child, path)

    walk(root, "")
    return out


class SourceMap:
    """Resolve a waveform signal path to the Chisel line that declared it."""

    def __init__(self, gen_src: Path):
        self.gen_src = Path(gen_src)
        hier = self.gen_src / "model_module_hierarchy.json"
        if not hier.is_file():
            raise FileNotFoundError(f"no module hierarchy at {hier}")
        self.inst2mod = instance_to_module(hier)
        # A VCD path is rooted at `TOP.TestDriver.testHarness...` while the
        # hierarchy is rooted at `TestHarness...`, so the two share a SUFFIX, not
        # a prefix. Index every suffix; a suffix claimed by two different modules
        # is marked ambiguous (None) rather than resolved arbitrarily, because a
        # confidently wrong source line is worse than no source line.
        self._suffix: dict[str, str | None] = {}
        for path, mod in self.inst2mod.items():
            parts = path.split(".")
            for i in range(len(parts)):
                suf = ".".join(parts[i:])
                if suf in self._suffix and self._suffix[suf] != mod:
                    self._suffix[suf] = None
                else:
                    self._suffix.setdefault(suf, mod)
        self._cache: dict[str, dict[str, list[SourceRef]]] = {}
        self._collateral = self.gen_src / "gen-collateral"

    def _module_map(self, module: str) -> dict[str, list[SourceRef]]:
        if module not in self._cache:
            p = self._collateral / f"{module}.sv"
            self._cache[module] = module_signal_map(p) if p.is_file() else {}
        return self._cache[module]

    def module_of(self, signal_path: str) -> str | None:
        """Module of the instance that owns a signal, longest-prefix match.

        The VCD path is rooted at `TOP.TestDriver...` while the hierarchy is
        rooted at `TestHarness`, so the shared suffix is what matches.
        """
        parts = signal_path.split(".")[:-1]          # drop the signal leaf
        for start in range(len(parts)):              # longest suffix first
            mod = self._suffix.get(".".join(parts[start:]))
            if mod:
                return mod
        return None

    def resolve(self, signal_path: str) -> list[SourceRef]:
        """Chisel source references for one full waveform signal path."""
        mod = self.module_of(signal_path)
        if not mod:
            return []
        return self._module_map(mod).get(signal_path.rsplit(".", 1)[-1], [])
