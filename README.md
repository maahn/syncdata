# Syncdata

Automated rsync-based data synchronization tool for scientific instruments. Syncs data from remote servers to local storage while preserving file timestamps and permissions.

## Overview

This tool synchronizes data from various VISSS (Video In Situ Snowfall Sensor) instruments and other scientific equipment deployed at different locations (Incheon, Ny-Alesund, Hyytiala, etc.) to local storage.

## Requirements

- Python 3.6+
- Required packages:
  - `numpy`
  - `requests`
  - `filelock`
  - `loguru`
  - `paramiko` (for realtimedata.py)
  - `scp` (for realtimedata.py)
  - `textual` (optional, for the terminal GUI `synctui.py`)

Install dependencies:
```bash
pip install numpy requests filelock loguru paramiko scp
```

Or install the package with all dependencies and short commands (editable, so
`python3 syncdata.py ...` keeps working and `config.json` stays where it is):
```bash
pip install -e .          # in the syncdata directory
pip install -e .[gui]     # additionally pulls in textual for synctui.py
```
This provides the console scripts `syncdata`, `syncdata-tui`, `syncdata-alerts`
and `syncdata-realtime`, each equivalent to `python3 <module>.py ...`. They
resolve `config.json`, `instruments/` and `state/` next to the checkout, so the
package must stay installed editable (or run from the checkout).

## Usage

```bash
# Sync a specific instrument (syncs last 2 months)
python syncdata.py <instrument_name>
# ... or via the installed console script
syncdata <instrument_name>

# Sync a specific instrument - choose the range
python syncdata.py <instrument_name> --days 3
python syncdata.py <instrument_name> --months 2

# Sync all instruments - last 2 months (daily mode)
python syncdata.py daily

# Sync all instruments - last 3 days (hourly mode)
python syncdata.py hourly

# Cron mode - syncs based on hour-specific schedules defined in config
python syncdata.py cron

# Print the current state without the GUI (exit 0 ok, 1 disturbance, 2 no data)
python syncdata.py status

# Terminal GUI (reads only state/, never syncs itself)
python synctui.py

# Endpoint alerts - evaluate once (called from cron or after sync runs)
python alerts.py

# Check and report problems to Mattermost (only sends when something is wrong)
python alerts.py --overview

# Simulate the alerts on recorded history logs
python alerts.py --replay logs/

# List available instruments
python syncdata.py
```

### Examples

```bash
# Sync one instrument
python syncdata.py visss1_follower_data@icn

# Run in cron mode (typically called from crontab)
python syncdata.py cron
```

## Configuration

Instrument configurations are stored as JSON files in the `instruments/` directory, named `<instrument_name>@<site_code>.json`. Site-specific configs are git-ignored; to add an instrument, copy the template and edit it:

```bash
cp "instruments/template@site.json.example" instruments/myinstrument@site.json
```

Files not ending in `.json` (such as the template) are ignored by the scripts. `realtime*` keys are optional.

### Configuration File Format

```json
{
  "user": "username",
  "host": "remote.server.com",
  "sshOptions": "-p 22",
  "rsyncOptions": "--exclude='*.txt' --compress",
  "CallOnComplete": "",
  "CallOnCompleteSSH": "",
  "CallOnCompleteTimeout": 1200,
  "CallOnCompleteLog": "/path/to/logfile.log",
  "CallOnCompleteWait": false,
  "remotePath": "/remote/data/path/",
  "localPath": "/local/data/path/",
  "monthString": "%Y/%m",
  "dayString": "%d",
  "hoursForDaySync": [1, 3, 5, 7, 9, 11, 13, 15, 17, 19, 21, 23],
  "hoursForMonthSync": [13],
  "channel": "",
  "chmod": "F644,D755",
  "group": "drops"
}
```

### Configuration Options

| Option | Description |
|--------|-------------|
| `user` | SSH username for remote connection |
| `host` | Remote server hostname (can be a list for multiple hosts) |
| `sshOptions` | Additional SSH options (e.g., `-p 2201` for custom port) |
| `rsyncOptions` | Additional rsync options (e.g., `--exclude`, `--compress`) |
| `remotePath` | Path to data on remote server (can be a list) |
| `localPath` | Local destination path (can be a list) |
| `monthString` | strftime format for monthly subdirectories (e.g., `%Y/%m`) |
| `dayString` | strftime format for daily subdirectories (e.g., `%d`) |
| `hoursForDaySync` | List of hours (0-23) when daily sync should run in cron mode |
| `hoursForMonthSync` | List of hours (0-23) when monthly sync should run in cron mode |
| `CallOnComplete` | Script to execute after successful sync |
| `CallOnCompleteSSH` | SSH command prefix for remote script execution |
| `CallOnCompleteTimeout` | Timeout in seconds for post-sync script |
| `CallOnCompleteLog` | Log file for post-sync script output |
| `CallOnCompleteWait` | Whether to wait for post-sync script to complete |
| `channel` | Mattermost channel for notifications (empty to disable) |
| `chmod` | rsync `--chmod` value for transferred files/dirs (default: `F644,D755`) |
| `group` | Local group ownership for transferred files (default: `drops`; set to `""` to disable, as in the template) |

## Realtime Data Tool

The `realtimedata.py` script fetches real-time status files and reports from instruments via SCP.

### Usage

```bash
# Fetch real-time data from all configured instruments
python realtimedata.py

# Fetch real-time data from a specific instrument
python realtimedata.py <instrument_name>
```

### Realtime Configuration Options

Add these options to instrument JSON files to enable real-time data fetching:

| Option | Description |
|--------|-------------|
| `realtimePath` | Local directory to store fetched files |
| `realtimeFiles` | List of remote file paths to fetch |
| `realtimeMaxAge` | Alarm threshold in s: newest fetched realtime file older than this (default 7200 = 2 h) raises a quicklook-freshness alarm |
| `realtimeReport` | Remote command to generate a status report (output saved as HTML) |

### Example Configuration

```json
{
  "realtimePath": "/path/to/quicklooks/",
  "realtimeFiles": ["/remote/path/status.txt", "/remote/path/latest.jpg"],
  "realtimeReport": "/home/user/scripts/generate_report.sh"
}
```

## Rsync Behavior

The tool uses rsync with the following default options:
- `-r` - Recursive
- `-l` - Copy symlinks as symlinks
- `-t` - Preserve modification times
- `--chmod=F644,D755` - Set permissions on transferred files/dirs (configurable via `chmod` key)
- `--chown=:drops` - Set group ownership on transferred files (configurable via `group` key)
- `--stats` - Show transfer statistics
- `--timeout=45` - I/O timeout in seconds
- `--info=progress2` - Transfer progress in percent (only when the local rsync supports it, >= 3.1.0; the progress is written into the run status and shown in the GUI)

Verbose output (`-v`) was removed on purpose: the file lists made up about half of the history logs (4.76 M lines in 59 days). The live view of a running sync comes from `state/` instead.

## Lock Files

The tool creates lock files (`syncdata.<instrument>.lock`) to prevent concurrent syncs of the same instrument. Lock files are automatically managed using the `filelock` library.

## Notifications

### Per-run messages (legacy)

Failed syncs or zero-byte transfers can be reported to a Mattermost channel. Configure the `channel` option in the instrument JSON file to enable notifications. Set `mattermostWebhookUrl` in `config.json`; it is not stored in the repository.

### Endpoint alerts (alerts.py)

One disturbance per endpoint (host and port) instead of one message per instrument and run:

- opens after 2 failed runs in a row, closes after 2 successful ones
- reminds every 24 h while open, with the duration in the message
- a busy lock means "already running" and is never a disturbance
- 48 h without new data is an alarm of its own (all instruments)
- a run hanging much longer than usual (2 h) is an alarm of its own
- realtime quicklooks that stopped updating are an alarm of their own: newest fetched file older than 2 h (per-instrument override `realtimeMaxAge`, s). Catches frozen sensors that still accept scp connections without producing new files. Resolves on its own when new quicklooks arrive
- the realtime fetches (realtimedata.py) feed the same endpoints

Until `"alertsDryRun": false` is set in `config.json`, messages that would be sent are only printed into the history log. `alerts.py --replay logs/` simulates the new logic on recorded history logs, so the message count can be compared before arming. The GUI keys `[a]` (acknowledge) and `[m]` (mute for 12 h) control the reminders.

`alerts.py --overview` reports problems to Mattermost - only when something is wrong (an open disturbance or no new data for a day); everything fine means no message.

## Run Status (state/)

Every sync and realtime fetch writes its status to `state/<instrument>.json`, and one line per event goes to `state/events.jsonl`. The status files are written atomically (temporary file, fsync, rename), and a status problem never stops a sync. A PID is only meaningful together with its hostname, and readers have to tolerate delays because `state/` lives on NFS. The terminal GUI and `syncdata.py status` read only this directory.

## Terminal GUI (synctui.py)

Run it on the server via ssh (`pip install textual` first). It reads only `state/` every 1.5 s and never syncs itself:

```
syncdata   läuft: 2   Störungen: 1   letzter Cron: 13:04 (11 min)
INSTRUMENT            STATUS     QUELLE  AKTUELL / ZULETZT      DATEN VOR
visss2_leader_data    läuft      cron    2026/11/05  142 MB     3 min
visss3_leader_data    Verb.err.  cron    seit 14 h              14 h
─ Störungen ──────────────────────────────────────────────
hyy :2204/:2205  Connection refused  seit 23:10  5 Instr.  gemeldet
[s] Sync starten  [l] Log  [e] Ereignisse  [a] bestätigen  [m] stumm  [q] Ende
```

A click on an instrument opens a dialog with the status, the last error and start buttons ("letzte 3 Tage" / "letzte 2 Monate"). Syncs started from the GUI run detached and survive closing the GUI; the output goes to `logs/hand_<instrument>.log`.

## Global Configuration

Global settings (webhook, SMTP) live in `config.json` in the repository root. It is git-ignored because it contains secrets; copy the template and edit it:

```bash
cp config.json.example config.json
```

| Key | Description |
|-----|-------------|
| `mattermostWebhookUrl` | Mattermost incoming webhook for failure notifications (empty/missing disables them) |
| `smtpServer`, `smtpPort` | SMTP server for e-mail notifications (default: `localhost`, `25`) |
| `senderEmail`, `smtpPassword` | Sender address and optional SMTP password |
| `root` | Base directory containing `instruments/` and `logs/` (default: directory of the script) |
| `alertsDryRun` | `true` (default): alerts.py only logs what it would send and the per-run messages stay active. `false`: arm the endpoint alerts |
| `overviewChannel` | Mattermost channel for the problem report (`alerts.py --overview`, only sends when something is wrong); falls back to the first instrument channel |

All keys are optional; the file itself is optional.

## Crontab Setup

Example crontab entry to run the data sync every hour:
```cron
0 * * * * /usr/bin/python3 /path/to/syncdata/syncdata.py cron >> /path/to/syncdata/syncdata.py.log 2>&1
```

Example crontab entry to fetch real-time data every 10 minutes (at minutes 1, 11, 21, ...):
```cron
1-59/10 * * * * /path/to/python3 /path/to/syncdata/realtimedata.py > /path/to/syncdata/realtimedata.py.log 2>&1
```

Recommended extra entries for the alerts (the time-based alarms also run when nothing syncs) and the daily problem check:
```cron
0,30 * * * * /usr/bin/python3 /path/to/syncdata/alerts.py >> /path/to/syncdata/logs/alerts.log 2>&1
0 7 * * * /usr/bin/python3 /path/to/syncdata/alerts.py --overview >> /path/to/syncdata/logs/alerts.log 2>&1
```

## Directory Structure

```
syncdata/
├── syncdata.py           # Main synchronization script
├── syncstate.py          # Run status and events under state/
├── alerts.py             # Endpoint alerts, replay, problem report
├── statusview.py         # Shared view data for the GUI and `syncdata.py status`
├── synctui.py            # Terminal GUI (textual)
├── realtimedata.py       # Real-time data handling (separate tool)
├── config.json.example   # Template for global settings (copy to config.json)
├── instruments/          # Instrument configuration files (git-ignored)
│   ├── template@site.json.example   # Template to copy
│   └── <name>@<site>.json           # Your configs
├── state/                # Run status per instrument, events, alerts state (auto-generated)
├── logs/                 # Timestamped logs (auto-generated)
├── *.lock                # Lock files (auto-generated)
└── *.log                 # Log files
```

## Troubleshooting

### Files synced but timestamps not preserved
Ensure rsync completes without interruption. The `-t` flag requires rsync to finish properly to set modification times.

### Lock file errors
If a sync is interrupted, lock files may remain. Check for stale lock files:
```bash
ls -la *.lock
```
Remove stale locks manually if needed (ensure no sync is running).

### Connection errors
- Verify SSH connectivity: `ssh -o ConnectTimeout=45 user@host -p port exit`
- Check SSH key authentication is configured
- Verify network/firewall settings

## Tests

```bash
pip install pytest
python3 -m pytest tests -q
```

The tests replace `ssh`, `rsync` and Mattermost with fakes, so no real instrument or webhook is touched. The terminal GUI tests need `textual` (`pip install textual`) and are skipped when it is missing.

## Authors

(c) M. Maahn, 2012-2026

University of Leipzig - Meteorology Department

## License

GNU General Public License v3.0, see [LICENSE](LICENSE).
