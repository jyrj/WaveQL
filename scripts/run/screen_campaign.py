#!/usr/bin/env python3
"""Screen a class-balanced sample of BOOM mutants. Resumable; safe to re-run."""
import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from waveql.corpus.campaign import DEFAULT_STIMULI, run_campaign          # noqa: E402
from waveql.harness.chipyard import ChipyardEnv                            # noqa: E402
from waveql.mutator.operators import CLASSES                               # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--per-class", type=int, default=5)
    ap.add_argument("--seed", type=int, default=20260904)
    ap.add_argument("--jobs", type=int, default=20)
    ap.add_argument("--config", default="WaveQLMediumBoomV3Config")
    ap.add_argument("--out", default=str(ROOT / "corpus" / "screen.jsonl"))
    ap.add_argument("--target", action="append",
                    default=None, help="repo-relative source dir (repeatable)")
    ap.add_argument("--stimulus", action="append", default=None)
    ap.add_argument("--classes", nargs="*", default=list(CLASSES))
    ap.add_argument("--skip-baseline", action="store_true")
    ap.add_argument("--chipyard", default=None,
                    help="checkout to mutate (default: thirdparty/chipyard)")
    ap.add_argument("--shard", default=None, metavar="I/N",
                    help="screen only shard I of N (0-based), round-robin")
    ap.add_argument("--run-workers", type=int, default=8,
                    help="concurrent stimulus simulators")
    ap.add_argument("--exclude-screened", action="append", default=[],
                    metavar="GLOB", help="skip mutants already in these manifests "
                    "(a disjoint second draw); repo-relative glob, repeatable")
    a = ap.parse_args()
    import json
    seen: set[str] = set()
    for g in a.exclude_screened:
        for m in sorted(ROOT.glob(g)):
            for line in m.read_text().splitlines():
                if line.strip():
                    seen.add(json.loads(line)["mutant_id"])
    if a.exclude_screened:
        print(f"[campaign] {len(seen)} mutant id(s) from earlier draws", flush=True)

    cy = ChipyardEnv.load(a.chipyard or (ROOT / "thirdparty" / "chipyard"))
    shard = None
    if a.shard:
        i, n = a.shard.split("/")
        shard = (int(i), int(n))
    run_campaign(
        cy, Path(a.out),
        targets=a.target,   # None -> the architectural target list
        per_class=a.per_class, seed=a.seed, config=a.config,
        stimuli=a.stimulus or DEFAULT_STIMULI, jobs=a.jobs,
        classes=a.classes, skip_baseline=a.skip_baseline,
        shard=shard, run_workers=a.run_workers, exclude_ids=frozenset(seen),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
