import json
from pathlib import Path

import yaml

from alerting.config import files


async def test_http_identity_and_unknown_event_is_readable(client):
    url = "/v1/events/generic-webhook"
    headers = {"X-Api-Key": "demo-generic-key", "Idempotency-Key": "http-42", "X-Gen-Sent-At": "1792833245000"}
    payload = {"kind": "sensor.new", "room": {"n": 42}}
    assert (await client.post(url, json=payload)).status_code == 401
    first = await client.post(url, json=payload, headers=headers)
    assert first.status_code == 202
    event_id, = first.json()["event_ids"]
    second = await client.post(url, json=dict(reversed(payload.items())), headers=headers)
    assert second.json() == {"accepted": 0, "duplicates": 1, "conflicts": 0, "rejected": [], "event_ids": [event_id]}
    changed = await client.post(url, json=dict(payload, kind="changed"), headers=headers)
    assert changed.json()["conflicts"] == 1 and changed.json()["event_ids"] == [event_id]
    stored = (await client.get(f"/v1/events/{event_id}")).json()
    assert stored["raw"] == payload and stored["fields"]["room.n"] == 42
    assert stored["status"] == "needs_review" and stored["source_event_id"] == "http-42"
    assert stored["trace"]["gen_sent_at"] == 1792833245000


async def test_wrapped_array_and_per_element_keys(client):
    raw = json.loads((Path(__file__).resolve().parents[3] / "fixtures/events/alertmanager_high_error_rate.json").read_text())
    headers = {"X-Api-Key": "demo-alertmanager-key", "Idempotency-Key": "batch-1"}
    batch = {"alerts": [raw, dict(raw, status="resolved"), 17]}
    url = "/v1/events/alertmanager"
    first = (await client.post(url, json=batch, headers=headers)).json()
    assert first["accepted"] == 2 and first["conflicts"] == 0 and first["rejected"][0]["index"] == 2
    assert len(set(first["event_ids"])) == 2
    again = (await client.post(url, json=batch["alerts"], headers=headers)).json()
    assert again["duplicates"] == 2 and again["event_ids"] == first["event_ids"]
    missing = await client.get("/v1/events/999999")
    assert missing.status_code == 404


async def test_http_bad_envelope_and_size_are_reported(client):
    url = "/v1/events/alertmanager"
    headers = {"X-Api-Key": "demo-alertmanager-key"}
    assert (await client.post(url, json={"alerts": {}}, headers=headers)).status_code == 400
    assert (await client.post(url, content=b"broken", headers=headers)).status_code == 400
    assert (await client.post(url, content=b"x" * 262145, headers=headers)).status_code == 413


async def test_next_event_uses_new_schema_config_without_restart(client, tmp_path, monkeypatch):
    (tmp_path / "sources.yaml").write_bytes((files.CONFIG_DIR / "sources.yaml").read_bytes())
    (tmp_path / "teams.yaml").write_bytes((files.CONFIG_DIR / "teams.yaml").read_bytes())
    registry = tmp_path / "schemas.yaml"
    registry.write_text("schemas: []", encoding="utf-8")
    monkeypatch.setattr(files, "CONFIG_DIR", tmp_path)
    headers = {"X-Api-Key": "demo-generic-key"}
    raw = {"unit": {"station": "boiler-4"}, "sensorState": "HOT"}

    async def send():
        response = await client.post("/v1/events/generic-webhook", headers=headers, json=raw)
        event_id, = response.json()["event_ids"]
        return (await client.get(f"/v1/events/{event_id}")).json()

    original = await send()
    assert original["status"] == "needs_review" and original["raw"] == raw
    mapping = {"id": "temperature", "name": "sensor.temperature", "detect": {"field": "sensorState", "op": "exists"},
               "aliases": {"service": "unit.station", "severity": "sensorState"}, "severity_map": {"HOT": "critical"}}
    registry.write_text(yaml.safe_dump({"schemas": [mapping]}), encoding="utf-8")
    known = await send()
    assert (known["status"], known["service"], known["severity"]) == ("pending", "boiler-4", "critical")
    assert known["fields"]["$schema"] == "sensor.temperature"
