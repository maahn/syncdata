#!/usr/bin/env python
# -*- coding: utf-8 -*-

import datetime
import errno
import glob
import json
import logging
import os
import smtplib
import ssl
import subprocess
import sys
import time
import traceback

# from dateutil.relativedelta import relativedelta
from copy import deepcopy

import numpy as np
import requests
from filelock import FileLock, Timeout
from loguru import logger


sshTimeout = 45  # seconds
daysAgo = 3
monthsAgo = 2

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


def execute_stdout(command):
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

    process.wait()
    return process.returncode, output


def monthSync(monthsAgo, **kwargs):
    print("month sync start")

    assert "monthString" in list(kwargs.keys())
    # assert "dayString" in kwargs.keys()
    assert "sshOptions" in list(kwargs.keys())
    assert "user" in list(kwargs.keys())
    assert "host" in list(kwargs.keys())
    assert "rsyncOptions" in list(kwargs.keys())

    transferredBytes = 0
    lockfile = "%s/syncdata.%s.lock" % (
        os.path.dirname(os.path.realpath(__file__)),
        kwargs["instrument"],
    )
    lock = FileLock(lockfile, timeout=1)

    # Connection test before the sync. Open ssh to host and exit immediatelly.
    # If successful than carry on with sync, if not than skip the sync.
    # This is important for ssh multiplexing, in case that this connection
    # is the master connection. PK 12.2.2019
    try:
        with lock:
            # some instruments do not have monthly folders, so keep things easy
            if (kwargs["monthString"] == "") and (kwargs["dayString"] == ""):
                monthsAgo = 0

            # lists of multiple paths are supported for some arguments
            if type(kwargs["host"]) is not list:
                kwargs["host"] = [kwargs["host"]]
            if type(kwargs["remotePath"]) is not list:
                kwargs["remotePath"] = [kwargs["remotePath"]]
            if type(kwargs["localPath"]) is not list:
                kwargs["localPath"] = [kwargs["localPath"]]

            for host, remotePath, localPath in zip(
                kwargs["host"], kwargs["remotePath"], kwargs["localPath"]
            ):
                command = "ssh -o ConnectTimeout=%s %s@%s %s exit" % (
                    sshTimeout,
                    kwargs["user"],
                    host,
                    kwargs["sshOptions"],
                )
                print("connecting to %s, lockfile %s" % (command, lockfile))

                subprocess.check_call(command.split())
                print("connection o.k.")

                c = ""
                s = ""
                e = None

                for mm in list(range(0, monthsAgo + 1))[::-1]:
                    mmString = (
                        datetime.datetime.utcnow() - datetime.timedelta(days=mm * 30)
                    ).strftime(kwargs["monthString"])

                    mmRemotePath = "%s/%s/" % (remotePath, mmString)
                    mmLocalPath = "%s/%s/" % (localPath, mmString)

                    s, e, b, c = sync(
                        mmLocalPath,
                        mmRemotePath,
                        kwargs["sshOptions"],
                        kwargs["user"],
                        host,
                        kwargs["rsyncOptions"],
                        chmod=kwargs.get("chmod", "F644,D755"),
                        group=kwargs.get("group", "drops"),
                    )
                    transferredBytes += b

    except subprocess.CalledProcessError as err:
        print("connection error.")
        body = "No connection to %s \n \n" % kwargs["instrument"]
        body += "\n".join(
            traceback.format_list(traceback.extract_tb(err.__traceback__))
        )
        # writeEmail(kwargs["email"], "Syncdata error %s" % kwargs["instrument"], body)
        postToMattermost(kwargs["channel"], kwargs["instrument"], body)
    except Timeout:
        print("lock file error.")
        body = "Lock file in place for %s \n" % kwargs["instrument"]
        # writeEmail(kwargs["email"], "Syncdata error %s" % kwargs["instrument"], body)
        postToMattermost(kwargs["channel"], kwargs["instrument"], body)
    else:
        if transferredBytes == 0:
            print("No data transferred for %s" % kwargs["instrument"])
            body = "No data transferred for %s \n" % kwargs["instrument"]
            body += "\n"
            body += "%s\n" % c
            body += "\n"
            body += "%s\n" % s

            if e is not None:
                body += "\n"
                body += "%s\n" % e

            # writeEmail(
            #    kwargs["email"], "Syncdata no data for %s" % kwargs["instrument"], body
            # )
            postToMattermost(kwargs["channel"], kwargs["instrument"], body)

    print("month sync done, %i bytes transferred" % transferredBytes)
    return transferredBytes


def daySync(daysAgo, **kwargs):
    assert "monthString" in list(kwargs.keys())
    assert "dayString" in list(kwargs.keys())
    assert "sshOptions" in list(kwargs.keys())
    assert "user" in list(kwargs.keys())
    assert "host" in list(kwargs.keys())
    assert "rsyncOptions" in list(kwargs.keys())

    transferredBytes = 0
    lockfile = "%s/syncdata.%s.lock" % (
        os.path.dirname(os.path.realpath(__file__)),
        kwargs["instrument"],
    )
    lock = FileLock(lockfile, timeout=1)

    # Connection test before the sync. Open ssh to host and exit immediatelly.
    # If successful than carry on with sync, if not than skip the sync.
    # This is important for ssh multiplexing, in case that this connection
    # is the master connection. PK 12.2.2019
    try:
        with lock:
            # some instruments do not have monthly folders, so keep things easy
            if (kwargs["monthString"] == "") and (kwargs["dayString"] == ""):
                daysAgo = 0

            # lists of multiple paths are supported for some arguments
            if type(kwargs["host"]) is not list:
                kwargs["host"] = [kwargs["host"]]
            if type(kwargs["remotePath"]) is not list:
                kwargs["remotePath"] = [kwargs["remotePath"]]
            if type(kwargs["localPath"]) is not list:
                kwargs["localPath"] = [kwargs["localPath"]]

            for host, remotePath, localPath in zip(
                kwargs["host"], kwargs["remotePath"], kwargs["localPath"]
            ):
                command = "ssh -o ConnectTimeout=%s %s@%s %s exit" % (
                    sshTimeout,
                    kwargs["user"],
                    host,
                    kwargs["sshOptions"],
                )
                print("connecting to %s, lockfile %s" % (command, lockfile))

                subprocess.check_call(command.split())
                print("connection o.k.")

                for dd in list(range(0, daysAgo + 1))[::-1]:
                    mmString = (
                        datetime.datetime.utcnow() - datetime.timedelta(days=dd)
                    ).strftime(kwargs["monthString"])
                    ddString = (
                        datetime.datetime.utcnow() - datetime.timedelta(days=dd)
                    ).strftime(kwargs["dayString"])

                    ddRemotePath = "%s/%s/%s/" % (remotePath, mmString, ddString)
                    ddLocalPath = "%s/%s/%s/" % (localPath, mmString, ddString)

                    s, e, b, c = sync(
                        ddLocalPath,
                        ddRemotePath,
                        kwargs["sshOptions"],
                        kwargs["user"],
                        host,
                        kwargs["rsyncOptions"],
                        chmod=kwargs.get("chmod", "F644,D755"),
                        group=kwargs.get("group", "drops"),
                    )
                    transferredBytes += b
            print("day transfer done, transferred %i bytes" % transferredBytes)
    except subprocess.CalledProcessError as err:
        body = "Connection error for %s\n" % kwargs["instrument"]
        body += "\n".join(
            traceback.format_list(traceback.extract_tb(err.__traceback__))
        )
        # writeEmail(kwargs["email"], "Syncdata error %s" % kwargs["instrument"], body)
        postToMattermost(kwargs["channel"], kwargs["instrument"], body)

    except Timeout:
        print("lock file error.")
        body = "Lock file in place for %s\n" % kwargs["instrument"]
        # writeEmail(kwargs["email"], "Syncdata error %s" % kwargs["instrument"], body)
        postToMattermost(kwargs["channel"], kwargs["instrument"], body)

    else:
        if transferredBytes == 0:
            body = "no data transferred for %s\n" % kwargs["instrument"]
            body += "\n"
            body += "%s\n" % c
            body += "\n"
            body += "%s\n" % s
            if e is not None:
                body += "\n"
                body += "%s\n" % e

            # writeEmail(
            #     kwargs["email"], "Syncdata no data for %s" % kwargs["instrument"], body
            # )
            postToMattermost(kwargs["channel"], kwargs["instrument"], body)

    return transferredBytes


def sync(LocalPath, RemotePath, sshOptions, user, host, rsyncOptions, chmod="F644,D755", group="drops"):
    print("starting sync")
    try:
        mkdir_p(LocalPath)
    except:
        print("failed: Cannot create directory", LocalPath)
        return "failed: Cannot create directory", LocalPath

    print(datetime.datetime.utcnow().strftime("%y%m%d %H:%M"), "syncing... ")

    chownOpt = "--chown=:%s" % group if group else ""
    chmodOpt = "--chmod=%s" % chmod if chmod else ""
    command = (
        "/usr/bin/rsync -vrlt --modify-window=2 %s %s --stats --timeout=%s %s -e 'ssh %s' %s@%s:%s %s"
        % (chmodOpt, chownOpt, sshTimeout, rsyncOptions, sshOptions, user, host, RemotePath, LocalPath)
    )
    print(command)
    try:
        errorcode, syncResult = execute_stdout(command)
        error = None
    except Exception as Argument:
        print(Argument)
        error = str(Argument)

    if errorcode != 0:
        transferredBytes = 0
    else:
        transferredBytes = parseRsyncOutput(syncResult)

    try:
        os.rmdir(LocalPath)
    except:
        pass
    else:
        print(
            datetime.datetime.utcnow().strftime("%y%m%d %H:%M"),
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


def postToMattermost(channel, instrument, text):
    if channel == "":
        return

    header = (
        "Automatic data transfer with `syncdata.py %s` failed!\n\n"
        % instrument
    )
    url = CONFIG.get("mattermostWebhookUrl", "")
    if not url:
        logger.warning("mattermostWebhookUrl not set in config.json, skipping Mattermost notification")
        return

    message = {
        "username": instrument,
        "channel": channel,
        "text": header + text,
    }
    message = json.dumps(message)
    # Send payload as HTTP Post Request to Webhook URL
    r = requests.post(url, data=message)
    r.raise_for_status()
    return


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
    l.write(datetime.datetime.utcnow().strftime("### START: %y%m%d %H:%M ###\n"))
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
            timestamp = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
            self.logFile.write("[%s] %s\n" % (timestamp, line))

    def flush(self):
        self.console.flush()
        self.logFile.flush()


def setupHistoryLog():
    logDir = "%s/logs" % ROOT
    mkdir_p(logDir)
    logPath = "%s/%s" % (
        logDir,
        datetime.datetime.utcnow().strftime("syncdata_%Y-%m-%d.log"),
    )
    logFile = open(logPath, "a")
    sys.stdout = Tee(sys.stdout, logFile)
    sys.stderr = Tee(sys.stderr, logFile)
    return logFile


@logger.catch
def main():
    setupHistoryLog()
    instrumentDir = "%s/instruments" % ROOT
    # our instruments:
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

    if len(sys.argv) <= 1:
        print("use python syncdata.py hourly|daily|cron|instrument")
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
            + " last months are synced for the specified instrument"
        )
        print("instruments: ")
        for key in np.sort(list(instrumentDict.keys())):
            print(key)
        sys.exit()

    hourNow = datetime.datetime.now().hour
    instruments = "all"
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

    print("mode", mode, instruments)

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
                monthSync(monthsAgo, instrument=inst, **kwargs)
            elif hourNow in kwargs["hoursForDaySync"]:
                daySync(daysAgo, instrument=inst, **kwargs)
            else:
                print("Skipped (not in schedule)", inst)
                continue
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

    elif mode == "daily":
        for inst, kwargs in list(instrumentDict.items()):
            if instruments != "all" and instruments != inst:
                continue
            print(inst)
            monthSync(monthsAgo, instrument=inst, **kwargs)
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

    elif mode == "hourly":
        for inst, kwargs in list(instrumentDict.items()):
            if instruments != "all" and instruments != inst:
                continue
            print(inst)
            daySync(daysAgo, instrument=inst, **kwargs)
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

    else:
        print("did not understand mode. EXIT")

    print("waiting for external commands to finish...")

    for p, l in zip(processes, logfiles):
        p.wait()

        if hasattr(l, "closed") and not l.closed:
            l.write(datetime.datetime.utcnow().strftime("### DONE: %y%m%d %H:%M ###\n"))
            l.flush()
            l.close()

    print(datetime.datetime.utcnow().strftime("%y%m%d %H:%M"), "done")


if __name__ == "__main__":
    main()
