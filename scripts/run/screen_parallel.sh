#!/usr/bin/env bash
# Screen mutants across N worker checkouts in parallel.
#
#   ./scripts/run/screen_parallel.sh [N_WORKERS] [PER_CLASS]
#
# Each worker owns an isolated COW clone of chipyard (scripts/setup/40_workers.sh)
# and takes a round-robin shard of the same class-balanced sample, so the sample
# and the seed are identical to a serial run -- only the order of execution
# differs. Manifests are per-shard and concatenate into one corpus.
#
# CORE BUDGET (sized for a 24-thread host). Each worker builds with -j$JOBS and runs up to
# $RUNW concurrent single-threaded simulators. Builds and stimulus runs from
# different workers overlap, so the allocation deliberately leaves headroom
# rather than summing to exactly 24.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
N="${1:-3}"
PER_CLASS="${2:-5}"
JOBS="${JOBS:-7}"
RUNW="${RUNW:-4}"
SEED="${SEED:-20260904}"

for i in $(seq 1 "$N"); do
    W="$ROOT/var/workers/w$i"
    [ -d "$W" ] || { echo "worker $W missing; run scripts/setup/40_workers.sh $N" >&2; exit 1; }
done

mkdir -p corpus var/log
for i in $(seq 1 "$N"); do
    idx=$((i - 1))
    nohup ./.venv/bin/python scripts/run/screen_campaign.py \
        --chipyard "$ROOT/var/workers/w$i" \
        --shard "$idx/$N" \
        --per-class "$PER_CLASS" --seed "$SEED" \
        --jobs "$JOBS" --run-workers "$RUNW" --skip-baseline \
        --out "corpus/draw-shard$idx.jsonl" \
        > "var/log/campaign-w$i.log" 2>&1 &
    echo "worker $i -> shard $idx/$N  pid $!  log var/log/campaign-w$i.log"
done
echo
echo "watch:  tail -f var/log/campaign-w*.log"
echo "manifests: corpus/draw-shard*.jsonl"
