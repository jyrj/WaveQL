#!/usr/bin/env bash
# Rebuild every submission artifact from the measurement files, in dependency
# order. No figure in the paper, the typeset PDF or the showcase page is typed by
# hand, so re-running this after new episodes land is the whole update.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT"
PY=${PYTHON:-python}
$PY scripts/run/render_paper.py      | head -1          # paper/numbers.json, README results
$PY scripts/run/build_site.py                           # paper/site/index.html
typst compile --root . paper/waveql-paper.typ paper/waveql-paper.pdf
echo "paper/waveql-paper.pdf: $(pdfinfo paper/waveql-paper.pdf | awk '/Pages/{print $2}') pages"
$PY scripts/run/headline.py
