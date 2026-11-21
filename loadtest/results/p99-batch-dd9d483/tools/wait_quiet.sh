#!/bin/zsh
# Exit 0 once no test/golden/playwright process has been seen for 3 checks 10 s apart; give up after $1 checks.
MAX=${1:-170}; quiet=0
for i in $(seq 1 $MAX); do
  busy=$(ps -Ao pid,command | grep -E '[p]ytest|[p]laywright|[g]olden_runner|[m]ake test|[v]itest|[s]aturation_trial|[d]ocker.* build|[d]ocker compose|[d]ocker-compose|[b]uildx' | grep -v -E 'wait_quiet|m11-|af-p99' | cut -c1-140)
  load1=$(sysctl -n vm.loadavg | awk '{print $2}'); if [ -n "$busy" ] || [ ${load1%.*} -ge ${LOADMAX:-9} ]; then quiet=0; else quiet=$((quiet+1)); fi
  if [ $quiet -ge ${NEED:-6} ]; then echo "QUIET $(date +%H:%M:%S) load $(sysctl -n vm.loadavg)"; exit 0; fi
  sleep 10
done
echo "STILL BUSY $(date +%H:%M:%S) load $(sysctl -n vm.loadavg)"; echo "$busy"; exit 1
