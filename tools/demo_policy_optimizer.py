"""Replay the bundled policy optimizer demo in memory."""

from datetime import datetime, timedelta
from pathlib import Path

import yaml

from alerting.config import files
from alerting.contracts import CanonicalEvent
from alerting.simulate.optimizer import optimize_history


def main():
    fixture_path = Path(__file__).resolve().parents[1] / "fixtures/protected/optimizer_demo.yaml"
    fixture = yaml.safe_load(fixture_path.read_text(encoding="utf-8"))
    rule, history = fixture["rule"], fixture["history"]
    start = datetime.fromisoformat(history["first_at"])
    events = [
        CanonicalEvent(
            id=i + 1, source_id="optimizer-fixture", transport="http", payload_hash=f"optimizer-{i}",
            received_at=start + timedelta(seconds=i * history["every_s"]),
            raw={}, fields=history["fields"], severity=history["fields"]["$severity"], status="processed",
        )
        for i in range(history["count"])
    ]
    report = optimize_history(events, rule, rule, files.schemas(), files.directory(), files.runbooks())
    print(f'{report["evaluated_count"]} options evaluated on {report["events_replayed"]} synthetic events.')
    best = report["options"][0]
    saved = best["savings"]
    print(f'{best["id"]} reduced planned interruptions by {saved["planned_interruptions"]} '
          f'({saved["night_interruptions"]} at night). '
          f'{best["protected_violations"]} violations in {best["protected_cases"]} protected cases.')
    rejected = report["best_rejected"]
    example = rejected["counterexample"]
    print(f'{rejected["id"]} rejected. The planned {example["channel"]} for {example["recipient"]} '
          f'is {example["kind"]} in episode {example["episode_id"]}.')


if __name__ == "__main__":
    main()
