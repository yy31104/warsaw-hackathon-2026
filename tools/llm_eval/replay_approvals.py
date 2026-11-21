"""Apply the current approval policy to immutable benchmark outputs, without calling a model."""
import argparse
import json
from collections import defaultdict
from pathlib import Path

from alerting.assist.runbook_structure import approval_for
from .scoring import step_score


def replay(path):
    totals = defaultdict(lambda: dict(steps=0, required=0, missing_before=0,
                                     missing_after=0, added_reviews=0, missing_steps=0, exact_steps_after=0,
                                     expected_steps=0, unnecessary_reviews_after=0))
    for line in path.read_text().splitlines():
        row = json.loads(line)
        if row["task"] != "runbook" or row["product"] is None:
            continue
        before = row["actual"]
        expected = row["expected"]["steps"]
        # Preserve every generated text, parameter and action, changing only the approval flag.
        after = [{**s, "approval": approval_for(s["text"], s["action"])} for s in before]
        scores = step_score(expected, after)
        mode = totals[row["mode"]]
        mode["steps"] += len(before)
        mode["required"] += sum(s["approval"] == "required" for s in expected)
        mode["missing_before"] += row["product"]["risky_without_approval"]
        mode["missing_after"] += scores["risky_without_approval"]
        mode["missing_steps"] += scores["missing_risky_steps"]
        mode["exact_steps_after"] += scores["steps"][0]
        mode["expected_steps"] += scores["steps"][1]
        mode["unnecessary_reviews_after"] += sum(s["approval"] == "required" and e["approval"] == "none"
                                                 for s, e in zip(after, expected) if s["text"] == e["text"])
        mode["added_reviews"] += sum(a["approval"] == "required" and b["approval"] != "required"
                                     for b, a in zip(before, after))
    return dict(totals)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("cases_jsonl", type=Path)
    args = parser.parse_args()
    print(json.dumps(replay(args.cases_jsonl), indent=2))
