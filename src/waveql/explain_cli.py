"""waveql-explain: why did this processor fail, in one command.

    waveql-explain b3bd38d7

reads the failing run's waveform, the DUT commit log and the Spike divergence,
joins them on cycle, and walks back through the generated netlist from the
failure to the logic that caused it -- printing one line per link with the value,
the cycle it last changed, and the Chisel file:line behind it.

A human debugging this opens a waveform viewer and scrolls. This is the same
evidence with no screen.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _task(mid: str):
    from waveql.corpus.task import load_tasks
    tasks = load_tasks(sorted((ROOT / "corpus").glob("*.jsonl")),
                       ROOT / "corpus" / "work")
    hits = [t for t in tasks if t.task_id.startswith(mid)]
    if not hits:
        raise SystemExit(f"no killed mutant matching {mid!r}. Known: "
                         + ", ".join(t.task_id[:8] for t in tasks))
    t = hits[0]
    rp = ROOT / "measurements" / "recapture.jsonl"
    if rp.is_file():
        for line in rp.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                if r["mutant_id"] == t.task_id and Path(r["vcd_path"]).is_file():
                    t.vcd_path = Path(r["vcd_path"])
    return t


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("mutant", help="mutant id prefix, e.g. b3bd38d7")
    ap.add_argument("--gen-src", default=str(ROOT / "var" / "netlist-snapshot"))
    ap.add_argument("--reveal", action="store_true",
                    help="after the explanation, show where the defect actually is")
    a = ap.parse_args(argv)

    t0 = time.monotonic()
    t = _task(a.mutant)
    tool = t.waveql_store(gen_src=Path(a.gen_src))
    db = tool.store.db
    nsig = db.execute("SELECT count(*) FROM signal").fetchone()[0]
    nchg = db.execute("SELECT count(*) FROM wave").fetchone()[0]
    ncom = db.execute("SELECT count(*) FROM commit_log").fetchone()[0]
    mb = t.vcd_path.stat().st_size / 1e6 if t.vcd_path else 0

    bar = "─" * 78
    print(bar)
    print(f" waveql-explain {t.task_id[:8]}   running {t.killing_stimulus}")
    print(f" joined {nsig:,} signals · {nchg:,} value changes · {ncom:,} commits "
          f"· from a {mb:.0f} MB waveform  ({time.monotonic() - t0:.0f}s)")
    print(bar)

    fd = tool.first_divergence()
    head = dict(zip(fd.columns, fd.rows[0])) if fd.rows else {}
    if head.get("kind") == "assertion":
        rep = tool.stall_report(limit=200)
        facts = {r[1]: r[2] for r in rep.rows if r[0] in ("stall", "rob")}
        print(f"\n THE PROCESSOR HUNG.  last instruction retired at cycle "
              f"{facts.get('last_retired_cycle')}, then nothing for "
              f"{facts.get('stalled_cycles')} cycles.\n")
        rows = [r for r in rep.rows if r[0] == "cause"]
        for r in rows:
            item, val, note = str(r[1]), str(r[2]), str(r[3])
            if item in ("stuck_entry", "rob_empty"):
                print(f"   ▸ {note}")
                continue
            depth, sig = item.split(" ", 1) if " " in item else ("", item)
            where = note.split(";", 1)[-1].strip() if ";" in note else ""
            when = note.split(";", 1)[0].replace("last change ", "") if ";" in note else ""
            print(f"   {depth:>4s}  {sig[:40]:40s} = {val[:12]:12s} "
                  f"{('@' + when) if when and when != 'None' else '':>7s}  {where}")
    else:
        kind = head.get("kind")
        if kind == "wdata":
            print(f"\n WRONG VALUE COMMITTED.  cycle {head.get('cycle')}: register "
                  f"x{head.get('reg')} got {head.get('dut')}, the golden ISA model "
                  f"says {head.get('detail')}.\n")
        else:
            print(f"\n WRONG PC.  cycle {head.get('cycle')}: the DUT fetched "
                  f"{head.get('dut')}, the golden ISA model is at {head.get('detail')}.\n")
        for r in fd.rows[1:]:
            item, val, where = str(r[1]), r[2], r[3] or ""
            if item.startswith("lane") or item == "frontend.s0_vpc":
                continue
            if item.startswith("--"):
                print(f"         {item}")
                continue
            depth, sig = item.split(" ", 1) if " " in item else ("", item)
            v = f"0x{int(val):x}" if str(val).isdigit() and int(val) > 9 else str(val)
            print(f"   {depth:>4s}  {sig[:44]:44s} = {v[:18]:18s}  {where}")

    print(f"\n {bar[1:]}")
    print(f" answered in {time.monotonic() - t0:.0f}s, with no waveform viewer and no screen.")
    if a.reveal:
        print(f"\n the defect was injected at  {t.true_path.split('/')[-1]}:{t.true_line}"
              f"   ({t.operator})")
    print(bar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
