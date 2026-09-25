#!/usr/bin/env bash
# Create N isolated Chipyard checkouts so mutants can be screened in parallel.
#
#   ./scripts/setup/40_workers.sh 3      # create/refresh 3 workers
#   ./scripts/setup/40_workers.sh clean  # remove them
#
# WHY THIS IS AFFORDABLE. The mutation engine edits Chisel source in place and
# holds an exclusive lock on the checkout, so one checkout means one mutant at a
# time -- and a mutant costs a 140 s build. On a filesystem with reflink (btrfs,
# XFS), `cp --reflink` makes a copy-on-write clone of the whole 15 GB tree
# instantly and at zero additional disk. Only the blocks a worker actually
# rewrites (its own generated-src) ever cost space.
#
# THE CONDA ENVIRONMENT IS SHARED, NOT COPIED. chipyard's .conda-env contains
# absolute paths baked in at creation, so a copy at a different path is broken:
# $RISCV, the compiler wrappers and the firtool shim would all point back at the
# original anyway. Each worker therefore symlinks .conda-env at the primary's.
# That is safe because a *simulator* build only reads it -- the toolchain was
# built once, by step 3 of build-setup.sh, and nothing here writes to it.
#
# Everything a build DOES write is worker-local: generated-src/, .ivy2/, .sbt/.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/env.sh"
PRIMARY="$CHIPYARD_DIR"
WORKERS="$ROOT/var/workers"

if [ "${1:-}" = "clean" ]; then
    rm -rf "$WORKERS"; echo "[workers] removed $WORKERS"; exit 0
fi
N="${1:-3}"
[ -d "$PRIMARY/.conda-env" ] || { echo "[workers] primary has no .conda-env; build it first" >&2; exit 1; }

mkdir -p "$WORKERS"
for i in $(seq 1 "$N"); do
    W="$WORKERS/w$i"
    if [ -d "$W/.git" ]; then
        echo "[workers] w$i exists; refreshing source only"
        rm -rf "$W/generators/boom" && cp -a --reflink=auto "$PRIMARY/generators/boom" "$W/generators/boom"
        continue
    fi
    echo "[workers] cloning primary -> w$i (reflink COW)"
    t0=$SECONDS
    rm -rf "$W"
    cp -a --reflink=auto "$PRIMARY" "$W"
    # Share the toolchain rather than duplicating a non-relocatable env.
    rm -rf "$W/.conda-env"
    ln -s "$PRIMARY/.conda-env" "$W/.conda-env"
    # Drop the primary's build output: this worker builds its own.
    rm -rf "$W/sims/verilator/generated-src" "$W"/sims/verilator/simulator-*
    echo "[workers] w$i ready in $((SECONDS-t0))s"
done
echo
echo "[workers] disk actually consumed by $N COW clones:"
du -sh --apparent-size "$WORKERS" 2>/dev/null | awk '{print "    apparent: "$1}'
du -sh "$WORKERS" 2>/dev/null | awk '{print "    real:     "$1}'
df -h "$ROOT" | tail -1 | awk '{print "    free:     "$4}'
