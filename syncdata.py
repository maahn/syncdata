#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import errno
import glob
import json
import logging
import os
import re
import smtplib
import ssl
import subprocess
import sys
import tempfile
import time

# from dateutil.relativedelta import relativedelta
from copy import deepcopy

import numpy as np
import requests
from filelock import FileLock, Timeout
from loguru import logger

import syncstate


sshTimeout = 45  # seconds
mattermostTimeout = 10  # seconds
daysAgo = 3
monthsAgo = 2
RSYNC_BIN = "/usr/bin/rsync"
LOCK_DIR = os.path.dirname(os.path.realpath(__file__))

ROOT = os.path.dirname(os.path.abspath(__file__))


def load_config():
    """Load optional settings from config.json in the script directory."""
    try:
        with open(os.path.join(ROOT, "config.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


CONFIG = load_config()
ROOT = CONFIG.get("root", ROOT)
smtp_server = CONFIG.get("smtpServer", "localhost")
port = CONFIG.get("smtpPort", 25)  # For starttls
sender_email = CONFIG.get("senderEmail", "noreply@localhost")
password = CONFIG.get("smtpPassword")

# state/ lives next to the code, or wherever config.json points to
syncstate.ROOT = ROOT


def alertsArmed():
    """True once endpoint alerts replace the per-run messages.

    config.json: "alertsDryRun": false arms them. The default keeps the old
    per-run messages, while alerts.py only logs what it would send.
    """
    return not CONFIG.get("alertsDryRun", True)


## Example for JSON file
# {
#         "user": "joyce",
#         "host": "iek8264.iek.kfa-juelich.de",
#         "sshOptions": "-p 22",
#         "rsyncOptions": "",
#         "CallOnComplete": "/home/hatpro/scripts/mirac-a/data_processing_jue/mirac-a_jue_2020.sh", #script to run after data was received
#         "CallOnCompleteSSH": "ssh -o ConnectTimeout="+str(sshTimeout)+" hatpro@ecir", #ssh connection for call on complete, don't forget to include --timeout=30 for ssh connections
#         "CallOnCompleteTimeout" : 1800, # "timeout in s for data processing"
#         "CallOnCompleteLog": "/home/hatpro/scripts/syncdata/mirac-a_data_jue.log", #logfile for data processing
#         "CallOnCompleteWait": True, #should the syncloop wait for the data processing to finih? True recommended for idl scripts and if several scripts are run on teh same machine
#         "remotePath": "/cygdrive/e/DATA/MIRAC-A", #directory to sync on the remote side
#         "localPath": "/data/obs/site/jue/mirac-a/l0", #local directory
#         "monthString": "%Y/%m", #if monthly folder are created, how are they named. see http://docs.python.org/2/library/datetime.html#strftime-and-strptime-behavior
#         "dayString": "%d", #if daily folder are created, how are they named. see http://docs.python.org/2/library/datetime.html#strftime-and-strptime-behavior
#         "hoursForDaySync": list(range(24)), #in cron mode, at which hours shall the $daysAgo last days be synced
#         "hoursForMonthSync": [2, 12],#in cron mode, at which hours shall the $monthsAgo last months be synced
# }


def mkdir_p(path):
    try:
        os.makedirs(path)
    except OSError as exc:  # Python >2.5
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


def execute_stdout(command, onProgress=None):
    # launch application as subprocess and print output constantly
    process = subprocess.Popen(
        command,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    output = ""

    for line in process.stdout:
        print(line, end="", flush=True)
        output += line
        if onProgress is not None:
            match = RSYNC_PROGRESS.match(line)
            if match:
                onProgress(int(match.group(1)))

    process.wait()
    return process.returncode, output


# --info=progress2 prints updates on one line: " 1,234  45%   1.2MB/s  0:00:30"
RSYNC_PROGRESS = re.compile(r"^\s*[\d,.]+ \s*(\d{1,3})%")


_rsyncProgressSupported = None


def rsyncSupportsProgress():
    """True when the local rsync understands --info=progress2 (>= 3.1.0).

    Cached; a problem running rsync --version means no progress flag.
    """
    global _rsyncProgressSupported
    if _rsyncProgressSupported is None:
        _rsyncProgressSupported = False
        try:
            result = subprocess.run(
                [RSYNC_BIN, "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            match = re.search(r"rsync version (\d+)\.(\d+)", result.stdout)
            if match:
                _rsyncProgressSupported = (int(match.group(1)), int(match.group(2))) >= (
                    3,
                    1,
                )
        except Exception:
            pass
    return _rsyncProgressSupported


def checkConnection(command):
    """Open and immediately close an ssh connection, raise if that fails.

    The ssh output is collected in a file and not in a pipe: with
    ControlPersist the background master keeps stdout/stderr open, so reading
    a pipe until EOF would block until the master exits. Printing the output
    also puts the failure reason into the history log, which output of the
    child process would otherwise bypass.
    """
    with tempfile.TemporaryFile(mode="w+") as outputFile:
        try:
            subprocess.run(
                command.split(),
                stdout=outputFile,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                check=True,
                # ConnectTimeout does not cover a hang after the TCP connect
                timeout=sshTimeout + 15,
            )
        except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
            outputFile.seek(0)
            err.output = outputFile.read().strip()
            print("connection failed: %s" % (err.output or err))
            raise


def errorReason(err):
    return getattr(err, "output", None) or str(err)


def connectionErrorBody(instrument, err):
    return "No connection to %s\n\n%s\n" % (instrument, errorReason(err))


def monthStrings(now, monthsAgo, monthString):
    """Format the months from monthsAgo months before now up to now, oldest first.

    Uses calendar months: subtracting multiples of 30 days skips or repeats
    months, e.g. February when starting from March 31.
    """
    strings = []
    for mm in range(monthsAgo, -1, -1):
        totalMonths = now.year * 12 + (now.month - 1) - mm
        year, month = divmod(totalMonths, 12)
        strings.append(datetime.date(year, month + 1, 1).strftime(monthString))
    return strings


def _assertSyncArgs(kwargs):
    assert "monthString" in list(kwargs.keys())
    assert "sshOptions" in list(kwargs.keys())
    assert "user" in list(kwargs.keys())
    assert "host" in list(kwargs.keys())
    assert "rsyncOptions" in list(kwargs.keys())


def _hostPaths(**kwargs):
    """Pair host, remotePath and localPath per index, like the old zip loop.

    Returns a list of (host, [(remotePath, localPath), ...]) entries.
    """
    hosts = kwargs["host"] if type(kwargs["host"]) is list else [kwargs["host"]]
    remotePaths = (
        kwargs["remotePath"]
        if type(kwargs["remotePath"]) is list
        else [kwargs["remotePath"]]
    )
    localPaths = (
        kwargs["localPath"]
        if type(kwargs["localPath"]) is list
        else [kwargs["localPath"]]
    )
    return [
        (host, [(remotePath, localPath)])
        for host, remotePath, localPath in zip(hosts, remotePaths, localPaths)
    ]


def errorKind(error, output):
    """Map the error of one sync() call to a status error kind.

    A day folder that does not exist yet shortly after midnight is reported
    by rsync as a change_dir failure and is not an error.
    """
    if error is None:
        return None
    text = "%s %s" % (error, output or "")
    if "change_dir" in text and "No such file or directory" in text:
        return "remote_dir_missing"
    return "rsync_failed"


def _runResult(transferredBytes, syncErrors):
    """Aggregate the errors of all sync() calls into one status result."""
    kinds = [errorKind(error, output) for error, output in syncErrors]
    if not kinds:
        return "ok" if transferredBytes > 0 else "no_new_data"
    if all(kind == "remote_dir_missing" for kind in kinds):
        return "remote_dir_missing"
    return "partial" if transferredBytes > 0 else "rsync_failed"


def _notify(kwargs, body):
    """Per-run Mattermost message, replaced by endpoint alerts once armed."""
    if alertsArmed():
        return
    postToMattermost(kwargs.get("channel", ""), kwargs["instrument"], body)


def _runSync(hostPaths, **kwargs):
    """Shared body of monthSync and daySync.

    hostPaths is a list of (host, [(remotePath, localPath), ...]) entries
    with the month or day folders already expanded. Writes the run status
    and events under state/.
    """
    instrument = kwargs["instrument"]
    source = kwargs.get("source", "hand")
    mode = kwargs.get("mode", "day")
    transferredBytes = 0
    c = ""
    s = ""
    e = None
    syncErrors = []
    lockfile = "%s/syncdata.%s.lock" % (LOCK_DIR, instrument)
    lock = FileLock(lockfile, timeout=1)
    tracker = syncstate.StatusTracker(instrument, source=source, mode=mode)

    # Connection test before the sync. Open ssh to host and exit immediatelly.
    # If successful than carry on with sync, if not than skip the sync.
    # This is important for ssh multiplexing, in case that this connection
    # is the master connection. PK 12.2.2019
    try:
        with lock:
            tracker.begin()
            try:
                for host, paths in hostPaths:
                    command = "ssh -o ConnectTimeout=%s %s@%s %s exit" % (
                        sshTimeout,
                        kwargs["user"],
                        host,
                        kwargs["sshOptions"],
                    )
                    print("connecting to %s, lockfile %s" % (command, lockfile))

                    checkConnection(command)
                    print("connection o.k.")

                    for remotePath, localPath in paths:
                        tracker.update(currentPath=remotePath)
                        s, e, b, c = sync(
                            localPath,
                            remotePath,
                            kwargs["sshOptions"],
                            kwargs["user"],
                            host,
                            kwargs["rsyncOptions"],
                            chmod=kwargs.get("chmod", "F644,D755"),
                            group=kwargs.get("group", "drops"),
                            tracker=tracker,
                        )
                        transferredBytes += b
                        if e is not None:
                            syncErrors.append((e, s))
                        if b > 0:
                            tracker.noteBytes()
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
                tracker.finish(result="connection", error=errorReason(err))
                raise
            tracker.finish(
                bytes=transferredBytes, result=_runResult(transferredBytes, syncErrors)
            )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as err:
        print("connection error.")
        body = connectionErrorBody(instrument, err)
        _notify(kwargs, body)
    except Timeout:
        print("lock file error.")
        tracker.locked()
        _notify(kwargs, "Lock file in place for %s \n" % instrument)
    else:
        if transferredBytes == 0:
            print("No data transferred for %s" % instrument)
            body = "No data transferred for %s \n" % instrument
            body += "\n"
            body += "%s\n" % c
            body += "\n"
            body += "%s\n" % s

            if e is not None:
                body += "\n"
                body += "%s\n" % e

            _notify(kwargs, body)

    print("%s sync done, %i bytes transferred" % (mode, transferredBytes))
    return transferredBytes


def monthSync(monthsAgo, **kwargs):
    print("month sync start")

    _assertSyncArgs(kwargs)
    # some instruments do not have monthly folders, so keep things easy
    if (kwargs["monthString"] == "") and (kwargs["dayString"] == ""):
        monthsAgo = 0
    months = monthStrings(syncstate.utcNow(), monthsAgo, kwargs["monthString"])
    hostPaths = [
        (
            host,
            [
                ("%s/%s/" % (remotePath, m), "%s/%s/" % (localPath, m))
                for remotePath, localPath in paths
                for m in months
            ],
        )
        for host, paths in _hostPaths(**kwargs)
    ]
    return _runSync(hostPaths, mode="month", **kwargs)


def daySync(daysAgo, **kwargs):
    print("day sync start")

    _assertSyncArgs(kwargs)
    assert "dayString" in list(kwargs.keys())
    # some instruments do not have daily folders, so keep things easy
    if (kwargs["monthString"] == "") and (kwargs["dayString"] == ""):
        daysAgo = 0
    now = syncstate.utcNow()
    days = []
    for dd in list(range(0, daysAgo + 1))[::-1]:
        stamp = now - datetime.timedelta(days=dd)
        days.append(
            (stamp.strftime(kwargs["monthString"]), stamp.strftime(kwargs["dayString"]))
        )
    hostPaths = [
        (
            host,
            [
                (
                    "%s/%s/%s/" % (remotePath, mm, dd),
                    "%s/%s/%s/" % (localPath, mm, dd),
                )
                for remotePath, localPath in paths
                for mm, dd in days
            ],
        )
        for host, paths in _hostPaths(**kwargs)
    ]
    return _runSync(hostPaths, mode="day", **kwargs)


def sync(LocalPath, RemotePath, sshOptions, user, host, rsyncOptions, chmod="F644,D755", group="drops", tracker=None):
    print("starting sync")
    try:
        mkdir_p(LocalPath)
    except:
        error = "failed: Cannot create directory %s" % LocalPath
        print(error)
        return "", error, 0, ""

    print(syncstate.utcNow().strftime("%y%m%d %H:%M"), "syncing... ")

    chownOpt = "--chown=:%s" % group if group else ""
    chmodOpt = "--chmod=%s" % chmod if chmod else ""
    # No -v: the file lists made up about half of the history logs (4.76 M
    # lines in 59 days). The --stats block is all we parse, and the live
    # view of a running sync comes from state/ instead.
    progressOpt = "--info=progress2 " if rsyncSupportsProgress() else ""
    command = (
        "%s -rlt %s--modify-window=2 %s %s --stats --timeout=%s %s -e 'ssh %s' %s@%s:%s %s"
        % (RSYNC_BIN, progressOpt, chmodOpt, chownOpt, sshTimeout, rsyncOptions, sshOptions, user, host, RemotePath, LocalPath)
    )
    print(command)
    syncResult = ""
    error = None
    transferredBytes = 0

    def onProgress(percent):
        # a status write per progress line would be too much for NFS; at
        # most one update every 5 s
        if tracker is None:
            return
        now = time.monotonic()
        if now - getattr(onProgress, "last", 0) < 5:
            return
        onProgress.last = now
        tracker.update(progress=percent)

    onProgress.last = 0

    try:
        errorcode, syncResult = execute_stdout(command, onProgress=onProgress if progressOpt else None)
    except Exception as Argument:
        print(Argument)
        error = str(Argument)
    else:
        if errorcode != 0:
            error = "rsync exit code %i for %s" % (errorcode, RemotePath)
            print(error)
        # rsync reports stats also after partial failures such as code 23, and
        # what it transferred before a failure is on disk either way
        transferredBytes = parseRsyncOutput(syncResult)

    if tracker is not None:
        tracker.update(progress=None)  # this folder is done

    try:
        os.rmdir(LocalPath)
    except:
        pass
    else:
        print(
            syncstate.utcNow().strftime("%y%m%d %H:%M"),
            "removed %s" % LocalPath,
        )

    print("done sync")

    return syncResult, error, transferredBytes, command


def parseRsyncOutput(syncResult):
    for line in syncResult.split("\n"):
        if line.startswith("Total transferred file size"):
            transferredBytes = float(
                line.split(" ")[-2].replace(",", "").replace(".", "")
            )
            return transferredBytes
    print("Could not parse rsync output")
    return 0


def sendToMattermost(username, channel, text):
    """Post one message to the configured webhook. A problem never raises."""
    url = CONFIG.get("mattermostWebhookUrl", "")
    if not url:
        logger.warning("mattermostWebhookUrl not set in config.json, skipping Mattermost notification")
        return False

    message = json.dumps({"username": username, "channel": channel, "text": text})
    # Send payload as HTTP Post Request to Webhook URL. A notification problem
    # must never stop the sync of the remaining instruments.
    try:
        r = requests.post(url, data=message, timeout=mattermostTimeout)
        r.raise_for_status()
    except requests.RequestException as err:
        print("failed to post to Mattermost: %s" % err)
        return False
    return True


def postToMattermost(channel, instrument, text):
    if channel == "":
        return

    header = (
        "Automatic data transfer with `syncdata.py %s` failed!\n\n"
        % instrument
    )
    sendToMattermost(instrument, channel, header + text)


def writeEmail(tos, subject, body):
    if tos == "":
        return None

    # Create a secure SSL context
    context = ssl.create_default_context()

    # Try to log in to server and send email
    try:
        server = smtplib.SMTP(smtp_server, port)
        server.ehlo()  # Can be omitted
        server.starttls(context=context)  # Secure the connection
        server.ehlo()  # Can be omitted
        if password is not None:
            server.login(sender_email, password)
        # TODO: Send email here
    except Exception as e:
        # Print any error messages to stdout
        print(e)
        return None

    for to in tos.split(","):
        print("Sending email %s %s" % (to, subject))

        message = """\
Subject: %s
To: %s
From: %s

%s""" % (
            subject,
            to,
            sender_email,
            body,
        )

        r = server.sendmail(sender_email, to, message)
    return r


def callExt(
    CallOnComplete,
    CallOnCompleteLog,
    CallOnCompleteWait,
    CallOnCompleteSSH,
    CallOnCompleteTimeout,
):
    l = open(CallOnCompleteLog, "w")
    l.write(syncstate.utcNow().strftime("### START: %y%m%d %H:%M ###\n"))
    l.close()  # little trick to make ist work
    l = open(CallOnCompleteLog, "a")
    if len(CallOnCompleteSSH) > 0:
        command = (
            CallOnCompleteSSH
            + " /usr/bin/nice -n 19 ~/bin/timeout -t "
            + str(CallOnCompleteTimeout)
            + " "
            + CallOnComplete
        )
    else:
        command = (
            "/usr/bin/nice -n 19 ~/bin/timeout -t "
            + str(CallOnCompleteTimeout)
            + " "
            + CallOnComplete
        )
    print("running... ", command)
    p = subprocess.Popen(command, shell=True, stdout=l, stderr=l)
    if CallOnCompleteWait:
        print("waiting for exit...")
        p.wait()
        print("finished")
    return p, l


processes = list()
logfiles = list()


class Tee(object):
    # crontab pipes our output through `tee` (no -a), which truncates the
    # log every run. Keep our own append-only, per-day, timestamped copy
    # so history survives across cron cycles and each line can be tied to
    # the cron run that produced it, regardless of how it's invoked.
    def __init__(self, console, logFile):
        self.console = console
        self.logFile = logFile
        self._buffer = ""

    def write(self, data):
        self.console.write(data)
        self._buffer += data
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            timestamp = syncstate.utcNow().strftime("%Y-%m-%d %H:%M:%S UTC")
            self.logFile.write("[%s] %s\n" % (timestamp, line))

    def flush(self):
        self.console.flush()
        self.logFile.flush()


def setupHistoryLog():
    logDir = "%s/logs" % ROOT
    mkdir_p(logDir)
    logPath = "%s/%s" % (
        logDir,
        syncstate.utcNow().strftime("syncdata_%Y-%m-%d.log"),
    )
    logFile = open(logPath, "a")
    sys.stdout = Tee(sys.stdout, logFile)
    sys.stderr = Tee(sys.stderr, logFile)
    return logFile


def loadInstruments(instrumentDir):
    """Load the instrument configs; unreadable ones are reported and skipped."""
    instrumentDict = dict()

    instruments = sorted(glob.glob("%s/*json" % instrumentDir))
    for instrumentF in instruments:
        instrument = os.path.basename(instrumentF)
        instrument = os.path.splitext(instrument)[0]
        with open(instrumentF) as f:
            try:
                props = json.load(f)
            except:
                body = "failed to read %s" % instrumentF
                print(body)
                postToMattermost("@maxmaahn", "syncdata.py", body)
                continue
        instrumentDict[instrument] = props
    return instrumentDict


def evaluateAlerts(instrumentDict):
    """Evaluate the endpoint alerts after the runs.

    In the default dry-run alerts.py only logs what it would send; set
    "alertsDryRun": false in config.json to arm it. A failure here must
    never stop the sync loop.
    """
    try:
        import alerts

        alerts.evaluate(instrumentDict)
    except Exception as err:
        print("alerts evaluation failed: %s" % err)


def statusMode():
    """Print the current state without the GUI.

    Exit 0: no open disturbance. 1: disturbance open. 2: no status data.
    """
    import statusview

    view = statusview.build()
    print(view["summary"])
    for row in view["rows"]:
        print(
            "%-30s %-15s %-5s %-22s %s"
            % (
                row["instrument"],
                row["status"],
                row["source"],
                row["current"],
                row["dataAge"],
            )
        )
    print("")
    for line in view["disturbances"]:
        print(line)
    if view["empty"]:
        sys.exit(2)
    sys.exit(1 if view["disturbances"] else 0)


@logger.catch
def main():
    setupHistoryLog()
    instrumentDir = "%s/instruments" % ROOT
    instrumentDict = loadInstruments(instrumentDir)

    if len(sys.argv) <= 1:
        print("use python syncdata.py hourly|daily|cron|status|instrument [--days N] [--months N]")
        print(
            "in hourly mode, the "
            + str(daysAgo)
            + " last days are synced for all instruments with hourly mode."
        )
        print(
            "in daily mode, the "
            + str(monthsAgo)
            + " last months are synced for all instruments"
        )
        print("in cron mode, the last months OR last days are synced depending on time")
        print(
            "in instrument mode, the "
            + str(monthsAgo)
            + " last months are synced for the specified instrument,"
        )
        print(
            "  or pass --days N / --months N to choose the range (used by the terminal GUI)"
        )
        print("in status mode, the current state is printed with an exit code")
        print("  (0 ok, 1 disturbance open, 2 no status data)")
        print("the terminal GUI lives in synctui.py")
        print("instruments: ")
        for key in np.sort(list(instrumentDict.keys())):
            print(key)
        sys.exit()

    if sys.argv[1] == "status":
        statusMode()

    hourNow = datetime.datetime.now().hour
    instruments = "all"
    # where a run comes from: the cron job or a hand start (also the GUI)
    source = "cron" if sys.argv[1] == "cron" else "hand"
    # if mod eis cron, we do the daily circle once a day (and skip the one after!)!
    if sys.argv[1] == "cron":
        mode = "cron"
    elif sys.argv[1] in ["daily", "hourly"]:
        mode = sys.argv[1]
    else:
        instruments = sys.argv[1]
        mode = "daily"
        if instruments not in instrumentDict.keys():
            raise KeyError(
                "Did not find instrument %s, use %s"
                % (instruments, list(instrumentDict.keys()))
            )

    # a single instrument can choose its range: --days N or --months N
    runner, span = monthSync, monthsAgo
    if mode == "daily" and instruments != "all":
        options = sys.argv[2:]
        i = 0
        while i < len(options):
            if options[i] == "--days" and i + 1 < len(options):
                runner, span = daySync, int(options[i + 1])
                i += 2
            elif options[i] == "--months" and i + 1 < len(options):
                runner, span = monthSync, int(options[i + 1])
                i += 2
            else:
                raise KeyError(
                    "did not understand option %s, use --days N or --months N"
                    % options[i]
                )

    print("mode", mode, instruments)

    def runOne(aRunner, aSpan, inst, kwargs):
        aRunner(aSpan, source=source, instrument=inst, **kwargs)
        if kwargs["CallOnComplete"] and kwargs["CallOnCompleteLog"]:
            p, l = callExt(
                kwargs["CallOnComplete"],
                kwargs["CallOnCompleteLog"],
                kwargs["CallOnCompleteWait"],
                kwargs["CallOnCompleteSSH"],
                kwargs["CallOnCompleteTimeout"],
            )
            processes.append(p)
            logfiles.append(l)
        evaluateAlerts({inst: instrumentDict[inst]})

    if mode == "cron":
        for inst, kwargs in list(instrumentDict.items()):
            assert type(kwargs["hoursForMonthSync"]) == list
            assert type(kwargs["hoursForDaySync"]) == list
            print(
                inst,
                hourNow,
                "month",
                kwargs["hoursForMonthSync"],
                "day",
                kwargs["hoursForDaySync"],
            )
            if hourNow in kwargs["hoursForMonthSync"]:
                runOne(monthSync, monthsAgo, inst, kwargs)
            elif hourNow in kwargs["hoursForDaySync"]:
                runOne(daySync, daysAgo, inst, kwargs)
            else:
                print("Skipped (not in schedule)", inst)
                continue

    elif mode == "daily":
        for inst, kwargs in list(instrumentDict.items()):
            if instruments != "all" and instruments != inst:
                continue
            print(inst)
            runOne(runner, span, inst, kwargs)

    elif mode == "hourly":
        for inst, kwargs in list(instrumentDict.items()):
            if instruments != "all" and instruments != inst:
                continue
            print(inst)
            runOne(daySync, daysAgo, inst, kwargs)

    else:
        print("did not understand mode. EXIT")

    print("waiting for external commands to finish...")

    for p, l in zip(processes, logfiles):
        p.wait()

        if hasattr(l, "closed") and not l.closed:
            l.write(syncstate.utcNow().strftime("### DONE: %y%m%d %H:%M ###\n"))
            l.flush()
            l.close()

    evaluateAlerts(instrumentDict)

    print(syncstate.utcNow().strftime("%y%m%d %H:%M"), "done")

    # The history log used to be flushed when garbage collection released the
    # Tee objects; the heartbeat threads keep them alive, so flush explicitly.
    sys.stdout.flush()
    sys.stderr.flush()


if __name__ == "__main__":
    main()
