#!/bin/zsh
# Usage: run_one.sh <label> <DELIVERY_BATCH> <DELIVERY_CONCURRENCY> <output dir under loadtest/results/...>
# Runs one pre-registered M11 end-to-end trial (700 normal + 2 critical /s, 1,000 keys, 60 s + 20 s drain,
# 2 intake, 4 normal rule workers, 1 critical, 1 delivery) with the delivery knobs set through Compose env.
set -u
LABEL=$1; BATCH=$2; CONC=$3; OUT=$4
REPO=${WORKDIR:?set WORKDIR to the folder holding the p99 clone}/p99
PY=/opt/homebrew/opt/python@3.12/bin/python3.12
DW=${DELIVERY_WORKERS:-1}
cd $REPO || exit 2
SNAP=$(dirname $OUT)/.snap-$LABEL; mkdir -p $SNAP

snapshot() {  # $1 = pre|post
  {
    print -r -- "{\"phase\":\"$1\",\"utc\":\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",\"local\":\"$(date +%H:%M:%S)\","
    print -r -- "\"loadavg\":\"$(sysctl -n vm.loadavg)\","
    print -r -- "\"test_processes\":$(ps -Ao pid,etime,command | grep -E '[p]ytest|[p]laywright|[g]olden_runner|[m]ake test|[v]itest|[r]un_saturation|[s]aturation_trial|[d]ocker.* build|[d]ocker compose|[d]ocker-compose|[b]uildx' | grep -v -E 'm11-|af-p99|grep|run_one.sh|compose_ls_sampler' | cut -c1-200 | $PY -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin]))'),"
    print -r -- "\"containers\":$(docker ps --format '{{json .}}' | $PY -c 'import json,sys; print(json.dumps([{k:r[k] for k in ("Names","Image","Status")} for r in map(json.loads,sys.stdin)]))'),"
    print -r -- "\"docker_stats\":$(docker stats --no-stream --format '{{json .}}' | $PY -c 'import json,sys; print(json.dumps([{k:r[k] for k in ("Name","CPUPerc","MemUsage")} for r in map(json.loads,sys.stdin)]))')"
    echo "}"
  } > $SNAP/$1.json
}

snapshot pre
export DELIVERY_BATCH=$BATCH DELIVERY_CONCURRENCY=$CONC
# Watch for this trial's delivery worker and record the env it actually received.
(
  for i in $(seq 1 240); do
    CMD=$(ls $OUT/m11-*/commands.json 2>/dev/null | head -1)
    if [ -n "$CMD" ]; then
      PROJ=$($PY -c "import json;print(json.load(open('$CMD'))['project'])")
      IDS=$(docker ps -q --filter "label=com.docker.compose.project=$PROJ" --filter "label=com.docker.compose.service=delivery-worker")
      if [ -n "$IDS" ]; then
        for id in ${(f)IDS}; do docker inspect $id; done | $PY -c '
import json,sys
raw=sys.stdin.read().replace("]\n[",",")
rows=json.loads(raw)
print(json.dumps([{"name":r["Name"],"image":r["Config"]["Image"],"env":[e for e in r["Config"]["Env"] if e.startswith("DELIVERY_")]} for r in rows],indent=2))' > $SNAP/delivery-env.json
        exit 0
      fi
    fi
    sleep 1
  done
) &
WATCH=$!
# Sample host load and foreign test processes every 5 s while the trial runs.
(
  while true; do
    print -r -- "{\"local\":\"$(date +%H:%M:%S)\",\"loadavg\":\"$(sysctl -n vm.loadavg)\",\"test_processes\":$(ps -Ao pid,command | grep -E '[p]ytest|[p]laywright|[g]olden_runner|[m]ake test|[v]itest|[d]ocker.* build|[d]ocker compose|[d]ocker-compose|[b]uildx' | grep -v -E 'm11-|af-p99|grep|compose_ls_sampler' | cut -c1-160 | $PY -c 'import json,sys; print(json.dumps([l.strip() for l in sys.stdin]))')}" >> $SNAP/during.jsonl
    sleep 5
  done
) &
SAMPLER=$!
$PY loadtest/run_saturation.py --output $OUT --rates 700 --keys 1000 --workers 4 --duration 60 \
  --critical-rate 2 --delivery-workers $DW --ingest-workers 2 --drain-seconds 20 --image-project af-p99-img \
  > $SNAP/runner.txt 2>&1
RC=$?
kill $SAMPLER 2>/dev/null
wait $WATCH 2>/dev/null
snapshot post
$PY - "$OUT" "$SNAP" "$LABEL" "$BATCH" "$CONC" "$RC" "$DW" <<'EOF'
import json, sys, pathlib
out, snap, label, batch, conc, rc, dw = sys.argv[1:]
snap = pathlib.Path(snap); out = pathlib.Path(out)
env = json.loads((snap / "delivery-env.json").read_text()) if (snap / "delivery-env.json").exists() else None
host = {"label": label, "requested": {"DELIVERY_BATCH": int(batch), "DELIVERY_CONCURRENCY": int(conc),
        "delivery_workers": int(dw)}, "runner_exit_code": int(rc),
        "delivery_worker_env_observed": env,
        "pre": json.loads((snap / "pre.json").read_text()), "post": json.loads((snap / "post.json").read_text()),
        "during_5s_samples": [json.loads(l) for l in (snap / "during.jsonl").read_text().splitlines() if l.strip()]
                             if (snap / "during.jsonl").exists() else []}
(out / "host.json").write_text(json.dumps(host, indent=2) + "\n")
(out / "runner-terminal.txt").write_text((snap / "runner.txt").read_text())
EOF
rm -rf $SNAP
echo "$LABEL done rc=$RC"
