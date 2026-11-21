#!/bin/zsh
# Usage: run_seq.sh label:batch:conc ...  Each trial waits for a quiet host first (deadline 07:55 local).
S=${WORKDIR:?set WORKDIR to the folder holding the p99 clone}
R=$S/p99/loadtest/results/p99-batch-dd9d483
for spec in "$@"; do
  IFS=: read label b c <<< "$spec"
  if [ "$(date +%H%M)" -ge 0755 ]; then echo "$label SKIPPED deadline $(date +%H:%M:%S)"; continue; fi
  LOADMAX=9 NEED=3 $S/wait_quiet.sh 120 || { echo "$label NOT_RUN busy $(date +%H:%M:%S)"; continue; }
  echo "$label start $(date +%H:%M:%S)"
  $S/run_one.sh $label $b $c $R/$label
  python3 $S/p99/loadtest/results/p99-batch-dd9d483/tools/analyze.py $R/$label/m11-* > $R/$label/analysis.json 2>&1
  python3 -c "
import json; d=json.load(open('$R/$label/analysis.json')); l=d['receiver_latency']; m=d['slowest20_normal']['median_ms']
print('$label', 'normal', l['normal'], 'critical', l['critical'], 'dup', d['duplicate_receipts'], 'p2c', round(m['processed_to_first_claim_ms']), 'done', '$(date +%H:%M:%S)')"
done
