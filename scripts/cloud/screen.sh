#!/bin/bash
# Screen one class-balanced draw across N worker checkouts on this host.
#   screen.sh DRAW SEED [EXCLUDE_GLOB ...]
# Workers are clones of $BASE (default var/workers/w0) with the build outputs
# removed, so each builds its own generated sources. Every earlier draw's
# manifests are passed as EXCLUDE_GLOBs so draws are disjoint.
set -euo pipefail
cd "${WAVEQL_ROOT:?}"
DRAW=$1; SEED=$2; shift 2
N=${N:-16}; PER_CLASS=${PER_CLASS:-15}; BASE=${BASE:-var/workers/w0}; PREFIX=${PREFIX:-s}
EXC=(); for g in "$@"; do EXC+=(--exclude-screened "$g"); done
mkdir -p var/log corpus/work
for i in $(seq 1 "$N"); do
  w=var/workers/$PREFIX$i
  if [ ! -d "$w/generators" ]; then
    (cp -a "$BASE" "$w.tmp" && rm -rf "$w.tmp/sims/verilator/generated-src" "$w.tmp"/sims/verilator/simulator-* \
      && mv "$w.tmp" "$w") &
  fi
done
wait
for i in $(seq 1 "$N"); do
  setsid nohup .venv/bin/python -u scripts/run/screen_campaign.py --chipyard "var/workers/$PREFIX$i" \
    --shard "$((i-1))/$N" --per-class "$PER_CLASS" --seed "$SEED" --jobs 6 --run-workers 6 \
    --skip-baseline "${EXC[@]}" --out "corpus/$DRAW-shard$((i-1)).jsonl" \
    > "var/log/screen-$DRAW-$PREFIX$i.log" 2>&1 < /dev/null &
done
echo "screening draw $DRAW (seed $SEED) on $N workers"
