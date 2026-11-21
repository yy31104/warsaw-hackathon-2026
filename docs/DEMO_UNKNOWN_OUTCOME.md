# Demo a runbook action with an unknown outcome

The action is **Retry workflow**. The simulator commits its effect and receipt, then holds the response for four seconds. The API stops waiting after three seconds and marks that same run **Unknown · retry locked**. **Check receipt** fetches the existing result.

## Set up before recording

From this checkout, start the real Compose stack with demo mode enabled. These commands use the default ports. If the demo stack uses custom ports, use the three corresponding loopback URLs below.

```sh
DEMO_MODE=1 ASSIST_ENABLED=0 docker compose up -d --build --wait
API=http://127.0.0.1:8000
UI=http://127.0.0.1:5173
OPS=http://127.0.0.1:8200

EVENT_ID=$(
  python3 -c 'import json,uuid; from pathlib import Path; event=json.loads(Path("fixtures/events/workflow_failed.json").read_text()); tag=uuid.uuid4().hex[:8]; event.update(workflowId="DEMO-"+tag,step="verify-unknown-"+tag); print(json.dumps(event))' |
  curl -fsS "$API/v1/events/generic-webhook" -H 'X-Api-Key: demo-generic-key' \
    -H 'Content-Type: application/json' --data-binary @- |
  python3 -c 'import json,sys; print(json.load(sys.stdin)["event_ids"][0])'
)
ALERT_ID=$(python3 - "$API" "$EVENT_ID" <<'PY'
import json
import sys
import time
from urllib.request import urlopen

api, event_id = sys.argv[1:]
for _ in range(50):
    with urlopen(f"{api}/v1/events/{event_id}/explain", timeout=3) as response:
        decisions = json.load(response)["decisions"]
    for row in decisions:
        if row["rule_id"] == "settlements-wf-failed" and row.get("alert_id"):
            print(row["alert_id"])
            raise SystemExit(0)
    time.sleep(.2)
raise SystemExit("The settlement incident is not ready")
PY
)
printf '%s/a/%s#runbook\n' "$UI" "$ALERT_ID"

curl -fsS "$OPS/demo/delay-next" -H 'Content-Type: application/json' \
  -d '{"action":"retry_workflow"}'
```

Open the printed incident URL in the live console. Select **Sam** in **Viewing as** and bring the **Retry workflow DEMO-…** row into view. The arm is one-shot. Repeat setup to get a fresh incident and workflow for each take.

## Record 20 seconds

| Time | Show |
| --- | --- |
| 0–5 s | Click **Run** on **Retry workflow DEMO-…**. After about three seconds the step becomes **Unknown · retry locked**. |
| 5–10 s | Show the **Check receipt** button and its `action-<id>` receipt key. |
| 10–14 s | Click **Check receipt**. The same step becomes **Succeeded**. |
| 14–20 s | Show the receiver's **Succeeded** receipt and **attempt = 1** using the prepared terminal command. |

Prepare this command before recording and press Enter at the last cut. The runbook's step index 1 supplies the same ID shown in the UI.

```sh
RUN_ID=$(curl -fsS "$API/v1/alerts/$ALERT_ID/runbook" |
  python3 -c 'import json,sys; print(next(s["run_id"] for s in json.load(sys.stdin)["steps"] if s["index"]==1))')
curl -fsS "$OPS/executions/action-$RUN_ID" |
  python3 -c 'import json,sys; r=json.load(sys.stdin); print("action-{} | {} | attempt = {}".format(sys.argv[1], r["state"], r["result"]["output"]["attempt"]))' "$RUN_ID"
```

The alert timeline records one `action.execute` and the operator's `action.reconcile`. A missing receipt leaves the step **Unknown** and displays the reason. The demo resolves the execution outcome on this step. The settlement incident can remain open.
