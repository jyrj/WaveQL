#!/usr/bin/env python3
"""Before the numbers are frozen: is every cell accounted for, exactly once?

  - every proposal queued for verification has a verdict (none stranded by a
    dead verifier or an orphaned claim);
  - no (task, arm, seed) cell is scored twice by different episodes of the
    same run (a re-run after a crash must not add a second episode);
  - every killed task of every draw has episodes in both arms, or is listed;
  - lost/errored cells are counted, so the paper can say how many.
Exit 1 if anything is unaccounted for.
"""
import collections, glob, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
M = ROOT / "measurements"


def rows(pat):
    for f in sorted(M.glob(pat)):
        for l in f.read_text().splitlines():
            if l.strip():
                r = json.loads(l); r["_file"] = f.name
                yield r


def main() -> int:
    bad = 0
    for q in sorted(M.glob("proposals-*.jsonl")):
        tag = q.name[len("proposals-"):-len(".jsonl")]
        eps = list(rows(f"episodes-{tag}.jsonl")) + list(rows(f"episodes-{tag}-*.jsonl"))
        queued = {(r["task_id"], r["arm"], int(r["seed"])) for r in rows(q.name)}
        final = collections.Counter((r["task_id"], r["arm"], int(r["seed"])) for r in eps)
        stranded = sorted(queued - set(final))
        doubled = [k for k, n in final.items() if n > 1]
        lost = sum(1 for r in eps if r.get("verdict") == "lost" or r.get("stop") == "error")
        print(f"{tag:20s} queued {len(queued):4d}  final cells {len(final):4d}  "
              f"stranded {len(stranded):3d}  doubled {len(doubled):3d}  lost {lost:3d}")
        for k in stranded[:10]:
            print(f"   STRANDED {k[0][:12]} {k[1]} s{k[2]}")
        for k in doubled[:10]:
            print(f"   DOUBLED  {k[0][:12]} {k[1]} s{k[2]}")
        bad += len(stranded) + len(doubled)
    # every killed task has both arms
    killed = set()
    for f in sorted((ROOT / "corpus").glob("*.jsonl")):
        last = {}
        for l in f.read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                if r.get("rescreen") or not last.get(r["mutant_id"], {}).get("rescreen"):
                    last[r["mutant_id"]] = r
        killed |= {m for m, r in last.items()
                   if r.get("verdict") == "killed" and r.get("kill_kind") in ("divergence", "assertion")}
    arms = collections.defaultdict(set)
    for r in rows("episodes-*.jsonl"):
        if r.get("verdict") != "lost" and r.get("stop") != "error":
            arms[r["task_id"]].add(r["arm"])
    one = sorted(t for t in killed if len(arms.get(t, ())) == 1)
    none = sorted(t for t in killed if not arms.get(t))
    print(f"killed tasks {len(killed)}: both arms {sum(1 for t in killed if len(arms.get(t, ())) == 2)}, "
          f"one arm {len(one)}, none {len(none)}")
    for t in one[:10]:
        print(f"   ONE ARM {t[:12]} {sorted(arms[t])}")
    for t in none[:10]:
        print(f"   NO EPISODES {t[:12]}")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
