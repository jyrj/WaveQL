"""Selection, application and — above all — reversal of Chisel mutations.

The corpus generator edits a *live Chipyard checkout in place*. That is a
deliberate choice: Chipyard builds from the submodule path, so building a mutant
anywhere else would mean either copying a ten-gigabyte tree per mutant or
teaching the build a new source root, and both buy less than they cost.

Editing in place makes reversal a correctness property, not a nicety. Every
failure mode here corrupts the corpus silently:

* a crash between apply and revert leaves the next mutant built on top of the
  previous one, so two defects are present and the ground truth names one;
* a partial write leaves a file that does not compile, and the resulting build
  failure gets attributed to the mutation rather than to the harness;
* two concurrent mutants in one checkout produce a design neither record
  describes.

So: the workspace holds an exclusive lock on the checkout, keeps the original
bytes in memory *and* on disk, restores in a ``finally``, and then verifies the
restoration by hash before releasing. If it cannot restore, it raises loudly
rather than letting a poisoned tree be reused.
"""

from __future__ import annotations

import contextlib
import difflib
import fcntl
import hashlib
import json
import os
import random
import re
import signal
from collections import defaultdict
from contextlib import contextmanager
from functools import lru_cache
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterator, Sequence

from waveql.mutator.operators import MutationSite, enumerate_sites
from waveql.mutator.scala_lex import code_mask


class MutationError(RuntimeError):
    """Raised when the checkout could not be returned to its pristine state."""


@dataclass(frozen=True)
class MutationRecord:
    """The ground truth for one BuggyBOOM task.

    This is what the scorer compares an agent's blame against, and what a human
    reviews to confirm the task is fair. It therefore records the *semantic*
    location (module, file, line) separately from the textual edit, because an
    agent that names the right module and the wrong line is a partial success we
    want to be able to measure.
    """

    mutant_id: str
    path: str                  # repo-relative path of the mutated file
    line: int
    col: int
    operator: str
    mutation_class: str
    before: str
    after: str
    context: str               # the original source line
    module: str | None         # enclosing Chisel module, when we can name it
    diff: str                  # unified diff, the reviewable artifact
    sha256_before: str
    sha256_after: str
    seed: int
    generator_version: str = "0.1.0"

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _unified(path: str, before: str, after: str) -> str:
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
            n=3,
        )
    )


_DECL = re.compile(r"\b(?:abstract\s+)?(class|object|trait)\s+([A-Za-z_$][\w$]*)")

# What makes a Scala class an elaborated hardware module rather than a data type.
# Bundles are excluded on purpose: a Bundle becomes wires inside its parent, never
# a Verilog module, so blaming one could never be matched against a waveform scope.
_MODULE_BASES = frozenset({
    "Module", "MultiIOModule", "RawModule", "ImplicitModule",
    "LazyModule", "LazyModuleImp", "CoreModule",
    "BoomModule",
})
# Data-type bases. Reaching one of these terminates the walk with "not a module".
_BUNDLE_BASES = frozenset({
    "Bundle", "BoomBundle", "CoreBundle", "Record", "Data", "UInt", "SInt", "Bits",
})


@dataclass(frozen=True)
class Scope:
    name: str
    kind: str            # class | object | trait
    header: str          # declaration text up to the body brace
    start: int
    end: int             # offset just past the closing brace

    @property
    def base(self) -> str | None:
        """The immediate superclass named in ``extends``, unqualified.

        ``extends freechips.rocketchip.tile.CoreModule with HasFoo`` yields
        ``CoreModule``: the package path is dropped because the inheritance graph
        is resolved by simple name across the source set, and the trait list after
        ``with`` is not the superclass.
        """
        m = re.search(r"\bextends\s+([A-Za-z_$][\w$.]*)", self.header)
        if not m:
            return None
        return m.group(1).rsplit(".", 1)[-1]

    @property
    def is_module(self) -> bool:
        """Direct-evidence answer, used when no project-wide resolver is available."""
        b = self.base
        return b in _MODULE_BASES if b else False


def scopes(src: str, mask: bytearray | None = None) -> list[Scope]:
    """Every class/object/trait body in the file, with exact brace-delimited spans.

    Brace tracking rather than "nearest preceding declaration", because BOOM
    writes multi-line class headers:

        abstract class IssueUnit(
          val numIssueSlots: Int,
          ...
        )(implicit p: Parameters) extends BoomModule

    A line-anchored regex cannot see the ``extends`` from the ``class`` line, so
    it skips such a class entirely and attributes everything inside it to the
    previous single-line declaration -- which, in issue-unit.scala, is a Bundle
    fifty lines earlier. That is a wrong answer in the ground truth of a
    localization benchmark, i.e. the worst kind of bug this project can have.
    """
    if mask is None:
        mask = code_mask(src)
    out: list[Scope] = []
    for m in _DECL.finditer(src):
        if not mask[m.start()]:
            continue
        # The body opens at the next live-code '{'. Anything before it (parameter
        # lists, extends clause, self-type) is the header.
        i = m.end()
        depth_paren = 0
        body = None
        while i < len(src):
            if mask[i]:
                c = src[i]
                if c in "([":
                    depth_paren += 1
                elif c in ")]":
                    depth_paren -= 1
                elif c == "{" and depth_paren == 0:
                    body = i
                    break
                elif c == "\n" and depth_paren == 0 and _decl_ends_here(src, mask, i):
                    break          # brace-less declaration (e.g. `class X extends Y`)
            i += 1
        if body is None:
            continue
        depth = 0
        j = body
        while j < len(src):
            if mask[j]:
                if src[j] == "{":
                    depth += 1
                elif src[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
            j += 1
        out.append(Scope(m.group(2), m.group(1), src[m.start():body], m.start(), j + 1))
    return out


def _decl_ends_here(src: str, mask: bytearray, nl: int) -> bool:
    """True if a declaration without a body ends at this newline.

    Looks ahead to the next live-code token: if it starts another declaration or
    a ``val``/``def``, the previous declaration had no brace body.
    """
    k = nl + 1
    while k < len(src) and (src[k].isspace() or not mask[k]):
        k += 1
    return bool(_DECL.match(src, k)) or src.startswith(("val ", "def ", "}"), k)


class ModuleResolver:
    """Resolves whether a class name elaborates to a hardware module.

    Necessary because BOOM inherits deeply and the base is usually several hops
    away from the class being mutated:

        ALUUnit -> PipelinedFunctionalUnit -> FunctionalUnit -> BoomModule
        RenameStage -> AbstractRenameStage -> BoomModule

    Header-only matching attributes none of those to a module, which silently
    dropped 24% of BOOM's mutation sites from module-level ground truth. The
    inheritance edges are collected once across the whole source set and then
    walked transitively.

    Names that resolve to a Bundle base are *not* modules: a Bundle is wires
    inside its parent and never appears as a scope in a waveform, so blaming one
    could not be matched against the waveform hierarchy.
    """

    def __init__(self) -> None:
        self.base_of: dict[str, str] = {}
        self._memo: dict[str, bool] = {}

    def add_source(self, src: str) -> None:
        for s in scopes(src):
            b = s.base
            if b:
                self.base_of.setdefault(s.name, b)

    @classmethod
    def from_paths(cls, paths) -> "ModuleResolver":
        r = cls()
        for p in paths:
            r.add_source(Path(p).read_text())
        return r

    def is_module(self, name: str | None) -> bool:
        if not name:
            return False
        if name in self._memo:
            return self._memo[name]
        seen: set[str] = set()
        cur: str | None = name
        verdict = False
        while cur and cur not in seen:
            seen.add(cur)
            if cur in _MODULE_BASES:
                verdict = True
                break
            if cur in _BUNDLE_BASES:
                verdict = False
                break
            cur = self.base_of.get(cur)
        for n in seen:
            self._memo[n] = verdict
        return verdict


@lru_cache(maxsize=128)
def _scopes_cached(src: str) -> tuple[Scope, ...]:
    """Memoized :func:`scopes`.

    Corpus generation asks "which module encloses this offset?" once per mutation
    site, and BOOM yields ~4,800 sites over 58 files. Re-scanning a file per site
    made that quadratic: 30 s for BOOM, 40 s for Rocket. Keyed on the source text
    itself, so a mutated file is a different key and can never return a stale
    scope map -- which matters, because the scope map is what names the module in
    the ground-truth record.
    """
    return tuple(scopes(src))


def enclosing_scopes(src: str, offset: int, mask: bytearray | None = None) -> list[Scope]:
    """Scopes containing `offset`, outermost first."""
    found = scopes(src, mask) if mask is not None else _scopes_cached(src)
    return [s for s in found if s.start <= offset < s.end]


def enclosing_module(src: str, offset: int, resolver: "ModuleResolver | None" = None) -> str | None:
    """Name the Chisel *module* an offset falls inside, or None.

    The innermost enclosing module-like class is returned, so a mutation inside a
    Bundle declared within a Module is still blamed on the Module -- which is what
    a waveform scope will actually be named. When nothing module-like encloses the
    offset (a free-standing Bundle, a companion object), this returns None rather
    than naming a neighbour.
    """
    chain = enclosing_scopes(src, offset)
    for s in reversed(chain):
        if resolver.is_module(s.name) if resolver else s.is_module:
            return s.name
    return None


@lru_cache(maxsize=8)
def default_resolver(root: Path, subtree: str) -> ModuleResolver:
    """A ModuleResolver over a whole source subtree, built once and cached.

    Necessary because inheritance crosses files: ALUUnit extends
    PipelinedFunctionalUnit extends FunctionalUnit extends BoomModule, and only
    the last of those names a module base. A per-file resolver -- or, as was the
    case here, NO resolver -- reports None for every class in that chain, so the
    ground truth of a corpus task ends up with no module to score against.
    """
    r = ModuleResolver()
    base = root / subtree
    for f in sorted(base.rglob("*.scala")):
        r.add_source(f.read_text())
    return r


# Edits made INSIDE a mutation that the signal handler must also undo. A patch
# under verification is the case: verify_applied restores it in a `finally`, and
# a `finally` does not run on SIGTERM. Without this registry, stopping a verifier
# mid-build would leave the agent's patch in the worker whenever it touched a
# file other than the mutation's own, and every later build there would compile it.
_held_edits: dict[Path, str] = {}


@contextmanager
def held_edit(path: Path, original: str) -> Iterator[None]:
    """Register `path` for restoration to `original` if a signal ends the run."""
    path = Path(path).resolve()
    _held_edits[path] = original
    try:
        yield
    finally:
        _held_edits.pop(path, None)


def _restore_held_edits() -> None:
    for path, text in list(_held_edits.items()):
        with contextlib.suppress(Exception):
            _atomic_write(path, text)
        _held_edits.pop(path, None)


@contextmanager
def mutated(root: Path, site: MutationSite, *, seed: int = 0,
            resolver: "ModuleResolver | None" = None) -> Iterator[MutationRecord]:
    """Apply one mutation to `root`, yield its record, and always revert.

    ``root`` is the checkout root the site's ``path`` is relative to.
    """
    target = root / site.path
    # The lock lives inside .git, not in the worktree: a lock file in the tree
    # shows up as an untracked change in the very checkout whose cleanliness we
    # use to prove the revert worked.
    lock_path = (root / ".git" / "waveql-mutant.lock") if (root / ".git").is_dir() else (root / ".waveql-mutant.lock")
    original = target.read_text()
    if original[site.start : site.end] != site.before:
        raise MutationError(f"{site.site_id}: source no longer matches the site (stale enumeration?)")

    mutated_src = site.apply(original)
    backup = target.with_suffix(target.suffix + ".waveql-orig")

    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    # A `finally` does not run on SIGTERM, and stopping a campaign is an ordinary
    # thing to do. Without this the checkout is left MUTATED with the backup
    # beside it, and the next run either cannot re-derive its sites or -- worse --
    # builds a design carrying two defects.
    prev_handlers: dict[int, Any] = {}

    def _revert_and_die(signum, frame):                            # noqa: ANN001
        # Children first: a build still running would otherwise keep compiling
        # the mutated source after it had been reverted, orphaned.
        with contextlib.suppress(Exception):
            from waveql.harness import children
            children.kill_all(signum)
        try:
            _restore_held_edits()           # the patch first, then the mutation
            _atomic_write(target, original)
            backup.unlink(missing_ok=True)
        finally:
            with contextlib.suppress(Exception):
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
                os.close(lock_fd)
            h = prev_handlers.get(signum)
            signal.signal(signum, h if callable(h) else signal.SIG_DFL)
            os.kill(os.getpid(), signum)

    for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        with contextlib.suppress(ValueError, OSError):
            prev_handlers[sig] = signal.signal(sig, _revert_and_die)
    try:
        # Exclusive, so a second generator in the same checkout blocks instead of
        # interleaving two defects into one build.
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        backup.write_text(original)
        _atomic_write(target, mutated_src)

        rec = MutationRecord(
            mutant_id=f"{_sha(site.site_id)[:12]}",
            path=site.path,
            line=site.line,
            col=site.col,
            operator=site.operator,
            mutation_class=site.mutation_class,
            before=site.before,
            after=site.after,
            context=site.context,
            module=enclosing_module(original, site.start, resolver),
            diff=_unified(site.path, original, mutated_src),
            sha256_before=_sha(original),
            sha256_after=_sha(mutated_src),
            seed=seed,
        )
        yield rec
    finally:
        try:
            _atomic_write(target, original)
            restored = target.read_text()
            if _sha(restored) != _sha(original):
                raise MutationError(f"{site.path}: revert did not restore the original bytes")
            backup.unlink(missing_ok=True)
        finally:
            for sig, h in prev_handlers.items():
                with contextlib.suppress(ValueError, OSError):
                    signal.signal(sig, h if callable(h) else signal.SIG_DFL)
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file in the same directory, then rename.

    A torn write here would be attributed to the mutation, not to the harness.
    """
    tmp = path.with_suffix(path.suffix + ".waveql-tmp")
    tmp.write_text(text)
    os.replace(tmp, path)


def collect_sites(root: Path, targets: Sequence[str]) -> list[MutationSite]:
    """Every mutation site under the given paths, in deterministic order.

    A target may be a directory OR a single .scala file. Handling both is not a
    convenience: an earlier version only rglob'd directories, so naming
    `exu/rob.scala` as a target silently contributed ZERO sites -- and the
    targets most worth mutating (rob.scala, core.scala, decode.scala,
    regfile.scala) are exactly the ones named as files. A target that matches
    nothing now raises rather than quietly shrinking the corpus.
    """
    sites: list[MutationSite] = []
    for rel in targets:
        base = root / rel
        if base.is_dir():
            files = sorted(base.rglob("*.scala"))
        elif base.is_file():
            files = [base]
        else:
            raise FileNotFoundError(f"mutation target does not exist: {base}")
        if not files:
            raise FileNotFoundError(f"mutation target matched no .scala files: {base}")
        for f in files:
            sites.extend(enumerate_sites(str(f.relative_to(root)), f.read_text()))
    sites.sort(key=lambda s: (s.path, s.start, s.operator))
    return sites


def sample_class_balanced(
    sites: Sequence[MutationSite],
    per_class: int,
    seed: int,
    *,
    classes: Sequence[str] | None = None,
) -> list[MutationSite]:
    """Draw up to `per_class` sites from each mutation class.

    Balanced rather than proportional, and the reason is the whole experiment.
    Raw site counts in BOOM are wildly skewed -- ``&&``/``||`` alone supply
    roughly forty percent of all sites, while the handshake family supplies about
    two percent. A proportional sample would produce a corpus that is mostly
    boolean-operator flips, and a per-class breakdown would have no power exactly
    where the interesting failures live.

    Classes with fewer sites than the quota contribute everything they have; the
    shortfall is reported by the caller rather than silently backfilled from a
    richer class, because backfilling would re-skew the sample it exists to
    balance.
    """
    rng = random.Random(seed)
    buckets: dict[str, list[MutationSite]] = defaultdict(list)
    # Sort the POOL, not just the output. Sites arrive in filesystem iteration
    # order, which differs between machines and changes whenever the mutation
    # engine atomically rewrites a file. Sampling from an unsorted pool makes
    # the "same" seeded draw a different set of mutants on another machine.
    for s in sorted(sites, key=lambda s: (s.path, s.start, s.operator, s.after)):
        buckets[s.mutation_class].append(s)
    wanted = list(classes) if classes else sorted(buckets)
    out: list[MutationSite] = []
    for cls in wanted:
        pool = buckets.get(cls, [])
        out.extend(pool if len(pool) <= per_class else rng.sample(pool, per_class))
    out.sort(key=lambda s: (s.path, s.start, s.operator))
    return out
