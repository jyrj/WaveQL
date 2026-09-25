#!/usr/bin/env python3
"""Verified repair rate by arm, paired by task.

PAIRED BY TASK. Tasks differ enormously in difficulty -- one mutant breaks all
twelve programs, another exactly one -- so an unpaired comparison mostly measures
which tasks landed in which arm. Only tasks where both arms ran are counted, and
the statistic is the mean per-task difference with a bootstrap interval.

REPEATED CELLS AVERAGE FIRST. Temperature 0 does not make episodes reproducible:
the same (task, arm, seed) can both repair and fail. A cell's episodes are
averaged into a rate before pairing, so a task with three episodes does not
outvote a task with one.

LOST EPISODES ARE EXCLUDED, not scored as failures: an episode killed by a
transport error never reached a verdict.
"""
import argparse, collections, json, re, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]; sys.path.insert(0, str(ROOT / "src"))
from waveql.analysis.metrics import bootstrap_ci                   # noqa: E402

BAND = 0.10          # differences smaller than this are treated as no effect


_OVERLAP = None


def overlap_excluded(task_id: str | None, fname: str) -> bool:
    """A later draw's row for a mutant an earlier draw already screened.

    The class-balanced sampler drew from a pool in filesystem order, so the
    exclusion list for draw 3, recomputed on another machine, missed most of
    draw 2 and six mutants were screened twice. The first screening stands;
    every later-draw row for those mutants (screen, episodes, verdicts,
    localization) is dropped. measurements/draw-overlap.json lists them.
    """
    global _OVERLAP
    if _OVERLAP is None:
        f = ROOT / "measurements" / "draw-overlap.json"
        d = json.loads(f.read_text()).get("later_draw_duplicates", {}) if f.is_file() else {}
        _OVERLAP = {k: set(v) for k, v in d.items()}
    tokens = set(re.split(r"[-_.]", fname))
    return any(task_id in ids and tag in tokens for tag, ids in _OVERLAP.items())


def load(patterns: list[str]) -> list[dict]:
    rows = []
    for pat in patterns:
        for p in sorted(ROOT.glob(pat)):
            for line in p.read_text().splitlines():
                if line.strip():
                    r = json.loads(line)
                    if not overlap_excluded(r.get("task_id"), p.name):
                        rows.append(r)
    return rows


def compute(patterns: list[str]) -> dict:
    """Repair rates, their paired difference and token costs, as data, so the
    paper, the showcase page and this script cannot disagree."""
    rows = load(patterns)
    lost = [r for r in rows if r.get("stop") == "error" or r.get("verdict") == "lost"]
    rows = [r for r in rows if r.get("stop") != "error" and r.get("verdict") != "lost"]
    # Two machines verifying the same agent episode yield two rows for ONE
    # episode; keep the first. The key is the episode's identity (its token,
    # query and time counts), NOT (task, arm, seed): separate episodes that share
    # a seed label are averaged like any repeated cell.
    seen, uniq = set(), []
    for r in rows:
        k = (r["task_id"], r["arm"], int(r.get("seed", 0)), r.get("tokens"),
             r.get("queries"), r.get("episode_seconds"))
        if k not in seen:
            seen.add(k); uniq.append(r)
    dup = len(rows) - len(uniq); rows = uniq
    cells = collections.defaultdict(list)
    for r in rows:
        cells[(r["task_id"], r["arm"])].append(r)
    tasks = sorted({t for t, _ in cells})
    paired = [t for t in tasks if (t, "waveql") in cells and (t, "control") in cells]
    fx = lambda t, a: (sum(1 for e in cells[(t, a)] if e["verdict"] == "fixed")
                       / len(cells[(t, a)]))
    wl = [fx(t, "waveql") for t in paired]
    cl = [fx(t, "control") for t in paired]
    diffs = [w - c for w, c in zip(wl, cl)]
    out = {"episodes": len(rows), "lost": len(lost), "duplicates": dup, "tasks": len(tasks),
           "paired": len(paired), "per_task": [
               {"task": t[:8], "waveql": w, "control": c,
                "n_w": len(cells[(t, "waveql")]), "n_c": len(cells[(t, "control")])}
               for t, w, c in zip(paired, wl, cl)]}
    if not diffs:
        out["verdict"] = "no paired tasks"
        return out
    mw, mc, md = sum(wl) / len(wl), sum(cl) / len(cl), sum(diffs) / len(diffs)
    wlo, whi = bootstrap_ci(wl); clo, chi = bootstrap_ci(cl); lo, hi = bootstrap_ci(diffs)
    out.update(waveql=mw, control=mc, diff=md, diff_ci=[lo, hi],
               waveql_ci=[wlo, whi], control_ci=[clo, chi], band=BAND,
               fixed_waveql=sum(1 for r in rows if r["arm"] == "waveql" and r["verdict"] == "fixed"),
               fixed_control=sum(1 for r in rows if r["arm"] == "control" and r["verdict"] == "fixed"),
               n_waveql=sum(1 for r in rows if r["arm"] == "waveql"),
               n_control=sum(1 for r in rows if r["arm"] == "control"))

    # Localization: the proposed patch landing in the file that holds the defect,
    # on the same paired tasks, averaged per cell the same way.
    loc = lambda t, a: (sum(1 for e in cells[(t, a)] if e.get("same_file_as_bug"))
                        / len(cells[(t, a)]))
    lw = [loc(t, "waveql") for t in paired]
    lc = [loc(t, "control") for t in paired]
    ldiff = [w - c for w, c in zip(lw, lc)]
    llo, lhi = bootstrap_ci(ldiff)
    out.update(loc_waveql=sum(lw) / len(lw), loc_control=sum(lc) / len(lc),
               loc_diff=sum(ldiff) / len(ldiff), loc_diff_ci=[llo, lhi])

    # Token cost per episode and per verified repair.
    for arm in ("waveql", "control"):
        a_rows = [r for r in rows if r["arm"] == arm]
        fixes = sum(1 for r in a_rows if r["verdict"] == "fixed")
        toks = sum(r.get("tokens", 0) for r in a_rows)
        out[f"tokens_per_episode_{arm}"] = toks / len(a_rows) if a_rows else None
        out[f"tokens_per_fix_{arm}"] = toks / fixes if fixes else None
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--episodes", nargs="+", default=["measurements/episodes-*.jsonl"])
    a = ap.parse_args()
    r = compute(a.episodes)
    print(f"{r['episodes']} scored episodes ({r['lost']} lost to transport, excluded), "
          f"{r['paired']} tasks with both arms")
    if "diff" not in r:
        return 0
    pc = lambda x: f"{x * 100:5.1f}%"
    print(f"verified repair rate  WaveQL  {pc(r['waveql'])}  [{pc(r['waveql_ci'][0])}, {pc(r['waveql_ci'][1])}]")
    print(f"verified repair rate  control {pc(r['control'])}  [{pc(r['control_ci'][0])}, {pc(r['control_ci'][1])}]")
    print(f"paired difference     {r['diff'] * 100:+.1f} points  "
          f"[{r['diff_ci'][0] * 100:+.1f}, {r['diff_ci'][1] * 100:+.1f}]")
    tw, tc = r["tokens_per_episode_waveql"], r["tokens_per_episode_control"]
    fw, fc = r["tokens_per_fix_waveql"], r["tokens_per_fix_control"]
    print(f"tokens per episode    WaveQL {tw:,.0f}  control {tc:,.0f}  ({tw / tc:.2f}x)")
    if fw and fc:
        print(f"tokens per repair     WaveQL {fw:,.0f}  control {fc:,.0f}  ({fw / fc:.2f}x)")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
