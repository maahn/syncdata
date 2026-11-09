#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Run status and events for syncdata, stored under state/.

One JSON file per instrument (state/<instrument>.json) holds the latest run
status, state/events.jsonl appends one line per event. The directory lives
on NFS, so:

- status files are written atomically: temporary file with hostname and pid
  in the same folder, flush, fsync, then os.replace,
- no SQLite,
- a PID is only meaningful together with its hostname,
- readers must tolerate delays: changes can become visible late across hosts.

A status problem is printed but never raised: writing the status must never
stop a sync. The syncs run on a single host, so one shared events file with
append-mode writes is enough.
"""

import datetime
import glob
import json
import os
import socket
import threading

HEARTBEAT_INTERVAL = 20  # s, well below the GUI's staleness threshold
HEARTBEAT_STALE = 150  # s; a heartbeat older than this is not an active run
MAX_EVENTS_BYTES = 20 * 1024 * 1024  # rotate the events file beyond this
EVENTS_TAIL_BYTES = 262144  # how much of the events file readers look at

# Error kinds stored in the status files. A remote day folder that does not
# exist yet shortly after midnight is not an error (see syncdata.errorKind).
ERROR_KINDS = (
    "connection",
    "rsync_failed",
    "partial",
    "no_new_data",
    "locked",
    "remote_dir_missing",
)

ROOT = os.path.dirname(os.path.abspath(__file__))


def utcNow():
    """Current UTC time as an aware datetime (utcnow() is deprecated)."""
    return datetime.datetime.now(datetime.timezone.utc)


def stateDir():
    return os.path.join(ROOT, "state")


def statePath(instrument):
    return os.path.join(stateDir(), "%s.json" % instrument)


def eventsPath():
    return os.path.join(stateDir(), "events.jsonl")


def parseIso(text):
    """Parse an ISO timestamp written by isoformat(); None when unusable."""
    try:
        return datetime.datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None


def heartbeatAge(status, now=None):
    """Seconds since the heartbeat of a status, or None when unknown."""
    if not status:
        return None
    heartbeat = parseIso(status.get("heartbeat"))
    if heartbeat is None:
        return None
    now = now or utcNow()
    return max(0.0, (now - heartbeat).total_seconds())


def heartbeatFresh(status, now=None, stale=HEARTBEAT_STALE):
    """True when the status was written recently, tolerating NFS delays."""
    age = heartbeatAge(status, now)
    return age is not None and age <= stale


def writeJson(path, data):
    """Atomically write a JSON file: temp file in the same folder, fsync,
    then os.replace. Never raises."""
    directory = os.path.dirname(path)
    hostname = socket.gethostname()
    try:
        os.makedirs(directory, exist_ok=True)
        tmp = os.path.join(
            directory,
            ".%s.%s.%i.tmp" % (
                os.path.basename(path),
                hostname,
                os.getpid(),
            ),
        )
        try:
            with open(tmp, "w") as f:
                json.dump(data, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)
    except Exception as err:
        print("failed to write %s: %s" % (path, err))


def writeStatus(instrument, fields):
    """Atomically write the status of one instrument. Never raises."""
    base = dict(fields)
    base.setdefault("instrument", instrument)
    base.setdefault("hostname", socket.gethostname())
    base.setdefault("pid", os.getpid())
    writeJson(statePath(instrument), base)


def readStatus(instrument):
    """Read the status of one instrument; None when there is none."""
    try:
        with open(statePath(instrument)) as f:
            return json.load(f)
    except Exception:
        return None


def allStatuses():
    """Read every instrument status. alerts.json is not a status.

    Files that cannot be read (for example a delayed write seen over NFS)
    are skipped; the GUI has to cope with that anyway.
    """
    statuses = {}
    for path in glob.glob(os.path.join(stateDir(), "*.json")):
        instrument = os.path.splitext(os.path.basename(path))[0]
        if instrument == "alerts":
            continue
        try:
            with open(path) as f:
                statuses[instrument] = json.load(f)
        except Exception:
            continue
    return statuses


def appendEvent(kind, instrument="", text="", **extra):
    """Append one event to the shared events file. Never raises."""
    try:
        os.makedirs(stateDir(), exist_ok=True)
        _trimEvents()
        event = {
            "time": utcNow().isoformat(),
            "kind": kind,
            "instrument": instrument,
            "text": text,
            "hostname": socket.gethostname(),
            "pid": os.getpid(),
        }
        event.update(extra)
        with open(eventsPath(), "a") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as err:
        print("failed to write event: %s" % err)


def _trimEvents():
    """Keep the events file from growing without bound. Best effort."""
    path = eventsPath()
    try:
        if not os.path.exists(path):
            return
        size = os.path.getsize(path)
        if size <= MAX_EVENTS_BYTES:
            return
        with open(path, "rb") as f:
            f.seek(max(0, size - EVENTS_TAIL_BYTES))
            tail = f.read()
        lines = tail.split(b"\n", 1)[-1]
        tmp = "%s.tmp" % path
        with open(tmp, "wb") as f:
            f.write(lines)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception as err:
        print("failed to trim events: %s" % err)


def readEvents(limit=100, tailBytes=EVENTS_TAIL_BYTES):
    """Read the last events, newest last. Tolerates a torn final line."""
    try:
        with open(eventsPath(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - tailBytes))
            data = f.read().decode("utf-8", "replace")
    except OSError:
        return []
    lines = data.split("\n")
    if size > tailBytes:
        lines = lines[1:]  # first line may be cut in half
    lines = [line for line in lines if line.strip()]
    events = []
    for line in lines[-limit:]:
        try:
            events.append(json.loads(line))
        except ValueError:
            continue
    return events


class StatusTracker:
    """Writes the status of one running sync and keeps the heartbeat alive.

    The heartbeat is refreshed by a daemon thread, so the GUI can tell an
    active run from a finished one even when a single rsync takes longer
    than the staleness threshold.
    """

    def __init__(self, instrument, source="", mode=""):
        self.instrument = instrument
        self.fields = {"source": source, "mode": mode, "running": False}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None

    def begin(self):
        """Write the running status and start the heartbeat thread."""
        carried = readStatus(self.instrument) or {}
        self.fields.update(
            {
                "start": utcNow().isoformat(),
                "heartbeat": utcNow().isoformat(),
                "finished": None,
                "running": True,
                "result": "running",
                "error": None,
                "bytes": 0,
                "currentPath": None,
                "currentFile": None,
                "lastDataAt": carried.get("lastDataAt"),
            }
        )
        appendEvent(
            "sync_start",
            self.instrument,
            "%s %s" % (self.fields["source"], self.fields["mode"]),
        )
        self._write()
        self._thread = threading.Thread(target=self._heartbeat, daemon=True)
        self._thread.start()
        return self

    def _heartbeat(self):
        while not self._stop.wait(HEARTBEAT_INTERVAL):
            with self._lock:
                self._write()

    def _write(self):
        fields = dict(self.fields)
        fields["heartbeat"] = utcNow().isoformat()
        writeStatus(self.instrument, fields)

    def update(self, **fields):
        with self._lock:
            self.fields.update(fields)
            self._write()

    def noteBytes(self):
        """Record that this run transferred data."""
        with self._lock:
            self.fields["lastDataAt"] = utcNow().isoformat()
            self._write()

    def finish(self, bytes=None, result="ok", error=None):
        """Stop the heartbeat and write the final status."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=HEARTBEAT_INTERVAL + 5)
        with self._lock:
            self.fields.update(
                {
                    "running": False,
                    "finished": utcNow().isoformat(),
                    "result": result,
                    "error": error,
                }
            )
            if bytes is not None:
                self.fields["bytes"] = bytes
            self._write()
        appendEvent(
            "sync_done",
            self.instrument,
            "%s, %s bytes"
            % (self.fields["result"], self.fields.get("bytes", 0)),
            result=self.fields["result"],
        )

    def locked(self):
        """Write a finished status because the lock was busy.

        Never hides an active run: another process may hold the lock, and
        its status must stay visible in the GUI.
        """
        carried = readStatus(self.instrument) or {}
        if carried.get("running") and heartbeatFresh(carried):
            appendEvent("locked", self.instrument, "already running")
            return
        writeStatus(
            self.instrument,
            {
                "source": self.fields.get("source"),
                "mode": self.fields.get("mode"),
                "start": utcNow().isoformat(),
                "finished": utcNow().isoformat(),
                "running": False,
                "result": "locked",
                "error": None,
                "bytes": 0,
                "lastDataAt": carried.get("lastDataAt"),
            },
        )
        appendEvent("locked", self.instrument, "lock busy")
