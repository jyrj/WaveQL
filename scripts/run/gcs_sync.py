#!/usr/bin/env python3
"""Mirror run files between machines through one GCS prefix.

Each machine uploads the files it WRITES (--up) and downloads the ones others
write (--down). A file has exactly one writer, so there is nothing to merge:
the hub publishes the agent queue and the screen manifests, every verifier host
publishes its own verdict rows, and anyone may read everything.

  gcs_sync.py --prefix gs://BUCKET/sync \
              --up 'measurements/proposals-draw4.jsonl' 'corpus/draw4-*.jsonl' \
              --down 'measurements/episodes-draw4-V-*.jsonl'

Safety: a file is never uploaded from a host that downloaded it, and a remote
file is never replaced by a smaller local copy (every mirrored file is
append-only), so a stale copy on one host cannot overwrite a newer one.
"""
import argparse, fnmatch, os, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
API = "https://storage.googleapis.com"


def session():
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    c, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/devstorage.read_write"])
    return AuthorizedSession(c)


def listing(s, bucket, prefix):
    out, tok = {}, None
    while True:
        q = {"prefix": prefix, "fields": "items(name,generation),nextPageToken"}
        if tok:
            q["pageToken"] = tok
        r = s.get(f"{API}/storage/v1/b/{bucket}/o", params=q, timeout=60)
        r.raise_for_status()
        d = r.json()
        for i in d.get("items", []):
            out[i["name"][len(prefix):]] = i["generation"]
        tok = d.get("nextPageToken")
        if not tok:
            return out


def remote_sizes(s, bucket, prefix):
    out, tok = {}, None
    while True:
        q = {"prefix": prefix, "fields": "items(name,size),nextPageToken"}
        if tok:
            q["pageToken"] = tok
        r = s.get(f"{API}/storage/v1/b/{bucket}/o", params=q, timeout=60)
        r.raise_for_status()
        d = r.json()
        for i in d.get("items", []):
            out[i["name"][len(prefix):]] = int(i["size"])
        tok = d.get("nextPageToken")
        if not tok:
            return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--prefix", default=os.environ.get("WAVEQL_SYNC"),
                    help="gs://bucket/prefix (default: $WAVEQL_SYNC)")
    ap.add_argument("--up", nargs="*", default=[])
    ap.add_argument("--down", nargs="*", default=[])
    ap.add_argument("--every", type=float, default=60)
    ap.add_argument("--once", action="store_true")
    a = ap.parse_args()
    if not a.prefix:
        raise SystemExit("no --prefix and no $WAVEQL_SYNC")
    bucket, _, prefix = a.prefix.removeprefix("gs://").partition("/")
    prefix = prefix.rstrip("/") + "/"
    s = session()
    sent: dict[str, tuple] = {}
    got: dict[str, str] = {}
    while True:
        try:
            n_up = n_down = 0
            mine = set()
            sizes = remote_sizes(s, bucket, prefix) if a.up else {}
            for g in a.up:
                for p in sorted(ROOT.glob(g)):
                    rel = str(p.relative_to(ROOT))
                    if rel in got:
                        continue                # another host's file: never republished
                    mine.add(rel)
                    st = p.stat(); sig = (st.st_size, st.st_mtime_ns)
                    if sent.get(rel) == sig:
                        continue
                    if sizes.get(rel, -1) > st.st_size:  # append-only: smaller is stale
                        print(f"  refusing to shrink {rel}: remote {sizes[rel]} > local {st.st_size}", flush=True)
                        continue
                    r = s.post(f"{API}/upload/storage/v1/b/{bucket}/o",
                               params={"uploadType": "media", "name": prefix + rel},
                               data=p.read_bytes(), timeout=120)
                    r.raise_for_status(); sent[rel] = sig; n_up += 1
            if a.down:
                for rel, gen in listing(s, bucket, prefix).items():
                    if rel in mine or got.get(rel) == gen:
                        continue
                    if not any(fnmatch.fnmatch(rel, g) for g in a.down):
                        continue
                    r = s.get(f"{API}/storage/v1/b/{bucket}/o/{(prefix + rel).replace('/', '%2F')}",
                              params={"alt": "media"}, timeout=120)
                    r.raise_for_status()
                    dst = ROOT / rel; dst.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dst.with_suffix(dst.suffix + ".part")
                    tmp.write_bytes(r.content); tmp.replace(dst)      # never a half file
                    got[rel] = gen; n_down += 1
            print(f"{time.strftime('%H:%M:%S')} up {n_up} down {n_down}", flush=True)
        except Exception as e:                                     # noqa: BLE001
            print(f"{time.strftime('%H:%M:%S')} sync error {type(e).__name__}: {e}", flush=True)
        if a.once:
            return 0
        time.sleep(a.every)


if __name__ == "__main__":
    raise SystemExit(main())
