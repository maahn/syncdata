#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Endpoint alerts for syncdata.

One disturbance per endpoint (host and port) instead of one message per
instrument and run:

- opens after 2 failed runs in a row, closes after 2 successful ones
- reminds every 24 h while open, with the duration in the message
- a busy lock means "already running" and is never a disturbance
- 48 h without new data is an alarm of its own (all instruments)
- a run hanging much longer than usual is an alarm of its own
  (the longest observed run was 61 min)

The state lives in state/alerts.json so cron runs survive. Until
"alertsDryRun": false is set in config.json, messages that would be sent are
only printed into the history log.

Evaluate after sync runs, or from its own cron entry (for example every
30 minutes, so the time-based alarms also run when nothing syncs):

    0,30 * * * * /usr/bin/python3 /home/drops/syncdata/alerts.py

Replay recorded history logs to see how many messages the new logic would
have sent, before arming it:

    python3 alerts.py --replay logs/
"""

import argparse
import datetime
import glob
import json
import os
import re

import syncdata
import syncstate

FAILURES_TO_OPEN = 2  # failed runs in a row before a disturbance opens
SUCCESSES_TO_CLOSE = 2  # successful runs in a row before it closes
REMINDER = 24 * 3600  # s; remind every 24 h while a disturbance is open
NO_DATA = 48 * 3600  # s; alarm after 48 h without new data
HANG = 2 * 3600  # s; a run hanging this long is unusual
MUTE = 12 * 3600  # s; [m] in the GUI mutes messages for 12 h
REALTIME_AGE = 2 * 3600  # s; quicklooks older than this are unusual

# results that count as a failed attempt for the endpoint
FAILURE_RESULTS = ("connection", "rsync_failed", "partial")
# results that say the endpoint was reachable
SUCCESS_RESULTS = ("ok", "remote_dir_missing", "no_new_data")


def statePath():
    return syncstate.statePath("alerts")


def loadState():
    try:
        with open(statePath()) as f:
            state = json.load(f)
    except Exception:
        state = {}
    state.setdefault("endpoints", {})
    state.setdefault("noData", {})
    state.setdefault("realtime", {})
    state.setdefault("hanging", {})
    state.setdefault("counted", {})
    state.setdefault("mutedUntil", None)
    return state


def saveState(state):
    syncstate.writeJson(statePath(), state)


def parsePort(sshOptions):
    """The port of an endpoint from the sshOptions, 22 by default."""
    match = re.search(r"-p\s+(\d+)", sshOptions or "")
    return int(match.group(1)) if match else 22


def configEndpoints(config):
    """The endpoints (host and port) of one instrument configuration."""
    hosts = config["host"] if type(config["host"]) is list else [config["host"]]
    return ["%s:%i" % (host, parsePort(config.get("sshOptions", ""))) for host in hosts]


def endpointLabel(key, instruments):
    """A short label: the site code of the instruments when they share one."""
    host = key.rsplit(":", 1)[0]
    sites = set()
    for instrument in instruments:
        if "@" in instrument:
            sites.add(instrument.rsplit("@", 1)[1])
    if len(sites) == 1:
        return sites.pop()
    return host.split(".")[0]


def instrumentsOnEndpoints(configs, endpoints):
    """The instruments whose configuration uses one of the endpoints."""
    result = set()
    for instrument, config in sorted(configs.items()):
        try:
            if set(configEndpoints(config)) & set(endpoints):
                result.add(instrument)
        except KeyError:
            continue
    return result


def channelsOf(configs, endpoints):
    """The Mattermost channels to inform about one of the endpoints."""
    channels = set()
    for instrument in instrumentsOnEndpoints(configs, endpoints):
        channel = configs[instrument].get("channel", "")
        if channel:
            channels.add(channel)
    return sorted(channels)


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


def muted(state, now):
    until = syncstate.parseIso(state.get("mutedUntil"))
    return until is not None and until > now


def deliver(state, kind, text, channels, now, dryRun):
    """Log a message, and post it to Mattermost once alerts are armed.

    A missing channel and an active mute mean the message is only logged.
    """
    if type(channels) is str:
        channels = [channels]
    channels = [channel for channel in channels if channel]
    silent = dryRun or muted(state, now)
    if channels and not silent:
        for channel in channels:
            syncdata.sendToMattermost("syncdata", channel, text)
        syncstate.appendEvent(
            kind, text=text, channel=", ".join(channels), dryRun=False
        )
    else:
        prefix = "alerts dry-run" if dryRun else "alerts"
        if muted(state, now) and not dryRun:
            prefix = "alerts muted until %s" % state.get("mutedUntil")
        print(
            "[%s] would send to %s: %s"
            % (prefix, ", ".join(channels) or "<no channel>", text)
        )
        syncstate.appendEvent(
            kind, text=text, channel=", ".join(channels), dryRun=True
        )


def _endpointState(state, endpoint):
    entry = state["endpoints"].setdefault(
        endpoint,
        {
            "open": False,
            "consecutiveFailures": 0,
            "consecutiveSuccesses": 0,
            "openedAt": None,
            "lastFailureAt": None,
            "lastReason": None,
            "lastMessageAt": None,
            "acknowledged": False,
        },
    )
    entry.setdefault("lastMessageAt", None)
    entry.setdefault("acknowledged", False)
    return entry


def applyOutcomes(state, outcomes, now, configs=None, deliverFn=deliver):
    """Feed one batch of outcomes into the per-endpoint state machine.

    outcomes is a list of dicts with endpoint, ok, reason, instrument and a
    key that identifies the run the outcome came from. Each key is counted
    once, so repeated evaluations of an unchanged status do not count twice.
    Returns the resolved disturbances as (endpoint, duration) pairs.
    """
    configs = configs or {}
    byInstrument = {}
    for outcome in outcomes:
        byInstrument.setdefault(outcome["instrument"], []).append(outcome)

    resolves = []
    for instrument, instrumentOutcomes in sorted(byInstrument.items()):
        key = instrumentOutcomes[0]["key"]
        if key is None or state["counted"].get(instrument) == key:
            continue
        state["counted"][instrument] = key

        for outcome in instrumentOutcomes:
            entry = _endpointState(state, outcome["endpoint"])
            if outcome["ok"]:
                entry["consecutiveFailures"] = 0
                entry["consecutiveSuccesses"] = entry["consecutiveSuccesses"] + 1
                if entry["open"] and entry["consecutiveSuccesses"] >= SUCCESSES_TO_CLOSE:
                    openedAt = syncstate.parseIso(entry["openedAt"])
                    duration = humanDuration((now - openedAt).total_seconds())
                    entry["open"] = False
                    entry["acknowledged"] = False
                    entry["lastMessageAt"] = None
                    resolves.append((outcome["endpoint"], duration))
                    deliverFn(
                        state,
                        "resolve",
                        "%s wieder erreichbar, unterbrochen für %s"
                        % (outcome["endpoint"], duration),
                        channelsOf(configs, [outcome["endpoint"]]),
                        now,
                        dryRun=state.get("dryRun", True),
                    )
            else:
                entry["consecutiveSuccesses"] = 0
                entry["consecutiveFailures"] = entry["consecutiveFailures"] + 1
                entry["lastReason"] = outcome["reason"]
                entry["lastFailureAt"] = now.isoformat()
                if (
                    not entry["open"]
                    and entry["consecutiveFailures"] >= FAILURES_TO_OPEN
                ):
                    entry["open"] = True
                    entry["openedAt"] = now.isoformat()
                    entry["lastMessageAt"] = None
    return resolves


def sendDueMessages(state, configs, now):
    """Send open and reminder messages, grouped per host like the GUI shows."""
    due = []
    for endpoint, entry in sorted(state["endpoints"].items()):
        if not entry["open"]:
            continue
        remindDue = entry["lastMessageAt"] is None or (
            (now - syncstate.parseIso(entry["lastMessageAt"])).total_seconds()
            >= REMINDER
        )
        if remindDue and not entry["acknowledged"]:
            due.append(endpoint)

    groups = {}
    for endpoint in due:
        groups.setdefault(endpoint.rsplit(":", 1)[0], []).append(endpoint)

    for host, endpoints in sorted(groups.items()):
        instruments = instrumentsOnEndpoints(configs, endpoints)
        label = endpointLabel(endpoints[0], instruments)
        ports = "/".join(":%s" % endpoint.rsplit(":", 1)[1] for endpoint in endpoints)
        oldest = min(
            syncstate.parseIso(state["endpoints"][endpoint]["openedAt"])
            for endpoint in endpoints
        )
        duration = humanDuration((now - oldest).total_seconds())
        reason = state["endpoints"][endpoints[0]]["lastReason"] or "unbekannt"
        text = "%s %s seit %s nicht erreichbar (%s), %i Instrumente betroffen: %s" % (
            label,
            ports,
            duration,
            reason,
            len(instruments),
            ", ".join(sorted(instruments)) or "-",
        )
        deliver(
            state,
            "open",
            text,
            channelsOf(configs, endpoints),
            now,
            dryRun=state.get("dryRun", True),
        )
        for endpoint in endpoints:
            state["endpoints"][endpoint]["lastMessageAt"] = now.isoformat()


def checkNoData(state, configs, statuses, now):
    """Alarm for instruments that received no new data for 48 h."""
    for instrument, config in sorted(configs.items()):
        status = statuses.get(instrument) or {}
        lastDataAt = syncstate.parseIso(status.get("lastDataAt"))
        if lastDataAt is None:
            continue  # never transferred data, nothing to compare against
        if status.get("running") and syncstate.heartbeatFresh(status, now):
            continue  # an active run may still be about to receive data
        entry = state["noData"].setdefault(
            instrument, {"open": False, "openedAt": None, "lastMessageAt": None}
        )
        age = (now - lastDataAt).total_seconds()
        if age < NO_DATA:
            if entry["open"]:
                entry["open"] = False
                openedAt = syncstate.parseIso(entry["openedAt"])
                duration = humanDuration((now - openedAt).total_seconds())
                deliver(
                    state,
                    "no_data_resolve",
                    "%s wieder neue Daten, unterbrochen für %s"
                    % (instrument, duration),
                    config.get("channel", ""),
                    now,
                    dryRun=state.get("dryRun", True),
                )
                entry["lastMessageAt"] = None
            continue
        if not entry["open"]:
            entry["open"] = True
            entry["openedAt"] = now.isoformat()
            entry["lastMessageAt"] = None
        remindDue = entry["lastMessageAt"] is None or (
            (now - syncstate.parseIso(entry["lastMessageAt"])).total_seconds()
            >= REMINDER
        )
        if remindDue:
            deliver(
                state,
                "no_data",
                "%s seit %s ohne neue Daten (letzte Daten %s)"
                % (instrument, humanDuration(age), status.get("lastDataAt")),
                config.get("channel", ""),
                now,
                dryRun=state.get("dryRun", True),
            )
            entry["lastMessageAt"] = now.isoformat()


def checkHanging(state, configs, statuses, now):
    """Alarm for runs that hang unusually long."""
    for instrument, status in sorted(statuses.items()):
        entry = state["hanging"].setdefault(instrument, {"lastMessageAt": None})
        if not (status.get("running") and syncstate.heartbeatFresh(status, now)):
            entry["lastMessageAt"] = None  # not running (any more)
            continue
        startedAt = syncstate.parseIso(status.get("start"))
        if startedAt is None:
            continue
        age = (now - startedAt).total_seconds()
        if age < HANG:
            continue
        remindDue = entry["lastMessageAt"] is None or (
            (now - syncstate.parseIso(entry["lastMessageAt"])).total_seconds()
            >= REMINDER
        )
        if remindDue:
            deliver(
                state,
                "hanging",
                "%s läuft seit %s, hängt möglicherweise (gestartet %s)"
                % (instrument, humanDuration(age), status.get("start")),
                configs.get(instrument, {}).get("channel", ""),
                now,
                dryRun=state.get("dryRun", True),
            )
            entry["lastMessageAt"] = now.isoformat()


def realtimeFileAge(config, now):
    """Age of the newest fetched realtime file, in s.

    Returns (age, path) or (None, None) when no file was ever fetched. The
    files are read where the fetches land: alerts.py and realtimedata.py run
    on the same host.
    """
    path = config.get("realtimePath", "")
    files = config.get("realtimeFiles")
    if not path or not files:
        return None, None
    newest = None
    newestPath = None
    for f in files:
        target = os.path.join(path, os.path.basename(f))
        try:
            mtime = os.path.getmtime(target)
        except OSError:
            continue  # not fetched (yet)
        if newest is None or mtime > newest:
            newest = mtime
            newestPath = target
    if newest is None:
        return None, None
    return max(0.0, now.timestamp() - newest), newestPath


def checkRealtimeAge(state, configs, statuses, now):
    """Alarm for realtime quicklooks that stopped updating.

    Catches instruments that are still reachable but produce no new files: a
    frozen sensor accepts scp without writing new data, so the endpoint
    alerts stay quiet. Per-instrument override: "realtimeMaxAge" (s).
    """
    for instrument, config in sorted(configs.items()):
        status = statuses.get(instrument) or {}
        if status.get("running") and syncstate.heartbeatFresh(status, now):
            continue  # a running fetch may still be about to fetch
        age, newestPath = realtimeFileAge(config, now)
        if age is None:
            continue  # nothing fetched yet, nothing to compare
        threshold = config.get("realtimeMaxAge", REALTIME_AGE)
        entry = state["realtime"].setdefault(
            instrument, {"open": False, "openedAt": None, "lastMessageAt": None}
        )
        entry.setdefault("lastMessageAt", None)
        if age < threshold:
            if entry["open"]:
                openedAt = syncstate.parseIso(entry["openedAt"])
                duration = humanDuration((now - openedAt).total_seconds())
                entry["open"] = False
                entry["lastMessageAt"] = None
                deliver(
                    state,
                    "realtime_resolve",
                    "%s wieder neue Quicklooks, unterbrochen für %s"
                    % (instrument, duration),
                    config.get("channel", ""),
                    now,
                    dryRun=state.get("dryRun", True),
                )
            continue
        if not entry["open"]:
            entry["open"] = True
            entry["openedAt"] = now.isoformat()
            entry["lastMessageAt"] = None
        remindDue = entry["lastMessageAt"] is None or (
            (now - syncstate.parseIso(entry["lastMessageAt"])).total_seconds()
            >= REMINDER
        )
        if remindDue:
            deliver(
                state,
                "realtime",
                "%s seit %s ohne neue Quicklooks (letzte Datei %s)"
                % (instrument, humanDuration(age), newestPath),
                config.get("channel", ""),
                now,
                dryRun=state.get("dryRun", True),
            )
            entry["lastMessageAt"] = now.isoformat()


def outcomesFromStatuses(configs, statuses, now):
    """One outcome per instrument status and endpoint, deduplicated later."""
    outcomes = []
    for instrument, config in sorted(configs.items()):
        status = statuses.get(instrument)
        if not status:
            continue
        result = status.get("result")
        if result not in FAILURE_RESULTS and result not in SUCCESS_RESULTS:
            continue
        if result in SUCCESS_RESULTS and status.get("running"):
            continue  # the run is still going, no verdict yet
        try:
            endpoints = configEndpoints(config)
        except KeyError:
            continue
        key = status.get("finished") or status.get("heartbeat")
        for endpoint in endpoints:
            outcomes.append(
                {
                    "endpoint": endpoint,
                    "ok": result in SUCCESS_RESULTS,
                    "reason": status.get("error") or result,
                    "instrument": instrument,
                    "key": key,
                }
            )
    return outcomes


def evaluate(configs=None, now=None):
    """Evaluate everything once. Called after sync runs or by its own cron."""
    now = now or syncstate.utcNow()
    state = loadState()
    state["dryRun"] = not syncdata.alertsArmed()
    if configs is None:
        configs = syncdata.loadInstruments("%s/instruments" % syncdata.ROOT)
    statuses = syncstate.allStatuses()
    outcomes = outcomesFromStatuses(configs, statuses, now)
    applyOutcomes(state, outcomes, now, configs)
    sendDueMessages(state, configs, now)
    checkNoData(state, configs, statuses, now)
    checkRealtimeAge(state, configs, statuses, now)
    checkHanging(state, configs, statuses, now)
    saveState(state)
    return state


def acknowledgeAll(now=None):
    """[a] in the GUI: stop reminders for everything that is open."""
    now = now or syncstate.utcNow()
    state = loadState()
    count = 0
    for entry in state["endpoints"].values():
        if entry["open"] and not entry["acknowledged"]:
            entry["acknowledged"] = True
            count += 1
    for entries in (state["noData"], state["realtime"], state["hanging"]):
        for entry in entries.values():
            if entry.get("open", False) and not entry.get("acknowledged", False):
                entry["acknowledged"] = True
                count += 1
    saveState(state)
    return count


def mute(now=None, hours=MUTE):
    """[m] in the GUI: no messages for the next hours."""
    now = now or syncstate.utcNow()
    state = loadState()
    state["mutedUntil"] = (now + datetime.timedelta(seconds=hours)).isoformat()
    saveState(state)
    return state["mutedUntil"]


# --------------------------------------------------------------------- replay

CONNECTING = re.compile(
    r"^\[(?P<ts>[^\]]+)\] connecting to ssh -o ConnectTimeout=\d+"
    r" \S+@(?P<host>\S+) (?P<opts>.*?) exit,"
)


def parseHistoryLogs(logsDir):
    """Recover (timestamp, endpoint, ok, reason) from the history logs.

    A failed connection test is recognized by the missing "connection o.k."
    line, the way the numbers in the handoff were counted.
    """
    events = []
    pending = None

    def flush():
        nonlocal pending
        if pending is not None:
            events.append(pending)
            pending = None

    for path in sorted(glob.glob("%s/syncdata_*.log" % logsDir)):
        with open(path, errors="replace") as f:
            for line in f:
                match = CONNECTING.match(line)
                if match:
                    flush()
                    pending = {
                        "ts": match.group("ts"),
                        "host": match.group("host"),
                        "port": parsePort(match.group("opts")),
                        "ok": False,
                        "reason": "keine Ursache im Log",
                    }
                    continue
                if " connection o.k." in line and pending is not None:
                    pending["ok"] = True
                elif (
                    " connection failed:" in line or " connection error." in line
                ) and pending is not None:
                    pending["reason"] = line.split("] ", 1)[-1].strip()
    flush()
    return events


def replay(logsDir):
    """Simulate the new alerts on recorded history logs and print the count."""
    state = loadState()
    state["dryRun"] = True
    configs = syncdata.loadInstruments("%s/instruments" % syncdata.ROOT)
    events = parseHistoryLogs(logsDir)
    failures = sum(1 for event in events if not event["ok"])

    def countOnly(*args, **kwargs):
        return None  # replay never sends, it only counts

    sent = {"open": 0, "resolve": 0}
    batches = {}
    for event in events:
        # the live evaluation always sees one moment; group by the minute
        batches.setdefault(event["ts"][:16], []).append(event)

    for minute, batch in sorted(batches.items()):
        now = datetime.datetime.fromisoformat(batch[0]["ts"][:16]).replace(
            tzinfo=datetime.timezone.utc
        )
        outcomes = []
        for event in batch:
            endpoint = "%s:%i" % (event["host"], event["port"])
            outcomes.append(
                {
                    "endpoint": endpoint,
                    "ok": event["ok"],
                    "reason": event["reason"],
                    "instrument": "replay",
                    "key": "%s %s" % (event["ts"], endpoint),
                }
            )
        resolves = applyOutcomes(state, outcomes, now, configs, deliverFn=countOnly)
        sent["resolve"] += len(resolves)
        for endpoint, entry in state["endpoints"].items():
            # count freshly opened disturbances; reminders are throttled the
            # same way, so a longer replay would add them every 24 h
            if entry["open"] and entry["lastMessageAt"] is None:
                entry["lastMessageAt"] = now.isoformat()
                sent["open"] += 1

    print("replayed %i connection tests from %s" % (len(events), logsDir))
    print("failed connection tests: %i" % failures)
    print("messages the new alerts would have sent:")
    print("  disturbance opened: %i" % sent["open"])
    print("  resolved: %i" % sent["resolve"])
    return sent


# -------------------------------------------------------------------- overview


def alertCountLast24h(now=None):
    """How many alert messages were sent in the last 24 h, from the events."""
    now = now or syncstate.utcNow()
    since = now - datetime.timedelta(hours=24)
    count = 0
    for event in syncstate.readEvents(500):
        time = syncstate.parseIso(event.get("time"))
        if time is None or time < since:
            continue
        if event.get("kind") == "alert":
            count += 1
    return count


def overviewText(now=None):
    """A compact text over all instruments, and whether there are problems."""
    import statusview

    now = now or syncstate.utcNow()
    view = statusview.build(now=now)
    configs = statusview.syncdata_loadInstruments()

    results = {}
    for row in view["rows"]:
        results[row["status"]] = results.get(row["status"], 0) + 1
    counts = ", ".join(
        "%i %s" % (count, status) for status, count in sorted(results.items())
    )

    problems = bool(view["disturbances"])
    lines = [
        "syncdata Überblick %s" % now.strftime("%d.%m. %H:%M UTC"),
        "%i Instrumente: %s" % (len(view["rows"]), counts or "-"),
    ]
    lines += ["Störung: %s" % line for line in view["disturbances"]]
    if not view["disturbances"]:
        lines.append("keine Störungen")
    else:
        openEndpoints = [
            endpoint
            for endpoint, entry in sorted(loadState().get("endpoints", {}).items())
            if entry.get("open")
        ]
        affected = instrumentsOnEndpoints(configs, openEndpoints)
        if affected:
            lines.append("Betroffen: %s" % ", ".join(sorted(affected)))

    # instruments without new data for a day or longer
    stale = [
        "%s (%s)" % (row["instrument"], row["dataAge"])
        for row in view["rows"]
        if row["dataAge"] not in ("-", None) and _olderThan(row["dataAge"], hours=24)
    ]
    if stale:
        problems = True
        lines.append("letzte Daten >24 h: %s" % ", ".join(stale))
    lines.append("Alarme letzte 24 h: %i" % alertCountLast24h(now))
    return "\n".join(lines), configs, problems


def _olderThan(dataAge, hours):
    """True when a humanDuration like '14 h' or '2 d' is older than hours."""
    match = re.match(r"(\d+) (s|min|h|d)$", dataAge)
    if not match:
        return False
    value, unit = int(match.group(1)), match.group(2)
    seconds = {"s": 1, "min": 60, "h": 3600, "d": 86400}[unit] * value
    return seconds >= hours * 3600


def overview(now=None):
    """Check the state and report to Mattermost - only when there are errors.

    Everything fine means no message, just a log line. A problem (an open
    disturbance or no new data for a day) sends the overview. In the default
    dry-run nothing is posted, like the alerts. The channel comes from
    "overviewChannel" in config.json, falling back to the first instrument.
    """
    now = now or syncstate.utcNow()
    text, configs, problems = overviewText(now)
    if not problems:
        print("[overview] alles ok, keine Nachricht")
        return text
    channel = syncdata.CONFIG.get("overviewChannel", "")
    if not channel:
        for config in configs.values():
            if config.get("channel", ""):
                channel = config["channel"]
                break
    dryRun = not syncdata.alertsArmed()
    if dryRun:
        print("[overview dry-run] would send to %s:\n%s" % (channel or "<no channel>", text))
        return text
    if not channel:
        print("[overview] no channel configured, only logging:\n%s" % text)
        return text
    syncdata.sendToMattermost("syncdata", channel, text)
    syncstate.appendEvent("overview", text=text, channel=channel)
    return text


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay", metavar="LOGS_DIR", help="simulate on old logs")
    parser.add_argument(
        "--overview",
        action="store_true",
        help="report problems (only sends when something is wrong)",
    )
    args = parser.parse_args()
    if args.replay:
        replay(args.replay)
        return
    if args.overview:
        overview()
        return
    evaluate()


if __name__ == "__main__":
    main()
