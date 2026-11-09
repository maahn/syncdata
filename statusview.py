#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Shared view data for the terminal GUI and `syncdata.py status`.

Plain strings only — no textual here, so the cron paths never import the
GUI and vice versa. Reads only state/, it never syncs and never locks.
"""

import os

import alerts
import syncstate

HEARTBEAT_STALE = syncstate.HEARTBEAT_STALE


def humanDuration(seconds):
    seconds = int(seconds)
    if seconds < 0:
        return "0 s"
    if seconds < 90:
        return "%i s" % seconds
    minutes = seconds // 60
    if minutes < 120:
        return "%i min" % minutes
    hours = minutes // 60
    if hours < 48:
        return "%i h" % hours
    return "%i d" % (hours // 24)


def humanBytes(size):
    size = float(size)
    if size < 1024:
        return "%i B" % size
    for unit in ("kB", "MB", "GB"):
        size /= 1024.0
        if size < 1024:
            return "%.1f %s" % (size, unit) if unit != "kB" else "%i %s" % (size, unit)
    return "%.1f TB" % (size / 1024.0)


def isRunning(status, now=None):
    """True when the heartbeat says this instrument runs right now."""
    return bool(status and status.get("running")) and syncstate.heartbeatFresh(
        status, now
    )


def statusLabel(status, now=None):
    """The status column for one instrument."""
    if not status:
        return "-"
    if status.get("running") and not syncstate.heartbeatFresh(status, now):
        return "abgebrochen"
    if isRunning(status, now):
        return "läuft"
    result = status.get("result")
    return {
        "ok": "ok",
        "connection": "Verb.err.",
        "rsync_failed": "Fehler",
        "partial": "teilw.",
        "no_new_data": "keine Daten",
        "locked": "belegt",
        "remote_dir_missing": "kein Ordner",
    }.get(result, "-")


def currentLabel(status, now=None):
    """The AKTUELL / ZULETZT column: what runs, or what happened last."""
    if not status:
        return "noch nie gelaufen"
    if isRunning(status, now):
        path = status.get("currentPath") or ""
        parts = [part for part in os.path.normpath(path).split("/") if part]
        tail = "/".join(parts[-2:]) if len(parts) >= 2 else (parts[-1] if parts else "-")
        progress = status.get("progress")
        percent = "%s %%  " % progress if progress is not None else ""
        return "%s%s  %s" % (percent, tail, humanBytes(status.get("bytes") or 0))
    finishedAt = syncstate.parseIso(status.get("finished")) or syncstate.parseIso(
        status.get("heartbeat")
    )
    if finishedAt is None:
        return "-"
    age = humanDuration((now - finishedAt).total_seconds())
    if status.get("result") in ("connection", "rsync_failed", "partial"):
        return "seit %s" % age
    return "vor %s" % age


def disturbanceLines(alertState, configs, now=None):
    """One line per host with open endpoints, like the GUI shows them."""
    now = now or syncstate.utcNow()
    openEndpoints = [
        (endpoint, entry)
        for endpoint, entry in sorted(alertState.get("endpoints", {}).items())
        if entry.get("open")
    ]
    if not openEndpoints:
        return []
    groups = {}
    for endpoint, entry in openEndpoints:
        groups.setdefault(endpoint.rsplit(":", 1)[0], []).append((endpoint, entry))

    lines = []
    for host, entries in sorted(groups.items()):
        endpoints = [endpoint for endpoint, entry in entries]
        instruments = alerts.instrumentsOnEndpoints(configs, endpoints)
        label = alerts.endpointLabel(endpoints[0], instruments)
        ports = "/".join(":%s" % endpoint.rsplit(":", 1)[1] for endpoint in endpoints)
        reason = entries[0][1].get("lastReason") or "unbekannt"
        reason = reason.split("\n")[0][:40]
        openedAt = min(
            syncstate.parseIso(entry.get("openedAt"))
            for endpoint, entry in entries
            if syncstate.parseIso(entry.get("openedAt"))
        )
        since = humanDuration((now - openedAt).total_seconds())
        acknowledged = all(entry.get("acknowledged") for endpoint, entry in entries)
        lines.append(
            "%s %s  %s  seit %s  %i Instr.  %s"
            % (
                label,
                ports,
                reason,
                since,
                len(instruments),
                "bestätigt" if acknowledged else "gemeldet",
            )
        )
    return lines


def build(configs=None, now=None):
    """Collect everything the GUI and `syncdata.py status` show."""
    now = now or syncstate.utcNow()
    if configs is None:
        configs = _loadConfigs()
    statuses = syncstate.allStatuses()
    alertState = alerts.loadState()

    rows = []
    running = 0
    lastCronAt = None
    for instrument in sorted(set(statuses) | set(configs)):
        status = statuses.get(instrument)
        if isRunning(status, now):
            running += 1
        if status and status.get("source") == "cron":
            startedAt = syncstate.parseIso(status.get("start"))
            if startedAt and (lastCronAt is None or startedAt > lastCronAt):
                lastCronAt = startedAt
        lastDataAt = syncstate.parseIso((status or {}).get("lastDataAt"))
        rows.append(
            {
                "instrument": instrument,
                "status": statusLabel(status, now),
                "source": (status or {}).get("source", "-"),
                "current": currentLabel(status, now),
                "dataAge": (
                    humanDuration((now - lastDataAt).total_seconds())
                    if lastDataAt
                    else "-"
                ),
                "raw": status,
            }
        )

    if lastCronAt is None:
        lastCron = "-"
    else:
        lastCron = "%s (%s)" % (
            lastCronAt.strftime("%H:%M"),
            humanDuration((now - lastCronAt).total_seconds()),
        )

    disturbances = disturbanceLines(alertState, configs, now)
    summary = "syncdata   läuft: %i   Störungen: %i   letzter Cron: %s" % (
        running,
        len(disturbances),
        lastCron,
    )
    mutedUntil = alertState.get("mutedUntil")
    if mutedUntil and syncstate.parseIso(mutedUntil) > now:
        summary += "   stumm bis %s" % mutedUntil.replace("+00:00", " UTC")

    return {
        "summary": summary,
        "rows": rows,
        "disturbances": disturbances,
        "mutedUntil": mutedUntil,
        "alertState": alertState,
        "statuses": statuses,
        "empty": not statuses and not alertState.get("endpoints"),
    }


def _loadConfigs():
    try:
        return syncdata_loadInstruments()
    except Exception:
        return {}


def syncdata_loadInstruments():
    import syncdata

    return syncdata.loadInstruments("%s/instruments" % syncdata.ROOT)
