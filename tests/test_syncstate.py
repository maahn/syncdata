import time

import pytest

import syncstate


def testStatusWriteIsAtomicAndReadsBack(sandbox):
    syncstate.writeStatus("inst@site", {"result": "ok", "bytes": 5})
    status = syncstate.readStatus("inst@site")
    assert status["result"] == "ok"
    assert status["bytes"] == 5
    assert status["hostname"]  # a PID is only meaningful with its hostname
    assert status["pid"]
    assert status["instrument"] == "inst@site"
    # no temporary files are left behind
    assert syncstate.allStatuses().keys() == {"inst@site"}


def testStatusWriteNeverRaises(sandbox, monkeypatch, capsys):
    def fail(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(syncstate.os, "replace", fail)
    syncstate.writeStatus("inst@site", {"result": "ok"})
    assert "disk full" in capsys.readouterr().out
    assert syncstate.readStatus("inst@site") is None


def testAllStatusesSkipsAlertsStateAndUnreadableFiles(sandbox):
    syncstate.writeStatus("inst@site", {"result": "ok"})
    syncstate.writeStatus("alerts", {"endpoints": {}})
    (sandbox / "state" / "broken.json").write_text("{not json")
    statuses = syncstate.allStatuses()
    assert statuses.keys() == {"inst@site"}


def testEventsAppendAndReadBack(sandbox):
    syncstate.appendEvent("sync_start", "a@site", "cron month")
    syncstate.appendEvent("sync_done", "a@site", "ok, 5 bytes")
    syncstate.appendEvent("locked", "b@site", "lock busy")
    events = syncstate.readEvents(2)
    assert [event["kind"] for event in events] == ["sync_done", "locked"]
    assert events[0]["instrument"] == "a@site"


def testEventWriteNeverRaises(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(syncstate.os, "makedirs", fail)
    syncstate.appendEvent("sync_start", "a@site", "text")
    assert "failed to write event" in capsys.readouterr().out


def fail(*args, **kwargs):
    raise OSError("no")


def testHeartbeatFreshParsesIso(sandbox):
    now = syncstate.utcNow()
    fresh = {"heartbeat": now.isoformat()}
    stale = {"heartbeat": syncstate.parseIso(now.isoformat()).isoformat()}
    assert syncstate.heartbeatFresh(fresh, now)
    assert not syncstate.heartbeatFresh(None, now)
    assert not syncstate.heartbeatFresh({"heartbeat": "not a time"}, now)


def testHeartbeatThreadKeepsStatusFresh(sandbox, monkeypatch):
    monkeypatch.setattr(syncstate, "HEARTBEAT_INTERVAL", 0.05)
    tracker = syncstate.StatusTracker("inst@site", source="hand", mode="day")
    tracker.begin()
    first = syncstate.readStatus("inst@site")
    assert first["running"] is True
    assert first["result"] == "running"
    time.sleep(0.2)
    tracker.update(currentPath="/remote/2026/11/06/")
    second = syncstate.readStatus("inst@site")
    assert second["heartbeat"] >= first["heartbeat"]
    assert second["currentPath"] == "/remote/2026/11/06/"
    tracker.noteBytes()
    assert syncstate.readStatus("inst@site")["lastDataAt"]
    tracker.finish(bytes=1234, result="ok")
    final = syncstate.readStatus("inst@site")
    assert final["running"] is False
    assert final["result"] == "ok"
    assert final["bytes"] == 1234
    assert final["finished"]


def testFinishStopsTheHeartbeat(sandbox, monkeypatch):
    monkeypatch.setattr(syncstate, "HEARTBEAT_INTERVAL", 0.05)
    tracker = syncstate.StatusTracker("inst@site", source="hand", mode="day")
    tracker.begin()
    tracker.finish(result="ok")
    last = syncstate.readStatus("inst@site")["heartbeat"]
    time.sleep(0.2)
    assert syncstate.readStatus("inst@site")["heartbeat"] == last


def testFinishAfterFailureAlsoRuns(sandbox, monkeypatch):
    # CallOnComplete runs after failed syncs; the tracker must finish too
    monkeypatch.setattr(syncstate, "HEARTBEAT_INTERVAL", 0.05)
    tracker = syncstate.StatusTracker("inst@site", source="cron", mode="day")
    tracker.begin()
    tracker.finish(result="connection", error="Connection refused")
    status = syncstate.readStatus("inst@site")
    assert status["result"] == "connection"
    assert status["error"] == "Connection refused"
    assert status["running"] is False


def testLockedDoesNotHideAnActiveRun(sandbox):
    # the lock is held by another process; its status must stay visible
    syncstate.writeStatus(
        "inst@site", {"running": True, "heartbeat": syncstate.utcNow().isoformat()}
    )
    tracker = syncstate.StatusTracker("inst@site", source="hand", mode="day")
    tracker.locked()
    status = syncstate.readStatus("inst@site")
    assert status["running"] is True
    assert "result" not in status


def testLockedWritesASnapshotWithoutAnActiveRun(sandbox):
    tracker = syncstate.StatusTracker("inst@site", source="cron", mode="day")
    tracker.locked()
    status = syncstate.readStatus("inst@site")
    assert status["result"] == "locked"
    assert status["running"] is False


def testBeginCarriesLastDataForward(sandbox):
    syncstate.writeStatus("inst@site", {"lastDataAt": "2026-11-05T10:00:00+00:00"})
    syncstate.StatusTracker("inst@site", source="cron", mode="day").begin()
    status = syncstate.readStatus("inst@site")
    assert status["lastDataAt"] == "2026-11-05T10:00:00+00:00"


def testTrimEventsKeepsTheTail(sandbox, monkeypatch):
    monkeypatch.setattr(syncstate, "MAX_EVENTS_BYTES", 200)
    for i in range(20):
        syncstate.appendEvent("sync_done", "inst@site", "event %i" % i)
    events = syncstate.readEvents(100)
    assert len(events) > 0
    assert events[-1]["text"] == "event 19"
