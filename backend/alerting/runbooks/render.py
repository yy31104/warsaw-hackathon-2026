import re

from alerting.runbooks.actions import REGISTRY
from alerting.runbooks.match import yaml_spec
from alerting.templates import render as template


def fill(value, fields):
    if isinstance(value, dict):
        return {k: fill(v, fields) for k, v in value.items()}
    if isinstance(value, list):
        return [fill(v, fields) for v in value]
    return template(value, fields) if isinstance(value, str) else value


def render(book, fields, runs=(), approvers=()):
    content = book["content"]
    fmt = book.get("format", "text")
    steps = []
    if fmt == "yaml-steps":
        for raw in yaml_spec(content).get("steps", []):
            data = fill(raw, fields)
            name = data.get("action")
            handler = REGISTRY.get(name)
            params = {
                k: v
                for k, v in data.items()
                if k not in ("action", "check", "if", "text", "approval", "resolves_alert")
            }
            label = (
                data.get("text")
                or data.get("check")
                or " ".join(
                    [str(name or "Manual step").replace("_", " "), *map(str, params.values())]
                )
            )
            if "if" in data:
                label = f"If {data['if']}: {label}"
            kind = (
                "manual"
                if not handler
                else "check"
                if "check" in data
                else "escalate"
                if name == "escalate"
                else "action"
            )
            steps.append(
                dict(
                    kind=kind,
                    text=label,
                    action=name if handler else None,
                    params=params,
                    approval=data.get("approval", handler.default_approval if handler else "none"),
                    resolves_alert=data.get("resolves_alert") is True,
                )
            )
    elif fmt in ("markdown", "checklist"):
        for line in content.splitlines():
            found = re.match(r"^\s*(?:\d+[.)]\s+|(?:[-*]\s+)?\[[ xX]\]\s+)(.*)$", line)
            if found:
                label = template(found[1], fields)
                steps.append(
                    dict(
                        kind="manual",
                        text=label,
                        approval="required" if "approval" in label.lower() else "none",
                    )
                )
    elif fmt == "url":
        url = template(content.strip(), fields)
        steps.append(dict(kind="link", url=url, text=url, approval="none"))
    else:
        steps.append(dict(kind="manual", text=template(content.strip(), fields), approval="none"))
    failures = sum(r["action"] == "retry_workflow" and r["state"] == "failed" for r in runs)
    for i, step in enumerate(steps):
        history = [r for r in runs if r["step_index"] == i]
        latest = history[-1] if history else {}
        step.update(
            index=i,
            state=latest.get("state"),
            run_id=latest.get("run_id"),
            approved_by=latest.get("approved_by"),
            runs=len(history),
            execution_count=sum(r["execution_count"] for r in history),
        )
        if step["state"] == "unknown":
            step["requested_by"] = latest.get("requested_by")
        if step["approval"] == "required" or step["state"] == "unknown":
            step["approvers"] = list(approvers)
        if step.get("action") == "escalate":
            step["suggested"] = failures >= 2
    return dict(
        runbook_id=book["id"],
        format=fmt,
        source_text=content,
        steps=steps,
        can_structure=fmt == "text",
    )
