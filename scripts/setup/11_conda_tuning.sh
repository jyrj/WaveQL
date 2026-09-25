#!/usr/bin/env bash
# Repo-local conda download tuning.
#
# WHY: chipyard build-setup can die in step 1
# with `ConnectionResetError(104, 'Connection reset by peer')` while conda-lock
# was populating .conda-env. A single-stream probe pulled conda-forge's 442 MB
# linux-64 repodata.json in 11 s at http 200, three times running, so the link
# is healthy -- what fails is conda's *concurrency*: 5 parallel fetch threads
# against the CDN get individual streams reset.
#
# Everything is written to tools/conda/.condarc, which conda reads because
# tools/conda is CONDA_ROOT. No system or ~/.condarc file is touched.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
CONDARC="$ROOT/tools/conda/.condarc"
CONDA="$ROOT/tools/conda/bin/conda"
[ -x "$CONDA" ] || { echo "[11_conda] run scripts/setup/10_conda.sh first" >&2; exit 1; }

# Fewer concurrent streams, and far more patience per stream.
"$CONDA" config --file "$CONDARC" --set fetch_threads 2
"$CONDA" config --file "$CONDARC" --set remote_max_retries 10
"$CONDA" config --file "$CONDARC" --set remote_backoff_factor 5
"$CONDA" config --file "$CONDARC" --set remote_connect_timeout_secs 30
"$CONDA" config --file "$CONDARC" --set remote_read_timeout_secs 300
# Deterministic resolution: strict priority keeps everything on conda-forge,
# which is what chipyard's lockfiles were solved against.
"$CONDA" config --file "$CONDARC" --set channel_priority strict
"$CONDA" config --file "$CONDARC" --set always_yes true

echo "[11_conda] wrote $CONDARC:"
cat "$CONDARC"
