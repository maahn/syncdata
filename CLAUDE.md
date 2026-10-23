# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Overview

This is an automated rsync-based synchronization tool for scientific instrument data. It syncs data from remote VISSS (Video In Situ Snowfall Sensor) instruments and other equipment at various field sites to local storage.

## Core Architecture

### Two Main Scripts

1. **syncdata.py** - Main synchronization engine
   - Uses rsync over SSH to pull data from remote servers
   - Supports monthly sync (last 2 months) and daily sync (last 3 days)
   - Implements file locking to prevent concurrent syncs of same instrument
   - Posts failure notifications to Mattermost
   - Can execute post-processing scripts after successful sync

2. **realtimedata.py** - Real-time status fetcher
   - Uses SCP (via paramiko) to fetch status files and reports
   - Separate from rsync-based data sync
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
- **instrument mode**: Syncs specific instrument (same as daily for that instrument)

### Key Behavior Details

**Rsync command structure** (syncdata.py:304-307):
- Uses `/usr/bin/rsync -vrlt -p --chmod=F644,D755 --stats --timeout=45`
- SSH connection test performed before sync to validate multiplexing
- Empty directories are removed after sync (syncdata.py:322-329)

**Lock file mechanism**:
- Lock files named `syncdata.<instrument>.lock` in repository root
- Uses `filelock` library with 1-second timeout
- Mattermost notification sent if lock acquisition fails

**Error handling**:
- Connection failures: Sends Mattermost notification with traceback
- Zero-byte transfers: Sends Mattermost notification with rsync output
- Lock timeouts: Sends Mattermost notification

**Directory structure syncing**:
- Monthly sync: constructs paths as `{remotePath}/{YYYY/MM}/`
- Daily sync: constructs paths as `{remotePath}/{YYYY/MM}/{DD}/`
- If both monthString and dayString are empty, sync the base path directly

## Common Commands

```bash
# Sync specific instrument (last 2 months)
python syncdata.py visss1_follower_data@icn

# Sync all instruments - last 2 months
python syncdata.py daily

# Sync all instruments - last 3 days
python syncdata.py hourly

# Cron mode - hour-based scheduling
python syncdata.py cron

# Fetch real-time status from all instruments
python realtimedata.py

# Fetch real-time status from specific instrument
python realtimedata.py visss1_leader_data@icn

# List available instruments
python syncdata.py
```

## Dependencies

```bash
pip install numpy requests filelock loguru paramiko scp
```

## Testing Approach

When modifying the sync logic, be aware that:
- The tool is designed to run in cron and should handle transient failures gracefully
- Changes to rsync command construction (syncdata.py:304-307) affect all instruments
- Lock file handling is critical for preventing data corruption
- monthString/dayString parsing uses standard strftime - verify with edge cases like month boundaries
