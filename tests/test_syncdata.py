import datetime
import glob
import os
import time

import pytest
from filelock import FileLock

import syncdata


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
    def fail(command):
        raise OSError("cannot start")

    monkeypatch.setattr(syncdata, "execute_stdout", fail)
    output, error, transferred, command = callSync(tmp_path)
    assert error == "cannot start"
    assert transferred == 0


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
