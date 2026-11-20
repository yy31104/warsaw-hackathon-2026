from .test_worker import Settings, process_batch, seed


async def test_feed_and_audit(api, database):
    await seed(database)
    await process_batch(database, Settings.load())
    feed = (await api.get("/v1/alerts?team=settlements&status=open&q=TIMEOUT")).json()
    assert feed["total"] == 1
    path = "/v1/alerts/" + str(feed["items"][0]["id"])
    detail = (await api.get(path)).json()
    assert detail["occurrences"] and detail["timeline"]
    assert (await api.post(path + "/ack", headers={"X-User": "rita"})).status_code == 403
    assert (await api.post(path + "/ack", headers={"X-User": "sam"})).json()[
        "status"
    ] == "acknowledged"
    assert len((await api.get(path + "/notifications")).json()["items"]) == 3
    assert len((await api.get("/v1/notifications?state=delivered")).json()["items"]) == 1
    assert (await api.post(path + "/resolve", headers={"X-User": "sam"})).json()[
        "status"
    ] == "resolved"
    assert any(x["kind"] == "audit" for x in (await api.get(path)).json()["timeline"])
