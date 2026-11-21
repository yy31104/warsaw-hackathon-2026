#!/bin/zsh
# Every 30 s: local time, load average and running Compose projects (JSON lines).
S=${WORKDIR:?set WORKDIR to the folder holding the p99 clone}
while true; do
  print -r -- "{\"local\":\"$(date +%H:%M:%S)\",\"loadavg\":\"$(sysctl -n vm.loadavg)\",\"projects\":$(docker compose ls --format json)}" >> $S/compose-ls.jsonl
  sleep 30
done
