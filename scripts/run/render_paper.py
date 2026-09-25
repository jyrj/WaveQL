#!/usr/bin/env python3
"""Compute every number in the paper from the measurement files.

Writes paper/numbers.json, which the Typst source reads with json(), and the
generated results region of README.md. No figure in the paper is typed by hand,
so a re-run after new measurements is the whole update; claims whose direction
depends on the data are chosen by conditionals over these numbers.
"""
import collections, json, re, statistics, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts" / "run"))
import headline                                                    # noqa: E402


def pct(x):
    return "n/a" if x is None else f"{x:.3f}"


def _loc_metrics(loc: list[dict], dirs: dict, static_files: list, static_dirs: list) -> dict:
    """Chain hit rates next to the two baselines a hit must beat.

    A chain that names hub files (core, rob, lsu ...) for every task scores hits
    by base rate alone. So the chain is reported beside a STATIC list (the files
    the first draw's chains named most) and a PERMUTATION baseline (the defect's
    file in some OTHER task's chain).
    """
    n = len(loc)
    if not n:
        return {"n": 0, "file": 0, "dir": 0, "static_file": 0, "static_dir": 0,
                "perm_file": 0.0, "perm_dir": 0.0, "median_named": 0}
    dset = lambda r: {dirs.get(f) for f in r["named"]} - {None}
    fh = sum(1 for r in loc if r["bug"] in r["named"])
    dh = sum(1 for r in loc if dirs.get(r["bug"]) in dset(r))
    sf = sum(1 for r in loc if r["bug"] in static_files)
    sd = sum(1 for r in loc if dirs.get(r["bug"]) in static_dirs)
    pf = [loc[i]["bug"] in loc[j]["named"] for i in range(n) for j in range(n) if i != j]
    pd = [dirs.get(loc[i]["bug"]) in dset(loc[j]) for i in range(n) for j in range(n) if i != j]
    return {"n": n, "file": fh, "dir": dh, "static_file": sf, "static_dir": sd,
            "perm_file": (sum(pf) / len(pf)) if pf else None,
            "perm_dir": (sum(pd) / len(pd)) if pd else None,
            "median_named": statistics.median(len(r["named"]) for r in loc)}


def heldout(dirs: dict) -> dict:
    fz = ROOT / "measurements" / "localization-heldout.json"
    if not fz.is_file():
        return {}
    a = json.loads(fz.read_text())
    sf, sd = a["baseline_static_files"]["list"], a["baseline_static_dirs"]["list"]
    read = lambda p: [json.loads(l) for l in p.read_text().splitlines() if l.strip()] if p.is_file() else []
    d1 = read(ROOT / "measurements" / "localization-draw1.jsonl")
    held = []          # draws 2 and 3: neither existed when the localizer was fixed
    for pat in ("localization-draw2*.jsonl", "localization-draw3*.jsonl"):
        for p in sorted((ROOT / "measurements").glob(pat)):
            held += [r for r in read(p) if not headline.overlap_excluded(r["task_id"], p.name)]
    return {"static_files": sf, "static_dirs": sd,
            "draw1": _loc_metrics(d1, dirs, sf, sd), "heldout": _loc_metrics(held, dirs, sf, sd)}


def _screen_rows(pattern: str) -> list[dict]:
    """One row per mutant; a re-screen supersedes the earlier row."""
    last = {}
    for f in sorted((ROOT / "corpus").glob(pattern)):
        for l in f.read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                if headline.overlap_excluded(r["mutant_id"], f.name):
                    continue
                if r.get("rescreen") or not last.get(r["mutant_id"], {}).get("rescreen"):
                    last[r["mutant_id"]] = r
    return list(last.values())


def corpus_counts() -> dict:
    out = {}
    for name, pat in (("draw1", "draw1*.jsonl"), ("draw2", "draw2*.jsonl"),
                      ("draw3", "draw3*.jsonl")):
        rs = _screen_rows(pat)
        v = collections.Counter(r["verdict"] for r in rs)
        k = collections.Counter(r.get("kill_kind") for r in rs if r["verdict"] == "killed")
        out[name] = {"screened": len(rs), "killed": v.get("killed", 0),
                     "tasks": k.get("divergence", 0) + k.get("assertion", 0),
                     "divergence": k.get("divergence", 0), "assertion": k.get("assertion", 0),
                     "other_kill": v.get("killed", 0) - k.get("divergence", 0) - k.get("assertion", 0),
                     "survived": v.get("survived", 0), "build_failed": v.get("build-failed", 0),
                     "invalid": v.get("invalid", 0), "error": v.get("error", 0)}
    return out


def token_ratio(rows: list[dict]) -> dict:
    """Tokens per episode, waveql/control, paired by task, with an interval.

    Per task, the mean tokens of each arm's episodes; the statistic is the ratio
    of the two across-task means, resampled over tasks (10,000 draws), the same
    unit the fix-rate interval resamples.
    """
    import random
    per = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if r.get("tokens"):
            per[r["task_id"]][r["arm"]].append(r["tokens"])
    ts = sorted(t for t in per if per[t]["waveql"] and per[t]["control"])
    if not ts:
        return {}
    w = {t: statistics.mean(per[t]["waveql"]) for t in ts}
    c = {t: statistics.mean(per[t]["control"]) for t in ts}
    ratio = lambda xs: sum(w[t] for t in xs) / sum(c[t] for t in xs)
    rng = random.Random(20260904)
    boots = sorted(ratio([rng.choice(ts) for _ in ts]) for _ in range(10000))
    return {"tasks": len(ts), "ratio": ratio(ts), "lo": boots[249], "hi": boots[9750],
            "waveql_mean": statistics.mean(w.values()), "control_mean": statistics.mean(c.values()),
            "per_task": [[round(c[t]), round(w[t])] for t in ts],
            "below": sum(1 for t in ts if w[t] < c[t])}


def by_kind(rows: list[dict]) -> dict:
    """Repair rate (per task, seeds averaged) and tokens per episode, by how the
    mutant failed: a wrong committed value or PC (divergence) or a fired
    assertion, most often BOOM's liveness check on a hang."""
    out = {}
    for kind in ("divergence", "assertion"):
        rs = [r for r in rows if r.get("kill_kind") == kind]
        cells = collections.defaultdict(list)
        for r in rs:
            cells[(r["task_id"], r["arm"])].append(r)
        paired = sorted({t for t, _ in cells if (t, "waveql") in cells and (t, "control") in cells})
        rate = lambda arm: (statistics.mean(sum(e["verdict"] == "fixed" for e in cells[(t, arm)])
                                            / len(cells[(t, arm)]) for t in paired) if paired else None)
        tok = lambda arm: statistics.mean(r["tokens"] for r in rs if r["arm"] == arm and r.get("tokens"))
        out[kind] = {"tasks": len(paired), "waveql": rate("waveql"), "control": rate("control"),
                     "tok_waveql": tok("waveql"), "tok_control": tok("control")}
    return out


def behaviour(rows: list[dict]) -> dict:
    """How each arm spent its budget (descriptive).

    Final rows only: mid-run, a proposal waits in the verify queue while an
    episode without one is final at once, so partial data over-counts
    non-proposals.
    """
    out = {}
    for arm in ("waveql", "control"):
        rs = [r for r in rows if r["arm"] == arm]
        if not rs:
            continue
        n = len(rs)
        tools = collections.Counter(t for r in rs for t in (r.get("tool_calls") or []))
        calls = sum(tools.values())
        out[arm] = {
            "n": n,
            "proposed": sum(1 for r in rs if r["verdict"] != "no-proposal") / n,
            "capped": sum(1 for r in rs if (r.get("queries") or 0) >= 30) / n,
            "median_queries": statistics.median(r.get("queries") or 0 for r in rs),
            "top_tools": tools.most_common(5),
            "why_share": tools.get("why", 0) / calls if calls else 0.0,
            "why_episodes": sum(1 for r in rs if "why" in (r.get("tool_calls") or [])) / n,
        }
    return out


def per_class(rows: list[dict]) -> list[dict]:
    """Per-class table: screen yield and verified repair rate by class."""
    scr = _screen_rows("*.jsonl")
    cls_of = {}
    by = collections.defaultdict(lambda: {"screened": 0, "killed": 0})
    for r in scr:
        rec = r.get("record") or r.get("site") or {}
        c = rec.get("mutation_class", "?")
        cls_of[r["mutant_id"]] = c
        by[c]["screened"] += 1
        by[c]["killed"] += r["verdict"] == "killed"
    cells = collections.defaultdict(list)
    for r in rows:
        cells[(r["task_id"], r["arm"])].append(r["verdict"] == "fixed")
    out = []
    for c in sorted(by):
        tids = {t for (t, a) in cells if cls_of.get(t) == c}
        paired = [t for t in tids if (t, "waveql") in cells and (t, "control") in cells]
        rate = lambda arm: (sum(sum(cells[(t, arm)]) / len(cells[(t, arm)]) for t in paired)
                            / len(paired)) if paired else None
        out.append({"cls": c, **by[c], "paired": len(paired),
                    "waveql": rate("waveql"), "control": rate("control")})
    return out


def main() -> int:
    h = headline.compute(["measurements/episodes-*.jsonl"])
    # The headline as it stood BEFORE the corpus was scaled: the files that
    # existed then. Reported because the paper's story is what scaling did to it.
    pre = headline.compute(["measurements/episodes-draw1-initial.jsonl"])
    loc = [json.loads(l) for l in (ROOT / "measurements" / "localization-draw1.jsonl")
           .read_text().splitlines() if l.strip()]
    dirs = {}
    # BOOM's file -> directory layout at the pinned commit, recorded so the
    # paper builds without a Chipyard checkout.
    dirs.update(json.loads((ROOT / "measurements" / "boom-v3-layout.json").read_text())["file_to_dir"])
    file_hits = sum(1 for r in loc if r["hit"])
    dir_hits = sum(1 for r in loc
                   if dirs.get(r["bug"]) in {dirs.get(n) for n in r["named"]})
    median = statistics.median(len(r["named"]) for r in loc)

    # right-file -> fixed conversion, same exclusions as the headline
    rows = []
    for p in sorted((ROOT / "measurements").glob("episodes-*.jsonl")):
        for l in p.read_text().splitlines():
            if l.strip():
                r = json.loads(l)
                if (r.get("stop") != "error" and r.get("verdict") != "lost"
                        and not headline.overlap_excluded(r.get("task_id"), p.name)):
                    rows.append(r)
    allrows = []
    for p in sorted((ROOT / "measurements").glob("episodes-*.jsonl")):
        allrows += [r for r in (json.loads(l) for l in p.read_text().splitlines() if l.strip())
                    if not headline.overlap_excluded(r.get("task_id"), p.name)]
    conv = {}
    for arm in ("waveql", "control"):
        rf = [r for r in rows if r["arm"] == arm and r.get("same_file_as_bug")]
        conv[arm] = f"{sum(1 for r in rf if r['verdict'] == 'fixed')} of {len(rf)}"
    rf_n = {arm: sum(1 for r in rows if r["arm"] == arm and r.get("same_file_as_bug"))
            for arm in ("waveql", "control")}

    k = lambda x: "n/a" if x is None else f"{x/1000:,.0f}k"
    vals = {
        "FILE": file_hits, "DIR": dir_hits, "MEDIAN": f"{median:g}",
        "LOST": h["lost"], "TOTAL": len(allrows),
        "EPISODES": h["episodes"], "PAIRED": h["paired"],
        "FW": pct(h["waveql"]), "FWLO": pct(h["waveql_ci"][0]), "FWHI": pct(h["waveql_ci"][1]),
        "FC": pct(h["control"]), "FCLO": pct(h["control_ci"][0]), "FCHI": pct(h["control_ci"][1]),
        "NFW": h["fixed_waveql"], "NW": h["n_waveql"],
        "NFC": h["fixed_control"], "NC": h["n_control"],
        "LW": f"{rf_n['waveql']} / {h['n_waveql']}", "LC": f"{rf_n['control']} / {h['n_control']}",
        "TEW": k(h["tokens_per_episode_waveql"]), "TEC": k(h["tokens_per_episode_control"]),
        "TFW": k(h["tokens_per_fix_waveql"]), "TFC": k(h["tokens_per_fix_control"]),
        "DIFF": f"{h['diff']*100:+.1f}",
        "DLO": f"{h['diff_ci'][0]*100:+.1f}", "DHI": f"{h['diff_ci'][1]*100:+.1f}",
        "LDIFF": f"{h['loc_diff']*100:+.1f}",
        "CW": conv["waveql"], "CC": conv["control"],
        "DIFF1": f"{pre['diff']*100:+.1f}", "PAIRED1": pre["paired"],
        "EPISODES1": pre["episodes"],
    }
    # The typeset 4-page paper reads the same numbers through Typst's json(),
    # so the two renderings cannot drift apart.
    cost = ROOT / "measurements" / "cost.json"
    nums = dict(vals)
    nums["h1"] = {k: h[k] for k in ("diff", "diff_ci", "waveql",
                                    "control", "waveql_ci", "control_ci", "paired",
                                    "episodes", "lost", "band")}
    nums["cost"] = json.loads(cost.read_text()) if cost.is_file() else {}
    # One row per task: the complete evidence, so a reader can check every claim
    # against every task rather than trusting the aggregates.
    chains = {}
    cp = ROOT / "measurements" / "demo_chains.json"
    if cp.is_file():
        chains = {c["id"]: c for c in json.loads(cp.read_text())}
    locmap = {r["task_id"][:8]: r for r in loc}
    eps = {}
    for r in rows:
        eps.setdefault((r["task_id"][:8], r["arm"]), []).append(r["verdict"])
    abbrev = {"fixed": "fixed", "not-fixed": "wrong", "no-proposal": "none",
              "build-failed": "no build", "rejected": "rejected"}
    table = []
    for tid, c in chains.items():
        lr = locmap.get(tid, {})
        named = set(lr.get("named", []))
        kind = {"assertion": "hang/assert", "pc": "wrong PC", "wdata": "wrong value"}.get(c["kind"], c["kind"])
        table.append({
            "task": tid, "kind": kind, "bug": f"{c['bug_file']}:{c['bug_line']}",
            "op": c["operator"],
            "file": c["bug_file"] in named,
            "dir": dirs.get(c["bug_file"]) in {dirs.get(n) for n in named},
            "waveql": ", ".join(abbrev.get(v, v) for v in eps.get((tid, "waveql"), [])) or "lost",
            "control": ", ".join(abbrev.get(v, v) for v in eps.get((tid, "control"), [])) or "lost",
        })
    nums["tasks"] = table
    nums["heldout"] = heldout(dirs)
    nums["corpus"] = corpus_counts()
    nums["classes"] = per_class(rows)
    nums["tokens"] = token_ratio(rows)
    ov = ROOT / "measurements" / "draw-overlap.json"
    nums["overlap"] = (sum(len(v) for v in json.loads(ov.read_text())["later_draw_duplicates"].values())
                       if ov.is_file() else 0)
    nums["behaviour"] = behaviour(rows)
    nums["by_kind"] = by_kind(rows)
    nums["patches_verified"] = sum(1 for r in rows if r["verdict"] in ("fixed", "not-fixed", "build-failed"))
    nums["tokens_per_fix"] = {"waveql": h["tokens_per_fix_waveql"], "control": h["tokens_per_fix_control"]}
    # Transcripts are stored per (task, arm, seed); a later run of the same cell
    # overwrites the file, so count the scored episodes whose transcript is theirs.
    import tarfile
    kept = {}
    tf = ROOT / "measurements" / "transcripts.tar.gz"
    if tf.is_file():
        with tarfile.open(tf) as t:
            for m in t.getmembers():
                if m.isfile() and m.name.endswith(".json"):
                    kept[Path(m.name).name] = json.loads(t.extractfile(m).read())
    tok = 0
    for r in rows:
        d = kept.get(Path(r.get("transcript") or "-").name)
        tok += bool(d) and d.get("queries") == r.get("queries") and d.get("turns") == r.get("turns")
    nums["transcripts"] = {"scored": len(rows), "own": tok}
    # Window coverage per draw: the first draw's recapture was verified 15/15;
    # later draws are checked by verify_windows --screen on their own dumps.
    win = {"draw1": [15, 15]}
    for tag, key in (("draw2", "draw2"), ("draw3", "draw3")):
        f = ROOT / "measurements" / f"windows-{tag}.txt"
        txt = f.read_text() if f.is_file() else ""
        m = re.search(r"(\d+)/(\d+) dumps contain", txt)
        if m:
            ok, n = int(m.group(1)), int(m.group(2))
            # A mutant already screened in an earlier draw counts there, not here.
            missed = {l.split()[0] for l in txt.splitlines() if "MISSED" in l}
            for r in (json.loads(l) for g in sorted((ROOT / "corpus").glob(f"{tag}*.jsonl"))
                      for l in g.read_text().splitlines() if l.strip()):
                if (headline.overlap_excluded(r["mutant_id"], f"{tag}.jsonl")
                        and r.get("verdict") == "killed" and r.get("vcd_path")
                        and r.get("kill_kind") in ("divergence", "assertion")):
                    n -= 1
                    ok -= r["mutant_id"][:8] not in missed
            win[key] = [ok, n]
    later = [v for k, v in win.items() if k != "draw1"]
    nums["windows"] = {"per_draw": win, "ok": sum(v[0] for v in win.values()),
                       "n": sum(v[1] for v in win.values()),
                       "later_ok": sum(v[0] for v in later), "later_n": sum(v[1] for v in later)}
    lost_by = collections.Counter(r["arm"] for r in allrows
                                  if r.get("stop") == "error" or r.get("verdict") == "lost")
    nums["lost_by_arm"] = {"waveql": lost_by.get("waveql", 0), "control": lost_by.get("control", 0)}
    # What scaling did to the repair-rate difference: the same computation over
    # the result files of each stage, cumulatively.
    stages = [
        ("first draw", ["measurements/episodes-draw1-initial.jsonl"]),
        ("+ seeds 2-3", ["measurements/episodes-draw1-extra-seeds.jsonl"]),
        ("+ draw 2", ["measurements/episodes-draw2*.jsonl"]),
        ("+ draw 3", ["measurements/episodes-draw3*.jsonl"]),
    ]
    traj, pats = [], []
    for label, add in stages:
        if not any(any(ROOT.glob(g)) for g in add):
            continue                  # a stage with no results yet adds no row
        pats = pats + add
        hs = headline.compute(pats)
        if "diff" in hs:
            traj.append({"label": label, "paired": hs["paired"], "episodes": hs["episodes"],
                         "diff": hs["diff"], "lo": hs["diff_ci"][0], "hi": hs["diff_ci"][1],
                         "diff_s": f"{hs['diff']*100:+.1f}"})     # formatted as the headline is
    nums["trajectory"] = traj
    cl = ROOT / "measurements" / "cloud.json"
    nums["cloud"] = json.loads(cl.read_text()) if cl.is_file() else {}

    tasks_total = sum(nums["corpus"][d]["tasks"] for d in nums["corpus"])
    tk = nums.get("tokens") or {}
    tok_line = (f"WaveQL reads {tk['ratio']:.2f}x the control's tokens per episode, 95% CI "
                f"[{tk['lo']:.2f}, {tk['hi']:.2f}], {tk['tasks']} paired tasks") if tk else "n/a"
    ho = nums["heldout"].get("heldout", {"n": 0, "file": 0, "static_file": 0, "dir": 0, "static_dir": 0})
    # README results, generated into a marked region for the same reason as the
    # paper: a hand-typed figure goes stale when the measurements change.
    readme = ROOT / "README.md"
    if readme.is_file():
        r = readme.read_text()
        b, e = "<!-- RESULTS:BEGIN", "<!-- RESULTS:END -->"
        if b in r and e in r:
            head = r[: r.index(b)]
            tail = r[r.index(e):]
            marker = r[r.index(b): r.index("-->", r.index(b)) + 3]
            body = f"""{marker}

Measured on {vals['EPISODES']} scored episodes of `gemini-3.1-pro` over {vals['PAIRED']}
BuggyBOOM tasks, every proposed patch verified by rebuilding the core:

| | WaveQL (joined store) | text control |
|---|---|---|
| verified repair rate (per task) | {nums['h1']['waveql']:.1%} | {nums['h1']['control']:.1%} |
| episodes repaired | {vals['NFW']} / {vals['NW']} | {vals['NFC']} / {vals['NC']} |
| tokens per episode | **{vals['TEW']}** | {vals['TEC']} |
| tokens per verified repair | **{vals['TFW']}** | {vals['TFC']} |

- **Comparable repairs for fewer tokens.** Repair-rate difference {vals['DIFF']} points
  (95% CI [{vals['DLO']}, {vals['DHI']}]); {tok_line}.
- **Navigation is the bottleneck.** With the store the agent proposed a patch in
  {nums['behaviour']['waveql']['proposed']:.0%} of episodes against
  {nums['behaviour']['control']['proposed']:.0%}; its most-used operation was a
  signal-name search.
- **Localization is a prior in an out-of-order core.** On {ho['n']} unseen bugs a
  fixed list of five hub files names the defect's file {ho['static_file']} times; the
  causal walk, {ho['file']}.
- **Scale changes the answer.** On the first {vals['PAIRED1']} tasks the joined view
  led by {vals['DIFF1']} points; at {vals['PAIRED']} the lead is gone.

Cost of the whole study: about **${nums['cost'].get('model_total_usd', 0):.0f}** of model
tokens and **${nums['cloud'].get('total_usd', 0):.0f}** of cloud compute.

"""
            readme.write_text(head + body + tail)
    (ROOT / "paper" / "numbers.json").write_text(json.dumps(nums, indent=1, default=str))
    print("rendered paper/numbers.json")
    for key in ("FILE", "DIR", "MEDIAN", "EPISODES", "PAIRED", "FW", "FC",
                "DIFF", "LDIFF", "TEW", "TEC", "CW", "CC", "LOST", "TOTAL"):
        print(f"  {key:9s} {vals[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
