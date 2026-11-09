import datetime
import glob
import os
import time

import pytest
from filelock import FileLock

import syncdata
import syncstate


def historyLog(tmp_path):
    files = glob.glob(str(tmp_path / "logs" / "*.log"))
    assert len(files) == 1
    with open(files[0]) as f:
        return f.read()


# ---------------------------------------------------------------- monthStrings


@pytest.mark.parametrize(
    "now, expected",
    [
        # 30-day steps gave March, March, January here
        (datetime.datetime(2026, 3, 31), ["2026/01", "2026/02", "2026/03"]),
        (datetime.datetime(2028, 3, 30), ["2028/01", "2028/02", "2028/03"]),
        (datetime.datetime(2026, 7, 31), ["2026/05", "2026/06", "2026/07"]),
        (datetime.datetime(2026, 1, 1), ["2025/11", "2025/12", "2026/01"]),
        (datetime.datetime(2026, 2, 15), ["2025/12", "2026/01", "2026/02"]),
    ],
)
def testMonthStringsAreCalendarMonths(now, expected):
    assert syncdata.monthStrings(now, 2, "%Y/%m") == expected


def testMonthStringsWithoutMonthlyFolders():
    assert syncdata.monthStrings(datetime.datetime(2026, 3, 31), 0, "") == [""]


# ------------------------------------------------------------ parseRsyncOutput


@pytest.mark.parametrize(
    "line, expected",
    [
        ("Total transferred file size: 0 bytes", 0),
        ("Total transferred file size: 1,234,567 bytes", 1234567),
        ("Total transferred file size: 1.234.567 bytes", 1234567),
    ],
)
def testParseRsyncOutput(line, expected):
    assert syncdata.parseRsyncOutput("other line\n%s\n" % line) == expected


def testParseRsyncOutputWithoutStats():
    assert syncdata.parseRsyncOutput("rsync: connection unexpectedly closed") == 0


# ------------------------------------------------------------------------ sync


def callSync(tmp_path, localPath=None):
    return syncdata.sync(
        localPath or str(tmp_path / "local"),
        "/remote/",
        "-p 2200",
        "user",
        "host.invalid",
        "",
    )


def testSyncSuccess(tmp_path):
    output, error, transferred, command = callSync(tmp_path)
    assert error is None
    assert transferred == 1234
    assert "user@host.invalid:/remote/" in command


def testSyncReturnsFourValuesWhenDirectoryCannotBeCreated(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    output, error, transferred, command = callSync(tmp_path, str(blocker / "sub"))
    assert "Cannot create directory" in error
    assert transferred == 0


def testSyncKeepsBytesAndReportsErrorOnPartialFailure(tmp_path, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_EXIT", "23")
    output, error, transferred, command = callSync(tmp_path)
    assert transferred == 1234
    assert "rsync exit code 23" in error


def testSyncSurvivesExecutionFailure(tmp_path, monkeypatch):
    def fail(command, onProgress=None):
        raise OSError("cannot start")

    monkeypatch.setattr(syncdata, "execute_stdout", fail)
    output, error, transferred, command = callSync(tmp_path)
    assert error == "cannot start"
    assert transferred == 0


# ------------------------------------------------------------------- progress


def testRsyncProgressSupportDetectsVersion(tmp_path, monkeypatch):
    writeScript = tmp_path / "rsync"
    writeScript.write_text('#!/bin/sh\necho "rsync version 3.2.3  protocol version 31"\n')
    writeScript.chmod(0o755)
    monkeypatch.setattr(syncdata, "RSYNC_BIN", str(writeScript))
    monkeypatch.setattr(syncdata, "_rsyncProgressSupported", None)
    assert syncdata.rsyncSupportsProgress() is True

    writeScript.write_text('#!/bin/sh\necho "rsync version 2.6.9  protocol version 30"\n')
    monkeypatch.setattr(syncdata, "_rsyncProgressSupported", None)
    assert syncdata.rsyncSupportsProgress() is False

    writeScript.write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(syncdata, "_rsyncProgressSupported", None)
    assert syncdata.rsyncSupportsProgress() is False


def testProgressOptOnlyWhenSupported(instrument, tmp_path):
    # without version output (the default fake) there is no progress flag
    syncdata.monthSync(2, **instrument)
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert calls
    for call in calls:
        assert "--info=progress2" not in call


def testProgressFlagWithModernRsync(anySync, instrument, tmp_path, monkeypatch):
    writeScript = tmp_path / "bin" / "rsync"
    content = writeScript.read_text()
    writeScript.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "rsync version 3.2.3 protocol version 31"; exit 0; fi\n'
        + content.split("\n", 1)[1]
    )
    monkeypatch.setattr(syncdata, "_rsyncProgressSupported", None)
    assert anySync(**instrument) > 0
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert calls
    for call in calls:
        assert "--info=progress2" in call
    # the sync itself still works with the flag
    assert syncstate.readStatus("testinst@site")["result"] == "ok"


def testExecute_stdoutParsesProgress(tmp_path):
    script = tmp_path / "progress.sh"
    script.write_text(
        '#!/bin/sh\nprintf " 1,234  45%%   1.2MB/s  0:00:30\\n"\nprintf "done\\n"\n'
    )
    script.chmod(0o755)
    percents = []
    code, output = syncdata.execute_stdout(
        str(script), onProgress=percents.append
    )
    assert percents == [45]
    assert "done" in output


def testProgressIsWrittenIntoTheStatus(anySync, instrument, tmp_path, monkeypatch):
    import syncstate

    writeScript = tmp_path / "bin" / "rsync"
    content = writeScript.read_text()
    # a fake rsync that prints a progress line before the stats
    writeScript.write_text(
        '#!/bin/sh\nif [ "$1" = "--version" ]; then echo "rsync version 3.2.3 protocol version 31"; exit 0; fi\n'
        'printf " 1,234  42%%   1.2MB/s  0:00:30\\n"\n'
        + content.split("\n", 1)[1]
    )
    monkeypatch.setattr(syncdata, "_rsyncProgressSupported", None)
    assert anySync(**instrument) > 0
    status = syncstate.readStatus("testinst@site")
    # the folder is done, so the progress is reset
    assert status["progress"] is None
    assert status["result"] == "ok"


def testStatusviewShowsThePercent(sandbox):
    import statusview

    status = {"running": True, "heartbeat": syncstate.utcNow().isoformat(),
              "progress": 42, "currentPath": "/remote/2026/11/", "bytes": 1234}
    label = statusview.currentLabel(status)
    assert "42 %" in label
    assert "2026/11" in label
    # without progress there is no percent
    status["progress"] = None
    assert "%" not in statusview.currentLabel(status)


# ------------------------------------------------------------- checkConnection


def testConnectionFailureReasonIsPrinted(monkeypatch, capsys):
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    monkeypatch.setenv("FAKE_SSH_MESSAGE", "Connection refused")
    with pytest.raises(syncdata.subprocess.CalledProcessError) as err:
        syncdata.checkConnection("ssh user@host.invalid exit")
    assert err.value.output == "Connection refused"
    assert "Connection refused" in capsys.readouterr().out


def testConnectionCheckDoesNotWaitForPersistentMaster(monkeypatch):
    # a ControlPersist master stays alive and keeps stdout/stderr open
    monkeypatch.setenv("FAKE_SSH_BACKGROUND", "4")
    start = time.time()
    syncdata.checkConnection("ssh user@host.invalid exit")
    assert time.time() - start < 3


def testConnectionCheckTimesOut(monkeypatch):
    monkeypatch.setenv("FAKE_SSH_HANG", "4")
    monkeypatch.setattr(syncdata, "sshTimeout", -14)  # results in a 1 s limit
    with pytest.raises(syncdata.subprocess.TimeoutExpired):
        syncdata.checkConnection("ssh user@host.invalid exit")


# ---------------------------------------------------------- monthSync / daySync


@pytest.fixture(params=["month", "day"])
def anySync(request):
    if request.param == "month":
        return lambda **kwargs: syncdata.monthSync(2, **kwargs)
    return lambda **kwargs: syncdata.daySync(3, **kwargs)


def testSyncTransfersData(anySync, instrument, posts):
    assert anySync(**instrument) > 0
    assert posts == []


def testConnectionFailureIsReportedWithReason(anySync, instrument, posts, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    monkeypatch.setenv("FAKE_SSH_MESSAGE", "Connection refused")
    assert anySync(**instrument) == 0
    assert len(posts) == 1
    assert "Connection refused" in posts[0]["text"]
    assert posts[0]["channel"] == "testchannel"


def testBusyLockIsReported(anySync, instrument, posts, tmp_path):
    with FileLock(str(tmp_path / "syncdata.testinst@site.lock")):
        assert anySync(**instrument) == 0
    assert "Lock file in place" in posts[0]["text"]


def testZeroBytesAreReportedWithRsyncError(anySync, instrument, posts, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_BYTES", "0")
    monkeypatch.setenv("FAKE_RSYNC_EXIT", "30")
    assert anySync(**instrument) == 0
    assert "rsync exit code 30" in posts[0]["text"]


def testPartialFailureDoesNotCountAsNoData(anySync, instrument, posts, monkeypatch):
    # e.g. the folder of the current day does not exist on the remote side yet
    monkeypatch.setenv("FAKE_RSYNC_EXIT", "23")
    assert anySync(**instrument) > 0
    assert posts == []


def testDirectoryFailureDoesNotCrash(anySync, instrument, posts, tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("")
    instrument["localPath"] = str(blocker)
    assert anySync(**instrument) == 0
    assert "Cannot create directory" in posts[0]["text"]


def testEmptyHostListDoesNotCrash(anySync, instrument, posts):
    instrument["host"] = []
    assert anySync(**instrument) == 0
    assert len(posts) == 1


def testMonthSyncVisitsEachMonthOnce(instrument, tmp_path):
    syncdata.monthSync(2, **instrument)
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert len(calls) == 3
    expected = syncdata.monthStrings(datetime.datetime.utcnow(), 2, "%Y/%m")
    for call, month in zip(calls, expected):
        assert "/remote/%s/" % month in call


# ------------------------------------------------------------- postToMattermost


def testMattermostPostHasTimeout(posts):
    syncdata.postToMattermost("channel", "inst", "text")
    assert posts[0]["timeout"] == syncdata.mattermostTimeout


def testMattermostWithoutChannelOrUrlSendsNothing(posts, monkeypatch):
    syncdata.postToMattermost("", "inst", "text")
    monkeypatch.setattr(syncdata, "CONFIG", {})
    syncdata.postToMattermost("channel", "inst", "text")
    assert posts == []


def testMattermostOutageDoesNotRaise(posts):
    posts.exception = syncdata.requests.ConnectionError("down")
    syncdata.postToMattermost("channel", "inst", "text")


def testMattermostHttpErrorDoesNotRaise(posts, monkeypatch):
    from conftest import FakeResponse

    posts.response = FakeResponse(500)
    syncdata.postToMattermost("channel", "inst", "text")


# ------------------------------------------------------------------------ main


def twoInstruments(instrument):
    first = dict(instrument, instrument="first@site")
    second = dict(instrument, instrument="second@site")
    return first, second


def testMattermostOutageDoesNotStopRemainingInstruments(
    instrument, writeInstruments, runMain, posts, tmp_path, monkeypatch
):
    writeInstruments(*twoInstruments(instrument))
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    posts.exception = syncdata.requests.ConnectionError("down")
    runMain("hourly")
    assert [post["username"] for post in posts] == ["first@site", "second@site"]
    assert "done" in historyLog(tmp_path)


def testDirectoryFailureDoesNotStopRemainingInstruments(
    instrument, writeInstruments, runMain, tmp_path
):
    blocker = tmp_path / "file"
    blocker.write_text("")
    first, second = twoInstruments(instrument)
    first["localPath"] = str(blocker)
    writeInstruments(first, second)
    runMain("hourly")
    log = historyLog(tmp_path)
    assert "syncdata.second@site.lock" in log
    assert "done" in log


def testConnectionFailureReasonReachesHistoryLog(
    instrument, writeInstruments, runMain, tmp_path, monkeypatch
):
    writeInstruments(instrument)
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    monkeypatch.setenv("FAKE_SSH_MESSAGE", "Connection refused")
    runMain("hourly")
    assert "Connection refused" in historyLog(tmp_path)


@pytest.mark.parametrize("mode", ["hourly", "daily", "cron"])
def testCallOnCompleteRunsAlsoAfterFailedSync(
    mode, instrument, writeInstruments, runMain, monkeypatch, tmp_path
):
    # decided behaviour: post-processing also runs after failed syncs
    instrument["CallOnComplete"] = "process.sh"
    instrument["CallOnCompleteLog"] = str(tmp_path / "process.log")
    writeInstruments(instrument)
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    calls = []

    class Process:
        def wait(self):
            pass

    def fakeCallExt(*args):
        calls.append(args[0])
        return Process(), open(os.devnull, "w")

    monkeypatch.setattr(syncdata, "callExt", fakeCallExt)
    monkeypatch.setattr(syncdata, "processes", [])
    monkeypatch.setattr(syncdata, "logfiles", [])
    runMain(mode)
    assert calls == ["process.sh"]


# ----------------------------------------------------------------- run status


def runStatus():
    import syncstate

    return syncstate.readStatus("testinst@site")


def testRunStatusIsWrittenOnSuccess(anySync, instrument, posts):
    assert anySync(**instrument) > 0
    status = runStatus()
    assert status["result"] == "ok"
    assert status["bytes"] > 0
    assert status["source"] == "hand"
    assert status["mode"] in ("month", "day")
    assert status["running"] is False
    assert status["lastDataAt"]


def testRunStatusRecordsConnectionError(anySync, instrument, posts, monkeypatch):
    monkeypatch.setenv("FAKE_SSH_EXIT", "255")
    monkeypatch.setenv("FAKE_SSH_MESSAGE", "Connection refused")
    assert anySync(**instrument) == 0
    status = runStatus()
    assert status["result"] == "connection"
    assert "Connection refused" in status["error"]


def testRunStatusRecordsLocked(anySync, instrument, posts, tmp_path):
    with FileLock(str(tmp_path / "syncdata.testinst@site.lock")):
        assert anySync(**instrument) == 0
    assert runStatus()["result"] == "locked"


def testRunStatusRecordsPartial(anySync, instrument, posts, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_EXIT", "23")
    assert anySync(**instrument) > 0
    assert runStatus()["result"] == "partial"


def testRunStatusRecordsNoData(anySync, instrument, posts, monkeypatch):
    monkeypatch.setenv("FAKE_RSYNC_BYTES", "0")
    assert anySync(**instrument) == 0
    assert runStatus()["result"] == "no_new_data"


def testRunStatusRecordsMissingRemoteDirAsNoError(
    anySync, instrument, posts, monkeypatch
):
    # shortly after midnight the day folder does not exist yet; not an error
    monkeypatch.setenv("FAKE_RSYNC_EXIT", "23")
    monkeypatch.setenv(
        "FAKE_RSYNC_ERROR",
        "rsync: change_dir#3 in /remote/2026/11/06 failed: No such file or directory",
    )
    assert anySync(**instrument) > 0
    assert runStatus()["result"] == "remote_dir_missing"
    assert posts == []


def testRunStatusSurvivesABrokenStateDir(anySync, instrument, posts, monkeypatch):
    # writing the status must never stop the sync
    monkeypatch.setattr(syncstate, "ROOT", "/proc/nonexistent-syncdata-test")
    assert anySync(**instrument) > 0
    assert posts == []


def testRunStatusSourceCron(instrument, writeInstruments, runMain):
    writeInstruments(instrument)
    runMain("cron")
    assert runStatus()["source"] == "cron"


def testRsyncCommandWithoutFileLists(instrument, tmp_path):
    # the file lists made half of the history logs
    syncdata.monthSync(2, **instrument)
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert calls
    for call in calls:
        assert "-v" not in call.split("--modify-window")[0].split()


def testNoUtcnowDeprecationWarning(anySync, instrument):
    import warnings

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        anySync(**instrument)
    assert not any("utcnow" in str(w.message) for w in caught)


# ------------------------------------------------------------ command line


def testInstrumentModeDefaultsToTwoMonths(instrument, writeInstruments, runMain, tmp_path):
    writeInstruments(instrument)
    runMain("testinst@site")
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert len(calls) == 3  # the months, like before


def testDaysOptionSyncsLastDays(instrument, writeInstruments, runMain, tmp_path):
    writeInstruments(instrument)
    runMain("testinst@site", "--days", "3")
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert len(calls) == 4
    assert runStatus()["mode"] == "day"


def testMonthsOptionSyncsLastMonths(instrument, writeInstruments, runMain, tmp_path):
    writeInstruments(instrument)
    runMain("testinst@site", "--months", "2")
    calls = (tmp_path / "rsync_calls.txt").read_text().splitlines()
    assert len(calls) == 3
    assert runStatus()["mode"] == "month"


def testUnknownOptionIsRejected(instrument, writeInstruments, runMain, tmp_path):
    # @logger.catch on main() logs the KeyError without re-raising; the run
    # must abort before anything is synced
    writeInstruments(instrument)
    runMain("testinst@site", "--weeks", "3")
    calls = (tmp_path / "rsync_calls.txt")
    assert not calls.exists() or calls.read_text() == ""


@pytest.mark.parametrize("code, ok", [(0, True)])
def testStatusModeExitCodes(instrument, writeInstruments, runMain, code, ok):
    import syncstate

    writeInstruments(instrument)
    runMain("hourly")  # writes a status
    with pytest.raises(SystemExit) as exc:
        runMain("status")
    assert exc.value.code == 0


def testStatusModeExitsOneOnDisturbance(instrument, writeInstruments, runMain):
    import alerts
    import syncstate

    writeInstruments(instrument)
    runMain("hourly")
    state = alerts.loadState()
    state["endpoints"]["host.invalid:2200"] = {
        "open": True,
        "consecutiveFailures": 2,
        "consecutiveSuccesses": 0,
        "openedAt": syncstate.utcNow().isoformat(),
        "lastFailureAt": syncstate.utcNow().isoformat(),
        "lastReason": "Connection refused",
        "lastMessageAt": None,
        "acknowledged": False,
    }
    alerts.saveState(state)
    with pytest.raises(SystemExit) as exc:
        runMain("status")
    assert exc.value.code == 1
