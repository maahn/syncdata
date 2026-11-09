# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an automated rsync-based synchronization tool for scientific instrument data. It syncs data from remote VISSS (Video In Situ Snowfall Sensor) instruments and other equipment at various field sites to local storage, tracks the run status under `state/`, sends endpoint alerts to Mattermost and offers a terminal GUI.

## Core Architecture

### Main Scripts

1. **syncdata.py** - Main synchronization engine
   - Uses rsync over SSH to pull data from remote servers
   - Supports monthly sync (last 2 months) and daily sync (last 3 days); a single instrument can choose its range with `--days N` / `--months N`
   - Implements file locking to prevent concurrent syncs of same instrument
   - Writes the run status of every run to `state/` (via syncstate.py)
   - Posts failure notifications to Mattermost (per-run messages, replaced by the endpoint alerts once armed)
   - Can execute post-processing scripts after every sync (also after failed ones)

2. **syncstate.py** - Run status and events
   - One JSON status per instrument under `state/<instrument>.json`, one event per line in `state/events.jsonl`
   - Atomic writes (temp file with hostname+pid, fsync, `os.replace`) because `state/` lives on NFS
   - A status problem is printed but never raised: writing the status must never stop a sync
   - `StatusTracker` keeps a heartbeat alive from a daemon thread, so the GUI can tell an active run from a finished one

3. **alerts.py** - Endpoint alerts, replay and problem report
   - One disturbance per endpoint (host:port) instead of one message per instrument and run
   - Opens after 2 failed runs in a row, closes after 2 successes, reminds every 24 h with the duration
   - 48 h without new data, runs hanging longer than 2 h and realtime quicklooks older than 2 h (`realtimeMaxAge` override) are alarms of their own
   - State in `state/alerts.json` so cron runs survive
   - `--replay logs/` simulates the logic on recorded history logs; `--overview` reports problems to Mattermost (only when something is wrong, not daily)
   - Until `"alertsDryRun": false` is set in config.json, alerts.py only logs what it would send

4. **statusview.py** - Shared view data (plain strings, no textual)
   - Builds the summary, per-instrument rows and disturbance lines used by the GUI and `syncdata.py status`

5. **synctui.py** - Terminal GUI (textual)
   - Reads only `state/` every 1.5 s, never syncs and never locks
   - A click on an instrument opens a dialog with start buttons; started syncs run detached (`start_new_session=True`)
   - Key bindings: s start, l log, e events, a acknowledge, m mute, q quit

6. **realtimedata.py** - Real-time status fetcher
   - Uses SCP (via paramiko) to fetch status files and reports
   - Writes its run status to `state/` like a data sync does (source "realtime"), so the GUI shows the fetch
   - Loads the instrument configs via the shared `syncdata.loadInstruments` (unreadable ones are reported and skipped); keeps its own `ROOT` module global
   - Configured via optional `realtimeFiles`, `realtimeReport`, and `realtimePath` keys in instrument JSON

### Configuration System

Instrument configurations live in `instruments/*.json`. Each file defines:
- SSH connection parameters (user, host, sshOptions including port)
- Paths (remotePath, localPath - can be lists for multiple sources/destinations)
- Directory structure patterns (monthString, dayString using strftime format)
- Sync scheduling (hoursForDaySync, hoursForMonthSync arrays)
- Post-processing hooks (CallOnComplete, CallOnCompleteSSH, etc.)
- Mattermost channel for error notifications

File naming convention: `<instrument_name>@<site_code>.json` (e.g., `visss1_leader_data@icn.json`)

### Sync Modes

- **daily mode**: Syncs last 2 months for all or specified instrument
- **hourly mode**: Syncs last 3 days for all or specified instrument
- **cron mode**: Syncs based on current hour and instrument's hoursForDaySync/hoursForMonthSync schedules
- **instrument mode**: Syncs specific instrument (same as daily for that instrument), with optional `--days N` / `--months N` to choose the range
- **status mode**: Prints the current state from `state/` and exits (0 ok, 1 disturbance open, 2 no status data)

### Key Behavior Details

**Rsync command structure** (in `sync()`):
- Uses `/usr/bin/rsync -rlt --modify-window=2 --chmod=... --chown=:... --stats --timeout=45`
- `--info=progress2` is added when the local rsync supports it (>= 3.1.0, version-checked once); the percent is throttled into the run status (at most one write every 5 s)
- No `-v`: the file lists made up about half of the history logs (4.76 M lines in 59 days)
- SSH connection test performed before sync to validate multiplexing
- Empty directories are removed after sync

**Lock file mechanism**:
- Lock files named `syncdata.<instrument>.lock` in repository root
- Uses `filelock` library with 1-second timeout
- A busy lock writes a "locked" status snapshot and is never a disturbance; it never hides the active run's status

**Error handling**:
- Connection failures: Sends Mattermost notification with traceback (per-run, until armed)
- Zero-byte transfers: Sends Mattermost notification with rsync output (per-run, until armed)
- Error kinds in the status: `connection`, `rsync_failed`, `partial`, `no_new_data`, `locked`, `remote_dir_missing`
- A remote day folder that does not exist yet shortly after midnight is not an error

**Directory structure syncing**:
- Monthly sync: constructs paths as `{remotePath}/{YYYY/MM}/`
- Daily sync: constructs paths as `{remotePath}/{YYYY/MM}/{DD}/`
- If both monthString and dayString are empty, sync the base path directly

## Common Commands

```bash
# Sync specific instrument (last 2 months)
python syncdata.py visss1_follower_data@icn

# Sync specific instrument - last 3 days or last 2 months
python syncdata.py visss1_follower_data@icn --days 3
python syncdata.py visss1_follower_data@icn --months 2

# Sync all instruments - last 2 months
python syncdata.py daily

# Sync all instruments - last 3 days
python syncdata.py hourly

# Cron mode - hour-based scheduling
python syncdata.py cron

# Print the current state (exit 0 ok, 1 disturbance, 2 no data)
python syncdata.py status

# Terminal GUI
python synctui.py

# Endpoint alerts (evaluate once), problem report, replay on old logs
python alerts.py
python alerts.py --overview
python alerts.py --replay logs/

# Fetch real-time status from all instruments
python realtimedata.py

# Fetch real-time status from specific instrument
python realtimedata.py visss1_leader_data@icn

# List available instruments
python syncdata.py
```

## Dependencies

```bash
pip install numpy requests filelock loguru paramiko scp textual
```

Or install the package: `pip install -e .` (add `.[gui]` for textual). The flat
layout keeps `python3 syncdata.py ...` working; `pyproject.toml` defines the
console scripts `syncdata`, `syncdata-tui`, `syncdata-alerts` and
`syncdata-realtime`. Keep it editable: `ROOT` (config.json, instruments/,
state/) resolves next to the checkout, so a site-packages install would break it.

## Testing Approach

Run the tests with `python3 -m pytest tests -q`. `tests/conftest.py` replaces ssh, rsync and Mattermost, so no real instrument or webhook is touched. The TUI tests need `textual` and are skipped when it is missing; the realtimedata tests stub paramiko when it is not installed.

When modifying the sync logic, be aware that:
- The tool is designed to run in cron and should handle transient failures gracefully
- Changes to rsync command construction (in `sync()`) affect all instruments
- Lock file handling is critical for preventing data corruption
- monthString/dayString parsing uses standard strftime - verify with edge cases like month boundaries
- Writing the status must never stop a sync: syncstate helpers never raise
- The alert state machine counts one outcome per run (deduplicated by the status timestamp)
