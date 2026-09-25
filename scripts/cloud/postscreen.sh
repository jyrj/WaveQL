#!/bin/bash
# After draw TAG's screen ends, in order:
#  1. re-screen with the current classifier any mutant filed "invalid" or "crash"
#     although an assertion fired before Spike's banner (spare workers);
#  2. run the frozen localizer on every killed task of the draw (held-out test);
#  3. check that every captured window contains its failure.
#   postscreen.sh TAG SEED "spare1 spare2 ..."
cd "${WAVEQL_ROOT:?}"
TAG=$1; SEED=$2; WORKERS=($3)
while ps -eo args= | awk '$1 == ".venv/bin/python" && $3 == "scripts/run/screen_campaign.py"' | grep -q .; do sleep 30; done
IDS=($(.venv/bin/python - "$TAG" <<'PY'
import json, glob, sys
for f in sorted(glob.glob(f"corpus/{sys.argv[1]}-shard*.jsonl")):
    for l in open(f):
        r = json.loads(l) if l.strip() else {}
        if r.get("verdict") == "invalid" or (r.get("verdict") == "killed" and r.get("kill_kind") == "crash"):
            print(r["mutant_id"])
PY
))
n=${#WORKERS[@]}
for k in $(seq 0 $((n - 1))); do
  mine=(); for j in $(seq "$k" "$n" $((${#IDS[@]} - 1))); do mine+=("${IDS[$j]}"); done
  [ ${#mine[@]} -eq 0 ] && continue
  .venv/bin/python -u scripts/run/rescreen.py --chipyard "var/workers/${WORKERS[$k]}" --seed "$SEED" \
    --ids "${mine[@]}" --out "corpus/$TAG-rescreen$k.jsonl" > "var/log/rescreen-$TAG-$k.log" 2>&1 &
done
wait
mkdir -p "var/heldout-$TAG"
for m in corpus/"$TAG"-*.jsonl; do
  k=$(basename "$m" .jsonl)
  .venv/bin/python scripts/run/trace_eval.py --manifests "$m" --out "var/heldout-$TAG/$k.jsonl" \
    > "var/heldout-$TAG/$k.log" 2>&1 &
done
wait
cat "var/heldout-$TAG"/*.jsonl > "measurements/localization-$TAG.jsonl"
.venv/bin/python scripts/check/verify_windows.py --screen "corpus/$TAG-*.jsonl" --stem capture \
  > "measurements/windows-$TAG.txt" 2>&1
tail -1 "measurements/windows-$TAG.txt"
