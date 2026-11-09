"""Tests for the terminal GUI. Skipped when textual is not installed."""

import asyncio
import json

import pytest

pytest.importorskip("textual")

import alerts  # noqa: E402
import syncstate  # noqa: E402
import synctui  # noqa: E402

SIZE = (100, 40)


def status(**overrides):
    now = syncstate.utcNow().isoformat()
    base = {
        "instrument": "inst@site",
        "result": "ok",
        "running": False,
        "finished": now,
        "heartbeat": now,
        "start": now,
        "source": "cron",
        "mode": "day",
        "bytes": 1234,
        "lastDataAt": now,
    }
    base.update(overrides)
    return base


def run(scenario):
    """Run an async scenario with the app and pilot, return its result."""

    async def main():
        app = synctui.SyncTui()
        async with app.run_test(size=SIZE) as pilot:
            return await scenario(app, pilot)

    return asyncio.run(main())


def startedProcesses(monkeypatch):
    """Record what startSync would spawn, without touching real syncs."""
    processes = []

    class FakeProcess:
        def __init__(self, args):
            self.args = args
            self.pid = 4242

    def fakePopen(args, **kwargs):
        processes.append(args)
        return FakeProcess(args)

    monkeypatch.setattr(synctui.subprocess, "Popen", fakePopen)
    return processes


# --------------------------------------------------------------- the main view


def testAppRendersRowsFromStateFiles(sandbox):
    syncstate.writeStatus("inst@site", status())
    syncstate.writeStatus("other@site", status(running=True, result="running"))

    async def scenario(app, pilot):
        table = app.query_one("#instruments")
        rows = [table.get_row_at(i)[0] for i in range(table.row_count)]
        assert rows == ["inst@site", "other@site"]
        summary = str(app.query_one("#summary").content)
        assert "läuft: 1" in summary
        disturbances = str(app.query_one("#disturbances").content)
        assert "keine Störungen" in disturbances

    run(scenario)


def testAppShowsDisturbances(sandbox):
    syncstate.writeStatus("inst@site", status())
    state = alerts.loadState()
    state["endpoints"]["host.invalid:2200"] = {
        "open": True,
        "consecutiveFailures": 2,
        "consecutiveSuccesses": 0,
        "openedAt": "2026-11-06T08:00:00+00:00",
        "lastFailureAt": "2026-11-06T08:00:00+00:00",
        "lastReason": "Connection refused",
        "lastMessageAt": None,
        "acknowledged": False,
    }
    alerts.saveState(state)

    async def scenario(app, pilot):
        disturbances = str(app.query_one("#disturbances").content)
        assert "host" in disturbances
        assert "seit" in disturbances

    run(scenario)


# ------------------------------------------------------------- start dialog


def testClickOnRowOpensTheStartDialog(sandbox):
    syncstate.writeStatus("inst@site", status())

    async def scenario(app, pilot):
        # first data row: header is row 0 of the table region
        await pilot.click("#instruments", offset=(10, 1))
        dialog = app.screen
        assert isinstance(dialog, synctui.StartDialog)
        text = str(dialog.query_one("#dialogText").content)
        assert "inst@site" in text
        await pilot.press("escape")
        assert not isinstance(app.screen, synctui.StartDialog)

    run(scenario)


def testStartButtonsSpawnTheRightArguments(sandbox, monkeypatch):
    syncstate.writeStatus("inst@site", status())
    processes = startedProcesses(monkeypatch)

    async def scenario(app, pilot):
        await pilot.click("#instruments", offset=(10, 1))
        await pilot.click("#startDays")
        await pilot.pause()
        assert len(processes) == 1
        args = processes[0]
        assert args[-3:] == ["inst@site", "--days", "3"]

        await pilot.click("#instruments", offset=(10, 1))
        await pilot.click("#startMonths")
        await pilot.pause()
        assert processes[1][-3:] == ["inst@site", "--months", "2"]

    run(scenario)


def testStartWritesIntoTheHandLog(sandbox, monkeypatch):
    syncstate.writeStatus("inst@site", status())
    startedProcesses(monkeypatch)

    async def scenario(app, pilot):
        await pilot.click("#instruments", offset=(10, 1))
        await pilot.click("#startDays")
        await pilot.pause()
        log = sandbox / "logs" / "hand_inst@site.log"
        assert log.exists()
        assert "syncdata.py inst@site --days 3" in log.read_text()

    run(scenario)


def testRunningInstrumentDisablesTheButtons(sandbox):
    syncstate.writeStatus("inst@site", status(running=True, result="running"))

    async def scenario(app, pilot):
        await pilot.click("#instruments", offset=(10, 1))
        dialog = app.screen
        assert isinstance(dialog, synctui.StartDialog)
        assert dialog.query_one("#startDays").disabled is True
        assert dialog.query_one("#startMonths").disabled is True
        text = str(dialog.query_one("#dialogText").content)
        assert "Läuft schon" in text

    run(scenario)


def testStartIsRefusedWhenTheStatusChanged(sandbox, monkeypatch):
    # the dialog opened while the instrument was idle; it is running now
    syncstate.writeStatus("inst@site", status())
    processes = startedProcesses(monkeypatch)

    async def scenario(app, pilot):
        dialog = synctui.StartDialog("inst@site", status(), running=False)
        app.push_screen(dialog)
        await pilot.pause()
        syncstate.writeStatus("inst@site", status(running=True, result="running"))
        await pilot.click("#startDays")
        await pilot.pause()
        assert processes == []

    run(scenario)


# ------------------------------------------------------------- key bindings


def testAcknowledgeKeyWritesTheState(sandbox):
    syncstate.writeStatus("inst@site", status())
    state = alerts.loadState()
    state["endpoints"]["host.invalid:2200"] = {
        "open": True,
        "consecutiveFailures": 2,
        "consecutiveSuccesses": 0,
        "openedAt": "2026-11-06T08:00:00+00:00",
        "lastFailureAt": "2026-11-06T08:00:00+00:00",
        "lastReason": "Connection refused",
        "lastMessageAt": None,
        "acknowledged": False,
    }
    alerts.saveState(state)

    async def scenario(app, pilot):
        await pilot.press("a")
        await pilot.pause()
        assert alerts.loadState()["endpoints"]["host.invalid:2200"]["acknowledged"] is True

    run(scenario)


def testMuteKeyWritesMutedUntil(sandbox):
    async def scenario(app, pilot):
        await pilot.press("m")
        await pilot.pause()
        assert alerts.loadState()["mutedUntil"]

    run(scenario)


def testLogAndEventViewersOpen(sandbox):
    syncstate.writeStatus("inst@site", status())
    syncstate.appendEvent("sync_done", "inst@site", "ok, 1234 bytes")
    logDir = sandbox / "logs"
    logDir.mkdir(exist_ok=True)
    (logDir / "syncdata_2026-11-06.log").write_text("2026-11-06 10:00 mode cron all\n")

    async def scenario(app, pilot):
        await pilot.press("l")
        await pilot.pause()
        viewer = str(app.screen.query_one("Static").content)
        assert "mode cron all" in viewer
        await pilot.press("escape")

        await pilot.press("e")
        await pilot.pause()
        viewer = str(app.screen.query_one("Static").content)
        assert "sync_done" in viewer
        await pilot.press("escape")

    run(scenario)


def testQuitKeyEndsTheApp(sandbox):
    async def scenario(app, pilot):
        await pilot.press("q")

    # run() leaves the context when the app exits; a clean exit is the assert
    assert run(scenario) is None


# ----------------------------------------------------------------- startSync


def testStartSyncRunsDetached(sandbox, monkeypatch):
    calls = {}

    class FakeProcess:
        pid = 4242

    def fakePopen(args, **kwargs):
        calls["args"] = args
        calls["kwargs"] = kwargs
        return FakeProcess()

    monkeypatch.setattr(synctui.subprocess, "Popen", fakePopen)
    process = synctui.startSync("inst@site", ["--days", "3"], root=str(sandbox))
    assert process.pid == 4242
    assert calls["kwargs"]["start_new_session"] is True  # survives closing the GUI
    assert calls["args"][-3:] == ["inst@site", "--days", "3"]
    log = (sandbox / "logs" / "hand_inst@site.log").read_text()
    assert "### START" in log
