"""Tests for the realtime data tool integration. paramiko is stubbed."""

import json
import sys
import types

import pytest


def _stubScpModules():
    """realtimedata imports paramiko and scp; stub them when not installed."""
    if "paramiko" not in sys.modules:
        try:
            import paramiko  # noqa: F401
        except ImportError:
            paramiko = types.ModuleType("paramiko")
            paramiko.SSHClient = type("SSHClient", (), {})
            sys.modules["paramiko"] = paramiko
    if "scp" not in sys.modules:
        try:
            import scp  # noqa: F401
        except ImportError:
            scp = types.ModuleType("scp")
            scp.SCPClient = type("SCPClient", (), {})
            sys.modules["scp"] = scp


_stubScpModules()

import realtimedata  # noqa: E402
import syncstate  # noqa: E402


@pytest.fixture(autouse=True)
def realtimeRoot(sandbox, monkeypatch):
    """The shared sandbox plus realtimedata's ROOT."""
    monkeypatch.setattr(realtimedata, "ROOT", str(sandbox))
    return sandbox


def config(**overrides):
    base = {
        "instrument": "inst@site",
        "user": "user",
        "host": "host.invalid",
        "sshOptions": "-p 2200",
        "realtimePath": "realtime",
        "realtimeFiles": ["/remote/status.txt", "/remote/latest.jpg"],
        "hoursForDaySync": list(range(24)),
        "hoursForMonthSync": [],
    }
    base.update(overrides)
    return base


class FakeScp:
    def __init__(self, fail=False):
        self.fail = fail

    def get(self, remote, local_path=None):
        if self.fail:
            raise OSError("scp failed")


class FakeSsh:
    def get_transport(self):
        return object()

    def exec_command(self, command):
        return None, None, None

    def close(self):
        pass


def fakeClient(fail=False, calls=None):
    class Factory:
        def __init__(self, server, port, user):
            if calls is not None:
                calls.append((server, port, user))
            if fail:
                raise OSError("cannot connect")

        def get_transport(self):
            return object()

        def close(self):
            pass

    return Factory


def patchScp(monkeypatch, fail=False):
    monkeypatch.setattr(
        realtimedata,
        "SCPClient",
        lambda transport, socket_timeout=None: FakeScp(fail),
    )


# ------------------------------------------------------------------- the fetch


def testStatusIsWrittenOnSuccess(sandbox, monkeypatch):
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient())
    patchScp(monkeypatch)
    assert realtimedata.getRealtimeData("inst@site", config()) == 0
    status = syncstate.readStatus("inst@site")
    assert status["result"] == "ok"
    assert status["source"] == "realtime"
    assert status["mode"] == "realtime"
    assert status["running"] is False
    # quicklooks are not instrument data: the no-data alarm is untouched
    assert status["lastDataAt"] is None


def testStatusRecordsConnectionFailure(sandbox, monkeypatch):
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient(fail=True))
    assert realtimedata.getRealtimeData("inst@site", config()) == 1
    status = syncstate.readStatus("inst@site")
    assert status["result"] == "connection"
    assert "cannot connect" in status["error"]


def testScpFailureIsAConnectionProblem(sandbox, monkeypatch):
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient())
    patchScp(monkeypatch, fail=True)
    assert realtimedata.getRealtimeData("inst@site", config()) == 1
    assert syncstate.readStatus("inst@site")["result"] == "connection"


def testPortComesFromSshOptions(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient(calls=calls))
    patchScp(monkeypatch)
    realtimedata.getRealtimeData("inst@site", config(sshOptions="-p 2204"))
    assert calls[0][1] == 2204
    assert calls[0][2] == "user"


def testRealtimeFailureFeedsTheEndpointAlerts(sandbox, monkeypatch):
    # the endpoint alerts count realtime outcomes like sync outcomes
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient(fail=True))
    realtimedata.getRealtimeData("inst@site", config())
    import alerts

    outcomes = alerts.outcomesFromStatuses(
        {"inst@site": config()}, syncstate.allStatuses(), syncstate.utcNow()
    )
    assert outcomes[0]["ok"] is False
    assert outcomes[0]["endpoint"] == "host.invalid:2200"


def testStatusSurvivesABrokenStateDir(sandbox, monkeypatch):
    # writing the status must never stop the fetch
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient())
    patchScp(monkeypatch)
    monkeypatch.setattr(syncstate, "ROOT", "/proc/nonexistent-syncdata-test")
    assert realtimedata.getRealtimeData("inst@site", config()) == 0
    monkeypatch.setattr(syncstate, "ROOT", str(sandbox))


# ---------------------------------------------------------------------- the CLI


def testInstrumentModeOnlyFetchesTheSelectedOne(sandbox, monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient(calls=calls))
    patchScp(monkeypatch)
    instrumentDir = sandbox / "instruments"
    instrumentDir.mkdir()
    (instrumentDir / "inst@site.json").write_text(json.dumps(config()))
    (instrumentDir / "other@site.json").write_text(
        json.dumps(config(instrument="other@site"))
    )
    sys.argv = ["realtimedata.py", "inst@site"]
    try:
        realtimedata.main()
    except SystemExit:
        pass
    assert len(calls) == 1  # only inst@site was fetched
    assert calls[0][1] == 2200


def testWithoutArgumentsAllInstrumentsAreFetched(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(realtimedata, "createSSHClient", fakeClient(calls=calls))
    patchScp(monkeypatch)
    instrumentDir = sandbox / "instruments"
    instrumentDir.mkdir()
    (instrumentDir / "inst@site.json").write_text(json.dumps(config()))
    (instrumentDir / "other@site.json").write_text(
        json.dumps(config(instrument="other@site"))
    )
    sys.argv = ["realtimedata.py"]
    try:
        realtimedata.main()
    except SystemExit:
        pass
    assert len(calls) == 2
