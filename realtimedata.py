#!/usr/bin/env python
# -*- coding: utf-8 -*-

import argparse
import datetime
import errno
import glob
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

import numpy as np
import paramiko
from scp import SCPClient

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


def getRealtimeData(inst, kwargs):
    assert "realtimePath" in kwargs.keys()

    parser = argparse.ArgumentParser()
    parser.add_argument("-p", default="22")
    argString = kwargs["sshOptions"]
    args = parser.parse_args(shlex.split(argString))
    print(kwargs["host"], int(args.p), kwargs["user"])
    try:
        ssh = createSSHClient(kwargs["host"], int(args.p), kwargs["user"])
    except Exception as e:
        print(e)
        return 1
    scp = SCPClient(ssh.get_transport(), socket_timeout=timeout)

    if "realtimeFiles" in kwargs.keys():
        for f in kwargs["realtimeFiles"]:
            local_path = f'{kwargs["realtimePath"]}/{os.path.basename(f)}'
            print(inst, f, local_path)
            try:
                scp.get(f, local_path=local_path)
            except Exception as e:
                print(e)
                continue

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
            return 1

    ssh.close()
    return 0


if __name__ == "__main__":
    instrumentDir = "%s/instruments" % ROOT
    # our instruments:
    instrumentDict = dict()

    instruments = sorted(glob.glob("%s/*json" % instrumentDir))
    for instrumentF in instruments:
        instrument = os.path.basename(instrumentF)
        instrument = os.path.splitext(instrument)[0]
        with open(instrumentF) as f:
            print(instrumentF)
            props = json.load(f)
        instrumentDict[instrument] = props

    if len(sys.argv) < 1:
        print("use python realtimedata.py (instrument)")
        print("in normal mode, status of all instruments is fetched")
        print("in instrument mode, only one instrument status is fetched")
        print("instruments: ")
        for key in np.sort(list(instrumentDict.keys())):
            print(key)
        sys.exit()

    hourNow = datetime.datetime.utcnow().hour
    instruments = "all"
    # if mod eis cron, we do the daily circle once a day (and skip the one after!)!

    if len(sys.argv) > 1:
        instruments = sys.argv[1]
        if instruments not in instrumentDict.keys():
            raise KeyError(
                "Did not find instrument %s, use %s"
                % (instruments, list(instrumentDict.keys()))
            )
    else:
        instruments = list(instrumentDict.keys())

    for inst, kwargs in list(instrumentDict.items()):
        if ("realtimeFiles" in kwargs.keys()) or ("realtimeReport" in kwargs.keys()):
            print("*" * 80)
            print(inst)
            getRealtimeData(inst, kwargs)

    # sync data
    # os.system("rsync -av /path/to/quicklooks user@host:/path/to/public_html/")
