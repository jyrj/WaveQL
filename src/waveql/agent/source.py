"""Source access for the repair task, and the patch contract.

Localization needs only evidence. Repair needs the code — so the agent must be
able to read the Chisel source it is being asked to fix. That opens two holes a
benchmark has to close, and they are closed here by construction rather than by
policy:

**The agent sees only the MUTATED tree.** It has no filesystem access at all;
it sees exactly what these tools serve, and they serve one checkout. The
unmutated original is never on any path the agent can name, so there is nothing
to deny-list and nothing to probe for. The injection diff likewise does not
exist in the agent's world.

**A patch is a structured edit, not free text.** ``propose_fix`` takes
(path, old_text, new_text) and the harness requires `old_text` to occur exactly
once in the file. A unified diff would let a near-miss apply at the wrong offset
or half-apply; an exact-match splice either lands where the agent meant or is
rejected with a reason it can act on.

What the agent may NOT repair its way out of is enforced by the verifier, not
here: see waveql.corpus.verify.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from waveql.store.store import Result

MAX_LINES = 160          # per read; the same order as every other capped answer
MAX_MATCHES = 60


@dataclass
class SourceView:
    """A read-only view of one checkout, rooted so paths cannot escape it."""

    root: Path
    subtree: str = "generators/boom/src/main/scala/v3"
    calls: list = field(default_factory=list)
    proposals: list = field(default_factory=list)
    # Rejected attempts are recorded, not discarded. An episode where the agent
    # proposed four times and every anchor missed is a DIFFERENT outcome from one
    # where it never proposed, and reporting both as "never called propose_fix"
    # misattributes a harness-shaped failure to the agent.
    rejected: list = field(default_factory=list)

    # --- path safety --------------------------------------------------------

    def _resolve(self, rel: str) -> Path:
        """Resolve a repo-relative path, refusing anything outside the subtree.

        Not defence against a malicious agent -- there is no shell here -- but
        against a confused one wandering into rocket-chip or the toolchain and
        spending its budget there.
        """
        rel = rel.strip().lstrip("/")
        if rel.startswith(self.subtree):
            p = (self.root / rel).resolve()
        else:
            p = (self.root / self.subtree / rel).resolve()
        base = (self.root / self.subtree).resolve()
        if not str(p).startswith(str(base)):
            raise ValueError(f"path outside the design subtree: {rel}")
        return p

    def _wrap(self, op: str, cols: list[str], rows: list[tuple], truncated: bool) -> Result:
        self.calls.append(op)
        return Result(cols, rows, truncated, op, 0.0)

    # --- reading ------------------------------------------------------------

    def list_files(self, pattern: str = "", limit: int = 80) -> Result:
        """Chisel files of the design under test, optionally filtered."""
        base = (self.root / self.subtree).resolve()
        rx = re.compile(pattern) if pattern else None
        rows = []
        for f in sorted(base.rglob("*.scala")):
            rel = str(f.relative_to(base))
            if rx and not rx.search(rel):
                continue
            rows.append((rel, sum(1 for _ in f.open(errors="replace"))))
        return self._wrap("list_files", ["file", "lines"], rows[:limit], len(rows) > limit)

    def read_source(self, path: str, start: int = 1, count: int = MAX_LINES) -> Result:
        """Read a line range. 1-based, inclusive, capped."""
        p = self._resolve(path)
        if not p.is_file():
            raise ValueError(f"no such file: {path}")
        lines = p.read_text(errors="replace").splitlines()
        start = max(1, int(start))
        count = min(int(count), MAX_LINES)
        chunk = lines[start - 1: start - 1 + count]
        return self._wrap("read_source", ["line", "text"],
                          [(start + i, t) for i, t in enumerate(chunk)],
                          start - 1 + count < len(lines))

    def search_source(self, pattern: str, file_pattern: str = "",
                      context: int = 0, limit: int = MAX_MATCHES) -> Result:
        """Regex search across the design's Chisel sources."""
        base = (self.root / self.subtree).resolve()
        rx = re.compile(pattern)
        frx = re.compile(file_pattern) if file_pattern else None
        rows: list[tuple] = []
        for f in sorted(base.rglob("*.scala")):
            rel = str(f.relative_to(base))
            if frx and not frx.search(rel):
                continue
            lines = f.read_text(errors="replace").splitlines()
            for i, t in enumerate(lines):
                if rx.search(t):
                    lo, hi = max(0, i - int(context)), min(len(lines), i + int(context) + 1)
                    for j in range(lo, hi):
                        rows.append((rel, j + 1, lines[j]))
                    if len(rows) >= int(limit):
                        break
            if len(rows) >= int(limit):
                break
        return self._wrap("search_source", ["file", "line", "text"],
                          rows[: int(limit)], len(rows) > int(limit))

    # --- the patch ----------------------------------------------------------

    @staticmethod
    def _nearest(src: str, old_text: str, k: int = 2) -> str:
        """The closest lines actually in the file, for an anchor that missed.

        "It does not match" leaves the agent to guess which character was wrong,
        and on the evidence it guesses once and gives up. Showing what IS there
        turns a dead end into an edit.
        """
        import difflib

        probe = old_text.strip().splitlines()[0] if old_text.strip() else ""
        if not probe:
            return ""
        lines = src.splitlines()
        best = difflib.get_close_matches(probe, [l.strip() for l in lines], n=k, cutoff=0.5)
        if not best:
            return ""
        out = []
        for b in best:
            for i, l in enumerate(lines, 1):
                if l.strip() == b:
                    out.append(f"  line {i}: {l}")
                    break
        return "\n\nClosest lines actually in the file:\n" + "\n".join(out)

    def propose_fix(self, path: str, old_text: str, new_text: str,
                    rationale: str = "") -> Result:
        """Register a one-hunk edit. Validated now; applied by the harness later.

        `old_text` must occur EXACTLY ONCE in the file. An ambiguous anchor is
        rejected rather than applied at the first match, because a patch that
        lands somewhere the agent did not mean is worse than no patch: it would
        be verified, and could even pass, while repairing nothing the task was
        about.
        """
        p = self._resolve(path)
        if not p.is_file():
            raise ValueError(f"no such file: {path}")
        src = p.read_text(errors="replace")
        n = src.count(old_text)
        if n == 0:
            self.rejected.append({"path": path, "old": old_text, "why": "no match"})
            raise ValueError(
                "old_text does not occur in that file. It must match the CURRENT "
                "source exactly, whitespace included -- read the lines first."
                + self._nearest(src, old_text))
        if n > 1:
            self.rejected.append({"path": path, "old": old_text, "why": f"{n} matches"})
            raise ValueError(
                f"old_text occurs {n} times; it must be unique. Include "
                "surrounding lines to disambiguate.")
        if old_text == new_text:
            self.rejected.append({"path": path, "old": old_text, "why": "no-op"})
            raise ValueError("old_text and new_text are identical: that is not an edit.")
        rel = str(p.relative_to(self.root))
        self.proposals.append({"path": rel, "old": old_text, "new": new_text,
                               "rationale": rationale})
        line = src[: src.index(old_text)].count("\n") + 1
        return self._wrap("propose_fix", ["status", "file", "line", "note"],
                          [("registered", rel, line,
                            "The harness will apply this, rebuild, and re-run every "
                            "stimulus under Spike lockstep. Only one proposal is "
                            "kept: a later one replaces this.")], False)


def source_declarations() -> list[dict]:
    S = lambda **kw: {"type": "STRING", **kw}
    I = lambda **kw: {"type": "INTEGER", **kw}
    return [
        {"name": "list_files",
         "description": "List the Chisel source files of the design under test.",
         "parameters": {"type": "OBJECT", "properties": {
             "pattern": S(description="optional regex on the path"), "limit": I()}}},
        {"name": "read_source",
         "description": "Read a line range of one Chisel file (1-based, inclusive).",
         "parameters": {"type": "OBJECT", "properties": {
             "path": S(description="e.g. 'exu/rob.scala'"), "start": I(), "count": I()},
             "required": ["path"]}},
        {"name": "search_source",
         "description": "Regex search the design's Chisel sources.",
         "parameters": {"type": "OBJECT", "properties": {
             "pattern": S(), "file_pattern": S(description="optional regex on the path"),
             "context": I(), "limit": I()}, "required": ["pattern"]}},
        {"name": "propose_fix",
         "description": "Propose the repair. old_text must match the current source "
                        "EXACTLY and occur exactly once; the harness applies it, "
                        "rebuilds the processor and re-runs every stimulus under Spike "
                        "lockstep co-simulation. Call this once you are confident.",
         "parameters": {"type": "OBJECT", "properties": {
             "path": S(), "old_text": S(), "new_text": S(), "rationale": S()},
             "required": ["path", "old_text", "new_text"]}},
    ]


def source_dispatch(view: SourceView, name: str, args: dict[str, Any]) -> str | None:
    """Returns rendered text, or None if `name` is not a source tool."""
    fn: Callable | None = {
        "list_files": view.list_files, "read_source": view.read_source,
        "search_source": view.search_source, "propose_fix": view.propose_fix,
    }.get(name)
    if fn is None:
        return None
    try:
        return fn(**args).render()
    except TypeError as e:
        return f"ERROR: bad arguments for {name}: {e}"
    except Exception as e:                                         # noqa: BLE001
        return f"ERROR: {type(e).__name__}: {e}"
