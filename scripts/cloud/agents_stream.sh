#!/bin/bash
# Stream agent episodes for draw TAG while it is still being screened: every
# minute, each killed task not yet launched gets its own agents process (one
# process per task -- the tools' Python work holds the GIL, so threads alone do
# not scale). A task is launched at most once; the launched set is a file.
# Writes measurements/agents-TAG.done when the screen is over and all finished.
#   agents_stream.sh TAG BASE_WORKER [EPISODES_IN_PARALLEL_PER_TASK]
cd "${WAVEQL_ROOT:?}"
TAG=$1; BASE=$2; PER=${3:-6}
set -a; source configs/gcp.env; set +a
L=var/log/launched-$TAG.txt; touch "$L"
ids() {
  .venv/bin/python - "$TAG" <<'PY'
import json, glob, sys
last = {}
for f in sorted(glob.glob(f"corpus/{sys.argv[1]}-*.jsonl")):
    for l in open(f):
        if l.strip():
            r = json.loads(l)
            if r.get("rescreen") or not last.get(r["mutant_id"], {}).get("rescreen"):
                last[r["mutant_id"]] = r
print("\n".join(sorted(m for m, r in last.items() if r.get("verdict") == "killed"
                       and r.get("kill_kind") in ("divergence", "assertion"))))
PY
}
screening() { ps -eo args= | awk '$1 == ".venv/bin/python" && $3 == "scripts/run/screen_campaign.py"' | grep -q .; }
running() { ps -eo args= | awk -v t="$TAG" '$1 == ".venv/bin/python" && $4 == "agents" && index($0, "--tag " t)' | grep -q .; }
while true; do
  NEW=$(comm -13 <(sort "$L") <(ids | sort))
  if [ -n "$NEW" ]; then
    for t in $NEW; do
      echo "$(date -u +%H:%M) launching $t"; echo "$t" >> "$L"
      setsid nohup .venv/bin/python -u scripts/run/scale.py agents --agents "$PER" --seeds 1 2 3 \
        --model "${WAVEQL_MODEL:-gemini-3.1-pro-preview}" --base "$BASE" --tag "$TAG" \
        --no-done-marker --tasks "$t" >> "var/log/agents-$TAG.log" 2>&1 < /dev/null &
    done
  elif ! screening && ! running; then
    date -u +%s > "measurements/agents-$TAG.done"; echo "all agents for $TAG finished"; break
  fi
  sleep 60
done
