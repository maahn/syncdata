import datetime
import json
import os
import time

import pytest

import alerts
import syncdata
import syncstate

NOW = datetime.datetime(2026, 11, 6, 12, 0, 0, tzinfo=datetime.timezone.utc)


def later(hours=0, minutes=0):
    return NOW + datetime.timedelta(hours=hours, minutes=minutes)


@pytest.fixture(autouse=True)
def dryRun(sandbox, monkeypatch):
    monkeypatch.setattr(syncdata, "CONFIG", {"mattermostWebhookUrl": "https://x.invalid/hook"})
    return sandbox


def config(**overrides):
    base = {
        "instrument": "visss3_leader_data@hyy",
        "user": "user",
        "host": "hyy.invalid",
        "sshOptions": "-p 2204",
        "rsyncOptions": "",
        "remotePath": "/remote",
        "localPath": "data",
        "monthString": "%Y/%m",
        "dayString": "%d",
        "channel": "testchannel",
        "group": "",
        "CallOnComplete": "",
        "CallOnCompleteLog": "",
        "CallOnCompleteWait": False,
        "CallOnCompleteSSH": "",
        "CallOnCompleteTimeout": 0,
        "hoursForDaySync": list(range(24)),
        "hoursForMonthSync": [],
    }
    base.update(overrides)
    return base


def outcome(endpoint, ok, key, reason="Connection refused", instrument="a@site"):
    return {
        "endpoint": endpoint,
        "ok": ok,
        "reason": reason,
        "instrument": instrument,
        "key": key,
    }


def status(result, **overrides):
    base = {
        "instrument": "a@site",
        "result": result,
        "running": False,
        "finished": NOW.isoformat(),
        "heartbeat": NOW.isoformat(),
        "lastDataAt": NOW.isoformat(),
    }
    base.update(overrides)
    return base


# ------------------------------------------------------------ state machine


def testOneFailureDoesNotOpenADisturbance():
    state = alerts.loadState()
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    assert state["endpoints"]["hyy.invalid:2204"]["open"] is False


def testTwoFailuresInARowOpenADisturbance():
    state = alerts.loadState()
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    assert state["endpoints"]["hyy.invalid:2204"]["open"] is True
    assert state["endpoints"]["hyy.invalid:2204"]["openedAt"] == later(minutes=5).isoformat()


def testAnOutcomeIsCountedOnlyOnce():
    state = alerts.loadState()
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], later(minutes=1))
    assert state["endpoints"]["hyy.invalid:2204"]["consecutiveFailures"] == 1


def testTwoSuccessesCloseADisturbanceWithDuration():
    state = alerts.loadState()
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    resolves = alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", True, "3")], later(hours=14))
    resolves += alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", True, "4")], later(hours=14, minutes=1))
    assert state["endpoints"]["hyy.invalid:2204"]["open"] is False
    assert len(resolves) == 1
    # opened 5 min after NOW, resolved 14 h after NOW -> 13 h floored
    assert "13 h" in resolves[0][1]


def testPartialCountsAsFailureAndRemoteDirMissingAsSuccess():
    state = alerts.loadState()
    alerts.applyOutcomes(
        state, [outcome("hyy.invalid:2204", False, "1", reason="rsync exit code 30")], NOW
    )
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", True, "2", reason="ok")], NOW)
    assert state["endpoints"]["hyy.invalid:2204"]["consecutiveFailures"] == 0


def testLockedIsNeverADisturbance():
    state = alerts.loadState()
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", True, "1", reason="locked")], NOW)
    assert state["endpoints"]["hyy.invalid:2204"]["consecutiveFailures"] == 0


# ----------------------------------------------------------------- messages


def testMessagesAreOnlyLoggedInDryRun(sandbox, capsys, posts):
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config()}, later(minutes=5))
    assert posts == []
    assert "[alerts dry-run] would send to testchannel" in capsys.readouterr().out
    assert state["endpoints"]["hyy.invalid:2204"]["lastMessageAt"]


def testArmedAlertsPostToMattermost(sandbox, monkeypatch, posts, capsys):
    monkeypatch.setattr(
        syncdata, "CONFIG", {"alertsDryRun": False, "mattermostWebhookUrl": "https://x.invalid/hook"}
    )
    state = alerts.loadState()
    state["dryRun"] = False
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config()}, later(minutes=5))
    assert len(posts) == 1
    assert posts[0]["channel"] == "testchannel"
    assert "seit" in posts[0]["text"]
    assert "nicht erreichbar" in posts[0]["text"]
    assert posts[0]["username"] == "syncdata"


def testReminderOnlyEvery24Hours(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config()}, later(minutes=5))
    assert "would send" in capsys.readouterr().out
    alerts.sendDueMessages(state, {"a@site": config()}, later(hours=1))
    assert "would send" not in capsys.readouterr().out
    alerts.sendDueMessages(state, {"a@site": config()}, later(hours=25))
    assert "would send" in capsys.readouterr().out


def testAcknowledgedDisturbancesDoNotRemind(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config()}, later(minutes=5))
    capsys.readouterr()
    alerts.acknowledgeAll(later(minutes=6))
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.sendDueMessages(state, {"a@site": config()}, later(hours=30))
    assert "would send" not in capsys.readouterr().out


def testResolveMessageEvenWhenAcknowledged(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config()}, later(minutes=5))
    alerts.saveState(state)  # persist the open disturbance, as evaluate() does
    alerts.acknowledgeAll(later(minutes=6))
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", True, "3")], later(hours=30))
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", True, "4")], later(hours=30, minutes=1))
    assert "wieder erreichbar" in capsys.readouterr().out


def testMuteSuppressesMessagesButNotLogging(sandbox, capsys, posts):
    state = alerts.loadState()
    state["dryRun"] = False
    alerts.mute(NOW)
    state = alerts.loadState()
    state["dryRun"] = False
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config()}, later(minutes=5))
    assert posts == []
    assert "alerts muted until" in capsys.readouterr().out


def testMessageWithoutChannelIsLoggedOnly(sandbox, capsys, posts):
    state = alerts.loadState()
    state["dryRun"] = False
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "1")], NOW)
    alerts.applyOutcomes(state, [outcome("hyy.invalid:2204", False, "2")], later(minutes=5))
    alerts.sendDueMessages(state, {"a@site": config(channel="")}, later(minutes=5))
    assert posts == []
    assert "<no channel>" in capsys.readouterr().out


def testEndpointsOfOneHostAreGroupedInOneMessage(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    for i in range(2):
        alerts.applyOutcomes(
            state,
            [outcome("hyy.invalid:2204", False, "%ia" % i), outcome("hyy.invalid:2205", False, "%ib" % i)],
            later(minutes=5 * i),
        )
    alerts.sendDueMessages(
        state,
        {
            "visss3_leader_data@hyy": config(sshOptions="-p 2204"),
            "visss3_sonic_data@hyy": config(sshOptions="-p 2205"),
        },
        later(minutes=10),
    )
    out = capsys.readouterr().out
    assert "hyy :2204/:2205" in out


# ------------------------------------------------------------- no data / hang


def testNoDataAlarmAfter48Hours(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    statuses = {"a@site": status("ok", lastDataAt=NOW.isoformat())}
    configs = {"a@site": config()}
    alerts.checkNoData(state, configs, statuses, later(hours=1))
    assert "would send" not in capsys.readouterr().out
    alerts.checkNoData(state, configs, statuses, later(hours=49))
    assert "ohne neue Daten" in capsys.readouterr().out


def testNoDataResolveWhenDataReturns(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    configs = {"a@site": config()}
    alerts.checkNoData(
        state, configs, {"a@site": status("ok", lastDataAt=NOW.isoformat())}, later(hours=49)
    )
    capsys.readouterr()
    fresh = later(hours=50)
    alerts.checkNoData(
        state, configs, {"a@site": status("ok", lastDataAt=fresh.isoformat())}, fresh
    )
    assert "wieder neue Daten" in capsys.readouterr().out


def testNoDataAlarmSparesARunningSync(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    configs = {"a@site": config()}
    statuses = {
        "a@site": status(
            "running",
            running=True,
            heartbeat=later(hours=50).isoformat(),
            start=later(hours=49).isoformat(),
            lastDataAt=NOW.isoformat(),
        )
    }
    alerts.checkNoData(state, configs, statuses, later(hours=50))
    assert "would send" not in capsys.readouterr().out


def testNoDataAlarmIgnoresInstrumentsWithoutData(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    alerts.checkNoData(
        state, {"a@site": config()}, {"a@site": status("ok", lastDataAt=None)}, later(hours=49)
    )
    assert "would send" not in capsys.readouterr().out


# ------------------------------------------------------------ realtime age

def realLater(hours=0):
    """Real current time plus hours (realtime tests compare real mtimes)."""
    return syncstate.utcNow() + datetime.timedelta(hours=hours)


def freshQuicklook(sandbox, ageHours=0):
    """Create a fetched realtime file with a controllable age."""
    dir_ = sandbox / "quicklooks"
    dir_.mkdir(exist_ok=True)
    target = dir_ / "latest.jpg"
    target.write_text("jpg")
    stamp = time.time() - ageHours * 3600
    os.utime(target, (stamp, stamp))
    return dir_


def realtimeConfig(sandbox, **overrides):
    return config(
        realtimePath=str(sandbox / "quicklooks"),
        realtimeFiles=["/remote/latest.jpg", "/remote/status.txt"],
        **overrides,
    )


def testRealtimeAlarmWhenQuicklooksAreStale(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    freshQuicklook(sandbox, ageHours=3)
    configs = {"a@site": realtimeConfig(sandbox)}
    statuses = {"a@site": status("ok")}
    alerts.checkRealtimeAge(state, configs, statuses, realLater())
    assert "ohne neue Quicklooks" in capsys.readouterr().out
    assert state["realtime"]["a@site"]["open"] is True


def testRealtimeIsQuietWhenQuicklooksAreFresh(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    freshQuicklook(sandbox, ageHours=0.5)
    configs = {"a@site": realtimeConfig(sandbox)}
    statuses = {"a@site": status("ok")}
    alerts.checkRealtimeAge(state, configs, statuses, realLater())
    assert "would send" not in capsys.readouterr().out
    assert state["realtime"]["a@site"]["open"] is False


def testRealtimeResolveWhenQuicklooksReturn(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    configs = {"a@site": realtimeConfig(sandbox)}
    statuses = {"a@site": status("ok")}
    freshQuicklook(sandbox, ageHours=3)
    alerts.checkRealtimeAge(state, configs, statuses, realLater())
    capsys.readouterr()
    freshQuicklook(sandbox, ageHours=0.1)  # new fetch updates the mtime
    alerts.checkRealtimeAge(state, configs, statuses, realLater(hours=0.1))
    assert "wieder neue Quicklooks" in capsys.readouterr().out
    assert state["realtime"]["a@site"]["open"] is False


def testRealtimeAlarmSparesARunningFetch(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    freshQuicklook(sandbox, ageHours=5)
    configs = {"a@site": realtimeConfig(sandbox)}
    statuses = {
        "a@site": status(
            "running",
            running=True,
            heartbeat=realLater().isoformat(),
            start=realLater().isoformat(),
        )
    }
    alerts.checkRealtimeAge(state, configs, statuses, realLater())
    assert "would send" not in capsys.readouterr().out


def testRealtimeAlarmIgnoresMissingFiles(sandbox, capsys):
    # nothing fetched yet: nothing to compare against
    state = alerts.loadState()
    state["dryRun"] = True
    configs = {"a@site": realtimeConfig(sandbox)}
    statuses = {"a@site": status("ok")}
    alerts.checkRealtimeAge(state, configs, statuses, realLater())
    assert "would send" not in capsys.readouterr().out


def testRealtimeMaxAgeIsConfigurable(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    freshQuicklook(sandbox, ageHours=1)
    configs = {"a@site": realtimeConfig(sandbox, realtimeMaxAge=1800)}  # 30 min
    alerts.checkRealtimeAge(state, configs, {"a@site": status("ok")}, realLater())
    assert "ohne neue Quicklooks" in capsys.readouterr().out


def testRealtimeAlarmIsOnlyLoggedInDryRun(sandbox, capsys, posts):
    state = alerts.loadState()
    state["dryRun"] = True
    freshQuicklook(sandbox, ageHours=3)
    alerts.checkRealtimeAge(
        state, {"a@site": realtimeConfig(sandbox)}, {"a@site": status("ok")}, realLater()
    )
    assert posts == []
    assert "[alerts dry-run]" in capsys.readouterr().out


def testHangingRunAlarm(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    configs = {"a@site": config()}
    statuses = {
        "a@site": status(
            "running",
            running=True,
            heartbeat=later(hours=3).isoformat(),
            start=NOW.isoformat(),
        )
    }
    alerts.checkHanging(state, configs, statuses, later(hours=3))
    assert "hängt möglicherweise" in capsys.readouterr().out


def testNormalRunDoesNotHang(sandbox, capsys):
    state = alerts.loadState()
    state["dryRun"] = True
    configs = {"a@site": config()}
    statuses = {
        "a@site": status(
            "running",
            running=True,
            heartbeat=later(minutes=30).isoformat(),
            start=NOW.isoformat(),
        )
    }
    alerts.checkHanging(state, configs, statuses, later(minutes=30))
    assert "would send" not in capsys.readouterr().out


# ------------------------------------------------------------------- evaluate


def writeStatusFile(name, fields):
    syncstate.writeStatus(name, fields)


def testEvaluateReadsStatusesAndStates(dryRun):
    writeStatusFile(
        "a@site",
        status("connection", error="Connection refused", instrument="a@site"),
    )
    (dryRun / "instruments").mkdir(exist_ok=True)
    import json

    (dryRun / "instruments" / "a@site.json").write_text(json.dumps(config()))
    state = alerts.evaluate({"a@site": config()}, now=NOW)
    assert state["counted"]["a@site"]
    # a single failed outcome does not open the disturbance yet
    assert state["endpoints"]["hyy.invalid:2204"]["open"] is False


def testEvaluateOpensAfterSecondFailure(dryRun):
    import json

    (dryRun / "instruments").mkdir(exist_ok=True)
    (dryRun / "instruments" / "a@site.json").write_text(json.dumps(config()))
    writeStatusFile(
        "a@site",
        status("connection", error="Connection refused", instrument="a@site"),
    )
    alerts.evaluate({"a@site": config()}, now=NOW)
    second = NOW + datetime.timedelta(hours=1)
    writeStatusFile(
        "a@site",
        status(
            "connection",
            error="Connection refused",
            instrument="a@site",
            finished=second.isoformat(),
            heartbeat=second.isoformat(),
        ),
    )
    state = alerts.evaluate({"a@site": config()}, now=second)
    assert state["endpoints"]["hyy.invalid:2204"]["open"] is True


# --------------------------------------------------------------------- replay


def testParseHistoryLogsDetectsFailuresByMissingOk(sandbox):
    logsDir = sandbox / "logs"
    logsDir.mkdir()
    (logsDir / "syncdata_2026-11-05.log").write_text(
        "[2026-11-05 10:00:00 UTC] connecting to ssh -o ConnectTimeout=45 user@hyy.invalid -p 2204 exit, lockfile /x/syncdata.a.lock\n"
        "[2026-11-05 10:00:02 UTC] connection o.k.\n"
        "[2026-11-05 11:00:00 UTC] connecting to ssh -o ConnectTimeout=45 user@hyy.invalid -p 2204 exit, lockfile /x/syncdata.a.lock\n"
        "[2026-11-05 11:00:31 UTC] connecting to ssh -o ConnectTimeout=45 user@hyy.invalid -p 2205 exit, lockfile /x/syncdata.b.lock\n"
        "[2026-11-05 11:00:33 UTC] connection failed: Connection refused\n"
    )
    events = alerts.parseHistoryLogs(str(logsDir))
    assert len(events) == 3
    assert events[0]["ok"] is True
    assert events[1]["ok"] is False  # no "connection o.k." in between
    assert events[1]["port"] == 2204
    assert events[2]["ok"] is False
    assert events[2]["port"] == 2205
    assert "Connection refused" in events[2]["reason"]


def testReplayCountsMessages(sandbox, capsys):
    import json

    logsDir = sandbox / "logs"
    logsDir.mkdir()
    lines = []
    t = datetime.datetime(2026, 11, 1, 10, 0, 0)
    for i in range(3):  # 3 failed runs -> one disturbance opens after 2
        stamp = (t + datetime.timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S UTC")
        lines.append(
            "[%s] connecting to ssh -o ConnectTimeout=45 user@hyy.invalid -p 2204 exit, lockfile /x/syncdata.a.lock\n" % stamp
        )
    lines.append("[2026-11-05 10:00:00 UTC] connecting to ssh -o ConnectTimeout=45 user@hyy.invalid -p 2204 exit, lockfile /x/syncdata.a.lock\n")
    lines.append("[2026-11-05 10:00:02 UTC] connection o.k.\n")
    (logsDir / "syncdata_2026-11-01.log").write_text("".join(lines))
    (sandbox / "instruments").mkdir(exist_ok=True)
    (sandbox / "instruments" / "a@site.json").write_text(json.dumps(config()))
    sent = alerts.replay(str(logsDir))
    assert sent["open"] == 1
    assert sent["resolve"] == 0  # the single success does not close yet
    assert "3" in capsys.readouterr().out


# ---------------------------------------------------------------------- misc


def testParsePort():
    assert alerts.parsePort("-p 2204") == 2204
    assert alerts.parsePort("") == 22
    assert alerts.parsePort("-o BatchMode=yes -p 22") == 22


def testConfigEndpointsWithHostList():
    endpoints = alerts.configEndpoints(config(host=["a.invalid", "b.invalid"], sshOptions="-p 2204"))
    assert endpoints == ["a.invalid:2204", "b.invalid:2204"]


def testEndpointLabelPrefersTheSiteCode():
    assert alerts.endpointLabel("ftp.meteo.example:2204", {"a@hyy", "b@hyy"}) == "hyy"
    assert alerts.endpointLabel("ftp.meteo.example:2204", {"a@hyy", "b@nya"}) == "ftp"


def testSaveAndLoadStateRoundTrip():
    state = alerts.loadState()
    state["endpoints"]["hyy.invalid:2204"] = {"open": True}
    alerts.saveState(state)
    assert alerts.loadState()["endpoints"]["hyy.invalid:2204"]["open"] is True
    # alerts state is not an instrument status
    assert syncstate.allStatuses() == {}


# -------------------------------------------------------------------- overview


def writeOverviewFixtures(sandbox):
    """A status and its instrument config for the overview."""
    (sandbox / "instruments").mkdir(exist_ok=True)
    (sandbox / "instruments" / "a@site.json").write_text(
        json.dumps(config(instrument="a@site"))
    )
    syncstate.writeStatus(
        "a@site",
        {
            "instrument": "a@site",
            "result": "ok",
            "running": False,
            "finished": NOW.isoformat(),
            "heartbeat": NOW.isoformat(),
            "start": NOW.isoformat(),
            "source": "cron",
            "mode": "day",
            "bytes": 5000,
            "lastDataAt": NOW.isoformat(),
        },
    )


def _addDisturbance(sandbox):
    """An open disturbance, so the overview has something to report."""
    state = alerts.loadState()
    state["endpoints"]["hyy.invalid:2204"] = {
        "open": True,
        "consecutiveFailures": 2,
        "consecutiveSuccesses": 0,
        "openedAt": (NOW - datetime.timedelta(hours=14)).isoformat(),
        "lastFailureAt": NOW.isoformat(),
        "lastReason": "Connection refused",
        "lastMessageAt": None,
        "acknowledged": False,
    }
    alerts.saveState(state)


def testOverviewTextListsInstrumentsAndDisturbances(sandbox):
    writeOverviewFixtures(sandbox)
    state = alerts.loadState()
    state["endpoints"]["hyy.invalid:2204"] = {
        "open": True,
        "consecutiveFailures": 2,
        "consecutiveSuccesses": 0,
        "openedAt": (NOW - datetime.timedelta(hours=14)).isoformat(),
        "lastFailureAt": NOW.isoformat(),
        "lastReason": "Connection refused",
        "lastMessageAt": None,
        "acknowledged": False,
    }
    alerts.saveState(state)
    text, configs, problems = alerts.overviewText(now=NOW)
    assert problems is True
    assert "a@site" in text
    assert "Instrumente" in text
    assert "site :2204" in text  # the disturbance line
    assert "Betroffen: a@site" in text
    assert "Störung" in text
    assert configs["a@site"]["channel"] == "testchannel"


def testOverviewWithoutProblemsSendsNothing(sandbox, monkeypatch, posts, capsys):
    # decided behaviour: the overview only reports errors, not daily
    writeOverviewFixtures(sandbox)
    monkeypatch.setattr(
        syncdata,
        "CONFIG",
        {"alertsDryRun": False, "mattermostWebhookUrl": "https://x.invalid/hook"},
    )
    alerts.overview(now=NOW)
    assert posts == []
    assert "alles ok, keine Nachricht" in capsys.readouterr().out


def testStaleDataIsAProblem(sandbox):
    # no new data for more than a day counts as a problem
    writeOverviewFixtures(sandbox)
    old = NOW - datetime.timedelta(hours=30)
    syncstate.writeStatus(
        "a@site",
        {
            "instrument": "a@site",
            "result": "ok",
            "running": False,
            "finished": old.isoformat(),
            "heartbeat": old.isoformat(),
            "start": old.isoformat(),
            "source": "cron",
            "mode": "day",
            "lastDataAt": old.isoformat(),
        },
    )
    text, configs, problems = alerts.overviewText(now=NOW)
    assert problems is True
    assert "letzte Daten >24 h" in text
    assert "a@site" in text


def testOverviewDryRunOnlyLogs(sandbox, capsys, posts):
    writeOverviewFixtures(sandbox)
    _addDisturbance(sandbox)
    alerts.overview(now=NOW)
    assert posts == []
    assert "[overview dry-run]" in capsys.readouterr().out


def testOverviewPostsWhenArmed(sandbox, monkeypatch, posts, capsys):
    writeOverviewFixtures(sandbox)
    _addDisturbance(sandbox)
    monkeypatch.setattr(
        syncdata, "CONFIG", {"alertsDryRun": False, "mattermostWebhookUrl": "https://x.invalid/hook"}
    )
    alerts.overview(now=NOW)
    assert len(posts) == 1
    assert posts[0]["channel"] == "testchannel"
    assert "Überblick" in posts[0]["text"]
    assert posts[0]["username"] == "syncdata"


def testOverviewFallsBackToTheFirstInstrumentChannel(sandbox, capsys, posts):
    writeOverviewFixtures(sandbox)
    _addDisturbance(sandbox)
    alerts.overview(now=NOW)
    assert "[overview dry-run] would send to testchannel" in capsys.readouterr().out


def testOverviewCountsAlertsOfTheLast24h(sandbox):
    writeOverviewFixtures(sandbox)
    syncstate.appendEvent("alert", "a@site", "test alert")
    text, configs, problems = alerts.overviewText(now=NOW)
    assert "Alarme letzte 24 h: 1" in text
