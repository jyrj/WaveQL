#!/usr/bin/env python3
"""Fix-rate at scale: agent episodes in parallel, verification queued.

Only the final verification of an episode needs a Chipyard checkout; the agent
phase reads Chisel source and queries the store, and is bound by the model API.
The two are therefore split:

  agents  N threads per process. Each episode gets a scratch copy of the Chisel
          source subtree with the mutation applied, so episodes need no
          checkout. Episodes with nothing to verify (no proposal, a rejected
          anchor, a transport loss) are finished here.
  verify  one process per worker checkout. Claims a pending proposal, applies
          the mutation, rebuilds, re-runs every stimulus and writes the row.
          Claims are lock files on one host (a dead holder's claim is taken
          over), or GCS objects created with ifGenerationMatch=0 when verifiers
          on several hosts share one queue (--gcs-claims).

Rows use the same schema as run_fixrate.py: episodes-<tag>-*.jsonl for scored
episodes and proposals-<tag>.jsonl for the verification queue, so headline.py
and every renderer read them unchanged.
"""
import argparse, json, os, shutil, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src")); sys.path.insert(0, str(ROOT / "scripts" / "run"))
from waveql.agent.arms import REPAIR_SYSTEM, ControlTool, arm_surface      # noqa: E402
from waveql.agent.seat import VertexSeat, cost_usd                         # noqa: E402
from waveql.agent.source import SourceView                                 # noqa: E402
from waveql.analysis.metrics import score_blame                            # noqa: E402
from waveql.corpus.campaign import DEFAULT_STIMULI                         # noqa: E402
from waveql.corpus.task import load_tasks                                  # noqa: E402
from waveql.corpus.verify import FixResult, _tampering, verify_applied     # noqa: E402
from waveql.harness.chipyard import ChipyardEnv                            # noqa: E402
from waveql.mutator.engine import mutated                                  # noqa: E402
import run_fixrate                                                          # noqa: E402

M = ROOT / "measurements"
SUBTREE = "generators/boom/src/main/scala/v3"
FINAL_NUDGE = ("You are out of budget. Call propose_fix NOW with your best candidate "
               "edit -- an unproposed fix scores zero -- and then give the three "
               "required lines.")
_write = threading.Lock()


def key(r):
    return (r["task_id"], r["arm"], int(r["seed"]))


def rows(pattern):
    for p in sorted(M.glob(pattern)):
        for line in p.read_text().splitlines():
            if line.strip():
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    pass


def append(path: Path, row: dict) -> None:
    with _write, path.open("a") as fh:
        fh.write(json.dumps(row, default=str) + "\n")


def tasks_with_windows():
    tasks = load_tasks(sorted((ROOT / "corpus").glob("*.jsonl")),
                       ROOT / "corpus" / "work")
    # A mutant screened by more than one draw is one task, from its first draw
    # (manifests sort by draw); see measurements/draw-overlap.json.
    seen: set[str] = set()
    tasks = [t for t in tasks if not (t.task_id in seen or seen.add(t.task_id))]
    rp = M / "recapture.jsonl"
    if rp.is_file():
        recap = {json.loads(l)["mutant_id"]: json.loads(l)
                 for l in rp.read_text().splitlines() if l.strip()}
        for t in tasks:
            r = recap.get(t.task_id)
            if r and r.get("vcd_path") and Path(r["vcd_path"]).is_file():
                t.vcd_path = Path(r["vcd_path"])
    return tasks


def scored_cells():
    """Cells with a final row that is not a transport loss."""
    return {key(r) for r in rows("episodes-*.jsonl")
            if r.get("verdict") != "lost" and r.get("stop") != "error"}


def base_row(t, arm, seed, model, ep, sv, fr, t0):
    sc = score_blame(ep.answer, true_module=t.true_module, true_context=t.true_context,
                     true_before=t.true_before, true_cycle=t.true_cycle)
    return {**asdict(fr), "model": model, "wall_seconds": round(time.monotonic() - t0, 1),
            "episode_seconds": round(ep.wall_seconds, 1), "queries": ep.queries,
            "turns": ep.turns, "tokens": ep.usage.total, "cost_usd": cost_usd(model, ep.usage),
            "tokens_prompt": ep.usage.prompt, "tokens_cached": ep.usage.cached,
            "tokens_output": ep.usage.output, "tokens_thought": ep.usage.thought,
            "stop": ep.stop_reason, "error": ep.error, "module": sc.module,
            "signal": sc.signal, "cycle": sc.cycle, "kill_kind": t.kill_kind,
            "mutation_class": t.mutation_class, "operator": t.operator,
            "true_module": t.true_module, "true_path": t.true_path,
            "true_line": t.true_line, "answer": (ep.answer or "")[:800],
            "tool_calls": [c.name for c in ep.tool_calls],
            "rejected_proposals": len(sv.rejected)}


# --------------------------------------------------------------------------- agents

def run_agents(a) -> int:
    model = a.model or os.environ.get("WAVEQL_MODEL_PRIMARY")
    base = ChipyardEnv.load(a.base)
    tasks = tasks_with_windows()
    if a.tasks:
        tasks = [t for t in tasks if any(t.task_id.startswith(x) for x in a.tasks)]
    pending = {key(r) for r in rows(f"proposals-{a.tag}.jsonl")}
    done = scored_cells() | pending
    jobs = [(t, arm, s) for s in a.seeds for t in tasks for arm in a.arms
            if (t.task_id, arm, s) not in done]
    print(f"{len(jobs)} episode(s) to run on {a.agents} parallel agents, "
          f"model {model}; {len(done)} cell(s) already scored or pending", flush=True)
    scratch_root = ROOT / "var" / "scratch-src"
    final_a = M / f"episodes-{a.tag}-A.jsonl"
    agent_out = M / f"proposals-{a.tag}.jsonl"
    tdir = M / "transcripts"; tdir.mkdir(parents=True, exist_ok=True)
    local = threading.local()

    def one(job):
        t, arm, seed = job
        t0 = time.monotonic()
        site = run_fixrate.site_for(base, t)
        sroot = scratch_root / f"{t.task_id[:12]}-{arm}-s{seed}"
        shutil.rmtree(sroot, ignore_errors=True)
        dst = sroot / SUBTREE
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(base.root / SUBTREE, dst)
        target = sroot / site.path
        orig = target.read_text()
        if orig[site.start:site.end] != site.before:
            raise RuntimeError(f"{t.task_id}: scratch copy does not match the site")
        target.write_text(site.apply(orig))
        sv = SourceView(root=sroot)
        wq = t.waveql_store(gen_src=Path(a.gen_src)) if arm == "waveql" else None
        ctl = ControlTool(t.text_evidence()) if arm == "control" else None
        decls, dispatch = arm_surface(arm, waveql=wq, control=ctl, source=sv)
        if not hasattr(local, "seat"):
            local.seat = VertexSeat(model=model)
        ep = local.seat.run(task_id=t.task_id, arm=arm, system=REPAIR_SYSTEM,
                            prompt=t.prompt(), declarations=decls, dispatch=dispatch,
                            max_turns=a.max_turns, deadline_s=a.deadline, seed=seed,
                            final_nudge=FINAL_NUDGE, mid_nudge=run_fixrate.MID_NUDGE,
                            commit_tools=["propose_fix", "read_source",
                                          "search_source", "list_files"])
        proposal = sv.proposals[-1] if sv.proposals else None
        fr = FixResult(task_id=t.task_id, arm=arm, seed=seed, verdict="no-proposal",
                       stimuli_total=len(DEFAULT_STIMULI), proposal=proposal)
        if proposal:
            why = _tampering(proposal["old"], proposal["new"])
            if why:
                fr.verdict, fr.reason = "rejected", why
        elif ep.error:
            fr.verdict = "lost"
            fr.reason = f"episode lost to a transport error after {ep.queries} queries: {ep.error[:120]}"
        else:
            fr.reason = (f"propose_fix was called but every anchor was rejected "
                         f"({len(sv.rejected)} attempt(s))" if sv.rejected
                         else "the agent never called propose_fix")
        row = base_row(t, arm, seed, model, ep, sv, fr, t0)
        tpath = tdir / f"{t.task_id[:8]}-{arm}-s{seed}.json"
        tpath.write_text(json.dumps({
            "task_id": t.task_id, "arm": arm, "seed": seed, "stop": ep.stop_reason,
            "turns": ep.turns, "queries": ep.queries, "true_path": t.true_path,
            "true_line": t.true_line,
            "tool_calls": [{"turn": c.turn, "name": c.name, "args": c.args,
                            "chars": c.result_chars} for c in ep.tool_calls],
            "transcript": ep.transcript}, indent=1, default=str))
        row["transcript"] = str(tpath)
        shutil.rmtree(sroot, ignore_errors=True)
        if proposal and fr.verdict != "rejected":
            append(agent_out, row)              # verification pending
            state = "-> verify"
        else:
            append(final_a, row)
            state = fr.verdict
        print(f"  A {t.task_id[:8]} {arm:8s} s{seed} {state:12s} q={ep.queries:2d} "
              f"tok={ep.usage.total:8,} {time.monotonic() - t0:5.0f}s", flush=True)
        return state

    with ThreadPoolExecutor(max_workers=a.agents) as ex:
        futs = {ex.submit(one, j): j for j in jobs}
        for f in as_completed(futs):
            try:
                f.result()
            except Exception as e:                               # noqa: BLE001
                t, arm, s = futs[f]
                print(f"  A {t.task_id[:8]} {arm} s{s} CRASH {type(e).__name__}: {e}",
                      flush=True)
    if not a.no_done_marker:        # a wave driver writes the marker after its LAST wave
        (M / f"agents-{a.tag}.done").write_text(str(time.time()))
    print("agents finished", flush=True)
    return 0


# --------------------------------------------------------------------------- verify

def alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def claim(k) -> bool:
    cdir = M / "claims"; cdir.mkdir(exist_ok=True)
    p = cdir / f"{k[0][:12]}-{k[1]}-s{k[2]}.lock"
    try:
        fd = os.open(p, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        os.write(fd, str(os.getpid()).encode()); os.close(fd)
        return True
    except FileExistsError:
        try:
            holder = int(p.read_text() or 0)
        except (ValueError, OSError):
            holder = 0
        if holder and not alive(holder):           # a dead verifier's claim
            p.unlink(missing_ok=True)
            return claim(k)
        return False


class GcsClaims:
    """Claims shared by verifiers on several machines.

    A claim is a GCS object created with ifGenerationMatch=0: the create succeeds
    for exactly one caller, so two machines can never verify the same cell. A
    local lock file cannot do that across hosts, and a pid means nothing there.
    """
    API = "https://storage.googleapis.com"

    def __init__(self, url: str, who: str):
        import google.auth
        from google.auth.transport.requests import AuthorizedSession
        bucket, _, prefix = url.removeprefix("gs://").partition("/")
        self.bucket, self.prefix, self.who = bucket, prefix.rstrip("/") + "/", who
        creds, _ = google.auth.default(
            scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
        self.s = AuthorizedSession(creds)

    @staticmethod
    def name(k) -> str:
        return f"{k[0][:12]}-{k[1]}-s{k[2]}"

    def held(self) -> set[str]:
        out, tok = set(), None
        while True:
            q = {"prefix": self.prefix, "fields": "items(name),nextPageToken"}
            if tok:
                q["pageToken"] = tok
            r = self.s.get(f"{self.API}/storage/v1/b/{self.bucket}/o", params=q, timeout=60)
            r.raise_for_status()
            d = r.json()
            out |= {i["name"][len(self.prefix):] for i in d.get("items", [])}
            tok = d.get("nextPageToken")
            if not tok:
                return out

    def claim(self, k) -> bool:
        r = self.s.post(f"{self.API}/upload/storage/v1/b/{self.bucket}/o",
                        params={"uploadType": "media", "name": self.prefix + self.name(k),
                                "ifGenerationMatch": "0"},
                        data=f"{self.who} {time.time():.0f}".encode(), timeout=60)
        if r.status_code == 412:
            return False
        r.raise_for_status()
        return True


def run_verify(a) -> int:
    cy = ChipyardEnv.load(a.worker)
    wname = Path(a.worker).name
    out = M / f"episodes-{a.tag}-V-{wname}.jsonl"
    stim_base = cy.root / "toolchains" / "riscv-tools" / "riscv-tests" / "build"
    stimuli = [stim_base / s for s in DEFAULT_STIMULI]
    tasks = {t.task_id: t for t in tasks_with_windows()}
    import socket
    gcs = (GcsClaims(a.gcs_claims, f"{socket.gethostname()}:{wname}:{os.getpid()}")
           if a.gcs_claims else None)
    print(f"verifier on {wname}" + (f", claims in {a.gcs_claims}" if gcs else ""), flush=True)
    idle = 0
    while True:
        # Re-read every pass: on a machine fed by a mirror, a proposal can arrive
        # before the manifest naming its task. Such a row waits; it is never
        # claimed, because a claim taken and then abandoned is a cell lost.
        tasks = {t.task_id: t for t in tasks_with_windows()}
        verified = {key(r) for r in rows(f"episodes-{a.tag}-V-*.jsonl")}
        pend = [r for r in rows(f"proposals-{a.tag}.jsonl")
                if key(r) not in verified and r["task_id"] in tasks]
        if a.foreign_claims:
            # Claims held by a verifier on another host, mirrored here as
            # lock names. Its pids mean nothing on this host, so they are
            # skipped outright rather than tested for liveness.
            fc = Path(a.foreign_claims)
            held = set(fc.read_text().split()) if fc.is_file() else set()
            pend = [r for r in pend
                    if f"{r['task_id'][:12]}-{r['arm']}-s{r['seed']}.lock" not in held]
        if a.reverse:
            pend.reverse()          # take the queue from the end another host starts at
        if gcs is not None:
            held = gcs.held()
            pend = [r for r in pend if gcs.name(key(r)) not in held]
        did = False
        for r in pend:
            k = key(r)
            if not (gcs.claim(k) if gcs is not None else claim(k)):
                continue
            did = True
            t = tasks[r["task_id"]]
            site = run_fixrate.site_for(cy, t)
            fr = FixResult(task_id=r["task_id"], arm=r["arm"], seed=int(r["seed"]),
                           verdict="no-proposal", stimuli_total=len(stimuli),
                           proposal=r["proposal"])
            t0 = time.monotonic()
            with mutated(cy.root, site, seed=int(r["seed"])):
                fr = verify_applied(cy, site, r["proposal"], stimuli, res=fr,
                                    work=ROOT / "corpus" / "fix" /
                                    f"{r['task_id']}-{r['arm']}-s{r['seed']}",
                                    jobs=a.jobs)
            final = {**r, **asdict(fr)}
            append(out, final)
            print(f"  V[{wname}] {r['task_id'][:8]} {r['arm']:8s} s{r['seed']} "
                  f"{fr.verdict:12s} {fr.stimuli_passed}/{fr.stimuli_total} "
                  f"revert={int(fr.exact_revert)} {time.monotonic() - t0:5.0f}s", flush=True)
            break                                    # re-scan: another may be ready
        if did:
            idle = 0
            continue
        if (M / f"agents-{a.tag}.done").is_file() and not pend:
            print(f"verifier on {wname}: nothing left", flush=True)
            return 0
        idle += 1
        time.sleep(30)


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="mode", required=True)
    g = sub.add_parser("agents")
    g.add_argument("--agents", type=int, default=8)
    g.add_argument("--seeds", type=int, nargs="+", default=[2, 3])
    g.add_argument("--arms", nargs="+", default=["waveql", "control"])
    g.add_argument("--tasks", nargs="*", default=None)
    g.add_argument("--model", default=None)
    g.add_argument("--max-turns", type=int, default=30)
    g.add_argument("--deadline", type=float, default=900.0)
    g.add_argument("--base", default=str(ROOT / "var" / "workers" / "w1"))
    g.add_argument("--gen-src", default=str(ROOT / "var" / "netlist-snapshot"))
    g.add_argument("--tag", default="scale")
    g.add_argument("--no-done-marker", action="store_true",
                   help="more waves follow; do not tell verifiers the queue is final")
    v = sub.add_parser("verify")
    v.add_argument("--worker", required=True)
    v.add_argument("--jobs", type=int, default=8)
    v.add_argument("--tag", default="scale")
    v.add_argument("--reverse", action="store_true",
                   help="take the queue from its end (a second machine's verifiers)")
    v.add_argument("--gcs-claims", default=None, metavar="gs://BUCKET/PREFIX",
                   help="claim cells through GCS so verifiers on several machines share one queue")
    v.add_argument("--foreign-claims", default=None,
                   help="file of lock names held by verifiers on another machine")
    a = ap.parse_args()
    return run_agents(a) if a.mode == "agents" else run_verify(a)


if __name__ == "__main__":
    raise SystemExit(main())
