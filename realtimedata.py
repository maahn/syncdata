#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import datetime
import errno
import json
import logging
import os
import shlex
import smtplib
import ssl
import subprocess
import sys
import time
import traceback

# from dateutil.relativedelta import relativedelta
from copy import deepcopy

import paramiko
from scp import SCPClient

import syncdata
import syncstate

timeout = 20

def createSSHClient(server, port, user):
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(server, port, user, timeout=timeout)
    return client


# sshTimeout = 45 #seconds
# daysAgo = 3
# monthsAgo=2

ROOT = os.path.dirname(os.path.abspath(__file__))
try:
    with open(os.path.join(ROOT, "config.json")) as f:
        ROOT = json.load(f).get("root", ROOT)
except FileNotFoundError:
    pass

# state/ lives next to the code, or wherever config.json points to
syncstate.ROOT = ROOT


def getRealtimeData(inst, kwargs):
    """Fetch the realtime files and reports of one instrument.

    Writes the run status under state/, like a data sync does, so the GUI
    shows the fetch. Quicklooks and status files are not instrument data,
    so the no-data alarm is not touched.
    """
    assert "realtimePath" in kwargs.keys()

    tracker = syncstate.StatusTracker(inst, source="realtime", mode="realtime")

    def fetch():
        parser = argparse.ArgumentParser()
        parser.add_argument("-p", default="22")
        argString = kwargs["sshOptions"]
        args = parser.parse_args(shlex.split(argString))
        print(kwargs["host"], int(args.p), kwargs["user"])
        try:
            ssh = createSSHClient(kwargs["host"], int(args.p), kwargs["user"])
        except Exception as e:
            print(e)
            return "connection", str(e)
        scp = SCPClient(ssh.get_transport(), socket_timeout=timeout)

        fetched = 0
        if "realtimeFiles" in kwargs.keys():
            for f in kwargs["realtimeFiles"]:
                local_path = f'{kwargs["realtimePath"]}/{os.path.basename(f)}'
                print(inst, f, local_path)
                try:
                    scp.get(f, local_path=local_path)
                    fetched += 1
                except Exception as e:
                    print(e)
                    continue
            if fetched == 0 and kwargs["realtimeFiles"]:
                ssh.close()
                return "connection", "no realtime file fetched"

        if "realtimeReport" in kwargs.keys():
            fname = f"realtimeReport_{inst}.html"
            command = f'{kwargs["realtimeReport"]} > {fname} 2>&1'
            local_path = f'{kwargs["realtimePath"]}/{fname}'
            print(kwargs["realtimeReport"], ">", local_path)
            stdin, stdout, stderr = ssh.exec_command(command)
            try:
                scp.get(fname, local_path=local_path)
            except Exception as e:
                print(e)
                ssh.close()
                return "connection", "failed to fetch the report"

        ssh.close()
        return "ok", None

    tracker.begin()
    try:
        result, error = fetch()
    except Exception as err:
        print(err)
        tracker.finish(result="connection", error=str(err))
        return 1
    tracker.finish(result=result, error=error)
    return 0 if result == "ok" else 1


def main():
    # loadInstruments is shared with syncdata.py; ROOT stays a module global
    # so tests can patch it.
    instrumentDict = syncdata.loadInstruments("%s/instruments" % ROOT)

    instruments = "all"
    if len(sys.argv) > 1:
        instruments = sys.argv[1]
        if instruments not in instrumentDict.keys():
            raise KeyError(
                "Did not find instrument %s, use %s"
                % (instruments, list(instrumentDict.keys()))
            )

    for inst, kwargs in list(instrumentDict.items()):
        if instruments != "all" and instruments != inst:
            continue
        if ("realtimeFiles" in kwargs.keys()) or ("realtimeReport" in kwargs.keys()):
            print("*" * 80)
            print(inst)
            getRealtimeData(inst, kwargs)

    # sync data
    # os.system("rsync -av /path/to/quicklooks user@host:/path/to/public_html/")


if __name__ == "__main__":
    main()
