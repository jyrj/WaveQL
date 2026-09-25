#!/usr/bin/env bash
# Install WaveQL's Scala sources into the Chipyard generator tree.
#
# WHY A COPY AND NOT A PATCH. These are new files, not edits to upstream ones, so
# there is nothing to patch against. They live in this repository (src/scala/,
# reviewable and version-controlled) and are copied into chipyard's build path,
# which is the only place sbt will compile them from. The copy is idempotent and
# each installed file carries a provenance header naming its source, so a reader
# who finds one inside thirdparty/ knows it is not upstream code.
#
# Removing them is `scripts/build/05_install_scala.sh --uninstall`.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck disable=SC1091
source "$ROOT/env.sh"
DEST="$CHIPYARD_DIR/generators/chipyard/src/main/scala/waveql"

if [ "${1:-}" = "--uninstall" ]; then
    rm -rf "$DEST"
    echo "[05_scala] removed $DEST"
    exit 0
fi

mkdir -p "$DEST"
count=0
while IFS= read -r src; do
    name="$(basename "$src")"
    {
        echo "// GENERATED COPY -- do not edit here."
        echo "// Source of truth: waveql/src/scala/${src#"$ROOT/src/scala/"}"
        echo "// Installed by scripts/build/05_install_scala.sh"
        cat "$src"
    } > "$DEST/$name"
    count=$((count + 1))
    echo "[05_scala] installed $name"
done < <(find "$ROOT/src/scala" -name "*.scala" | sort)

echo "[05_scala] $count file(s) -> $DEST"
