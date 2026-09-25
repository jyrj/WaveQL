#!/usr/bin/env python3
"""Does first_divergence's trace name the file the defect is in?

Not a ranking over all files -- nine of those have been tried and none worked.
This asks a narrower question with a definite answer: of the Chisel files the
causal chain actually names, is the buggy one among them, and how many are there?
A chain that names four files including the right one is useful to an agent in a
way that a 48-file ranking is not.
"""
import argparse, json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from waveql.corpus.task import load_tasks                          # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--manifests", default="corpus/*.jsonl",
                    help="repo-relative glob; the held-out test is the second draw only")
    ap.add_argument("--out", default="measurements/localization.jsonl")
    a = ap.parse_args()
    gen = ROOT / "var" / "netlist-snapshot"
    recap = {}
    rp = ROOT / "measurements" / "recapture.jsonl"
    if rp.is_file():
        recap = {json.loads(l)["mutant_id"]: json.loads(l)
                 for l in rp.read_text().splitlines() if l.strip()}
    tasks = load_tasks(sorted(ROOT.glob(a.manifests)), ROOT / "corpus" / "work")
    hit = tot = 0
    out = []
    for t in tasks:
        r = recap.get(t.task_id)
        if r and Path(r["vcd_path"]).is_file():
            t.vcd_path = Path(r["vcd_path"])
        try:
            tool = t.waveql_store(gen_src=gen)
        except Exception as e:                                     # noqa: BLE001
            print(f"  {t.task_id[:8]} store failed: {type(e).__name__}"); continue
        try:
            rows = (tool.first_divergence().rows if t.kill_kind == "divergence"
                    else tool.stall_report(limit=200).rows)
        except Exception as e:                                     # noqa: BLE001
            # A mutant that asserts before its first commit leaves no dump and
            # no commits; the tool says so. The chain then names no file: a
            # miss, scored as one rather than dropped from the test set.
            print(f"  {t.task_id[:8]} no chain: {type(e).__name__}")
            rows = []
        named, mods = [], []
        for w in rows:
            if str(w[0]) not in ("trace", "cause"):
                continue
            txt = " ".join(str(x) for x in w if x)
            for part in txt.split():
                if part.endswith(".scala") or ".scala:" in part:
                    f = part.split(":")[0].split("/")[-1]
                    if f not in named:
                        named.append(f)
            sig = str(w[1])
            if sig.startswith("d") and " " in sig:
                m = sig.split(" ", 1)[1].split(".")[0]
                if m not in mods:
                    mods.append(m)
        bug = Path(t.true_path).name
        ok = bug in named
        hit += ok; tot += 1
        out.append({"task_id": t.task_id, "kind": t.kill_kind, "bug": bug,
                    "named": named, "modules": mods, "hit": ok})
        print(f"  {t.task_id[:8]} {t.kill_kind:10s} {bug[:26]:28s} "
              f"{'HIT ' if ok else 'miss'} names={len(named):2d} "
              f"{[n.split('.scala')[0][:14] for n in named[:5]]}", flush=True)
    print(f"\nbug file named by the chain: {hit}/{tot}")
    (ROOT / a.out).write_text(
        "".join(json.dumps(x) + "\n" for x in out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
