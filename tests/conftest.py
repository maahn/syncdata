"""Test fixtures: nothing here reaches a real instrument or Mattermost.

ssh and rsync are replaced by shell scripts, requests.post by a recorder, and
all paths point into pytest's temporary directory.
"""

import json
import os
import stat
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import syncdata  # noqa: E402

FAKE_SSH = """#!/bin/sh
[ -n "$FAKE_SSH_BACKGROUND" ] && sleep "$FAKE_SSH_BACKGROUND" &
[ -n "$FAKE_SSH_HANG" ] && sleep "$FAKE_SSH_HANG"
[ -n "$FAKE_SSH_MESSAGE" ] && echo "$FAKE_SSH_MESSAGE" >&2
exit "${FAKE_SSH_EXIT:-0}"
"""

FAKE_RSYNC = """#!/bin/sh
[ -n "$FAKE_RSYNC_CALLS" ] && echo "$*" >> "$FAKE_RSYNC_CALLS"
[ -n "$FAKE_RSYNC_ERROR" ] && echo "$FAKE_RSYNC_ERROR" >&2
echo "Total transferred file size: ${FAKE_RSYNC_BYTES:-1,234} bytes"
exit "${FAKE_RSYNC_EXIT:-0}"
"""


def writeScript(path, content):
    path.write_text(content)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


class FakeResponse:
    def __init__(self, status=200):
        self.status = status

    def raise_for_status(self):
        if self.status >= 400:
            raise syncdata.requests.HTTPError("%i error" % self.status)


@pytest.fixture(autouse=True)
def sandbox(tmp_path, monkeypatch):
    """Isolate syncdata from the real tools, config, locks and network."""
    binDir = tmp_path / "bin"
    binDir.mkdir()
    writeScript(binDir / "ssh", FAKE_SSH)
    writeScript(binDir / "rsync", FAKE_RSYNC)
    monkeypatch.setenv("PATH", "%s%s%s" % (binDir, os.pathsep, os.environ["PATH"]))
    monkeypatch.setenv("FAKE_RSYNC_CALLS", str(tmp_path / "rsync_calls.txt"))
    monkeypatch.setattr(syncdata, "RSYNC_BIN", str(binDir / "rsync"))
    monkeypatch.setattr(syncdata, "ROOT", str(tmp_path))
    monkeypatch.setattr(syncdata, "LOCK_DIR", str(tmp_path))
    monkeypatch.setattr(
        syncdata, "CONFIG", {"mattermostWebhookUrl": "https://mattermost.invalid/hook"}
    )
    return tmp_path


@pytest.fixture(autouse=True)
def posts(monkeypatch):
    """Record Mattermost posts; a test may set .response or .exception."""

    class Recorder(list):
        response = FakeResponse()
        exception = None

        def __call__(self, url, data=None, timeout=None):
            self.append({"url": url, "timeout": timeout, **json.loads(data)})
            if self.exception:
                raise self.exception
            return self.response

    recorder = Recorder()
    monkeypatch.setattr(syncdata.requests, "post", recorder)
    return recorder


@pytest.fixture
def instrument(tmp_path):
    return {
        "instrument": "testinst@site",
        "user": "user",
        "host": "host.invalid",
        "sshOptions": "-p 2200",
        "rsyncOptions": "",
        "remotePath": "/remote",
        "localPath": str(tmp_path / "data"),
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


@pytest.fixture
def writeInstruments(tmp_path):
    """Store instrument configs where main() looks for them."""

    def write(*instruments):
        directory = tmp_path / "instruments"
        directory.mkdir(exist_ok=True)
        for props in instruments:
            props = dict(props)
            name = props.pop("instrument")
            (directory / ("%s.json" % name)).write_text(json.dumps(props))

    return write


@pytest.fixture
def runMain(monkeypatch):
    """Run main() and restore the streams that setupHistoryLog replaces."""

    def run(*args):
        monkeypatch.setattr(sys, "argv", ["syncdata.py", *args])
        stdout, stderr = sys.stdout, sys.stderr
        try:
            syncdata.main()
        finally:
            sys.stdout, sys.stderr = stdout, stderr

    return run
