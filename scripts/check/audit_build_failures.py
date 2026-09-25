#!/usr/bin/env python3
"""Is every build-failed verdict the patch's own fault?

A patch that does not compile fails in the file it edited. A compile error in
any OTHER file means the worker was carrying something that was not this patch
-- another cell's leftover edit, or a mutation that was never reverted -- and the
verdict belongs to that residue, not to the agent.

Reads the verify work directories (corpus/fix/<task>-<arm>-s<seed>/) and the
verdict rows; prints one line per build failure and a CONTAMINATED list.
"""
import argparse, glob, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ANSI = re.compile(r"\x1b\[[0-9;]*m")
SCALA_ERR = re.compile(r"\[error\]\s+(/\S+?\.scala):\d+:\d+:")
VLOG_ERR = re.compile(r"%Error[^:]*:\s*(\S+?):\d+")
# firtool reports against the Chisel locator, e.g. a patch that closes a
# combinational loop: "…/rob.scala:215:7: error: detected combinational cycle".
FIRTOOL_ERR = re.compile(r"^(\S+?\.scala):\d+:\d+: error:")
# An elaboration failure (a patch that compiles but breaks Chisel's construction,
# e.g. an initialization-order NullPointerException) has no compiler error line;
# its first BOOM stack frame names the file instead.
BOOM_FRAME = re.compile(r"\s+at boom\.\S+\((\S+?\.scala):\d+\)")


def first_error_files(log: Path) -> list[str]:
    files, frame_seen = [], False
    for line in log.read_text(errors="replace").splitlines():
        line = ANSI.sub("", line)
        m = SCALA_ERR.search(line) or VLOG_ERR.search(line) or FIRTOOL_ERR.match(line)
        if not m and not frame_seen:
            # Only the FIRST BOOM frame locates the fault; the frames below it
            # are its callers (tile.scala instantiating the D-cache, and so on).
            m = BOOM_FRAME.match(line)
            frame_seen = bool(m)
        if m and m.group(1) not in files:
            files.append(m.group(1))
    return files


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--rows", nargs="+", default=["measurements/episodes-*.jsonl"])
    a = ap.parse_args()
    rows = []
    for g in a.rows:
        for f in sorted(ROOT.glob(g)):
            for l in f.read_text().splitlines():
                if l.strip():
                    r = json.loads(l); r["_file"] = f.name; rows.append(r)
    bad, n = [], 0
    for r in rows:
        if r.get("verdict") != "build-failed":
            continue
        n += 1
        work = ROOT / "corpus" / "fix" / f"{r['task_id']}-{r['arm']}-s{r['seed']}"
        prop = (r.get("proposal") or {}).get("path", "")
        log = work / "build.log"
        if not log.is_file():
            print(f"  {r['task_id'][:8]} {r['arm']:8s} s{r['seed']} NO LOG ({r['_file']})")
            continue
        errs = first_error_files(log)
        same = lambda e: prop and (e.endswith(prop) or Path(e).name == Path(prop).name)
        own = [e for e in errs if same(e)]
        other = [e for e in errs if not same(e)]
        tag = "own" if own and not other else ("CONTAMINATED" if other else "no-error-line")
        if tag != "own":
            bad.append((r, errs))
        print(f"  {r['task_id'][:8]} {r['arm']:8s} s{r['seed']} {tag:14s} patch={Path(prop).name:26s} "
              f"errors_in={[Path(e).name for e in errs][:3]} ({r['_file']})")
    print(f"\n{n} build failure(s); {len(bad)} not attributable to the patch itself")
    for r, errs in bad:
        print(f"  SUSPECT {r['task_id'][:8]} {r['arm']} s{r['seed']} {r['_file']} {errs[:2]}")
    return 0 if not bad else 1


if __name__ == "__main__":
    raise SystemExit(main())
