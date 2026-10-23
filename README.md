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

Install dependencies:
```bash
pip install numpy requests filelock loguru paramiko scp
```

## Usage

```bash
# Sync a specific instrument (syncs last 2 months)
python syncdata.py <instrument_name>

# Sync all instruments - last 2 months (daily mode)
python syncdata.py daily

# Sync all instruments - last 3 days (hourly mode)
python syncdata.py hourly

# Cron mode - syncs based on hour-specific schedules defined in config
python syncdata.py cron

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
- `-v` - Verbose output
- `-r` - Recursive
- `-l` - Copy symlinks as symlinks
- `-t` - Preserve modification times
- `--chmod=F644,D755` - Set permissions on transferred files/dirs (configurable via `chmod` key)
- `--chown=:drops` - Set group ownership on transferred files (configurable via `group` key)
- `--stats` - Show transfer statistics
- `--timeout=45` - I/O timeout in seconds

## Lock Files

The tool creates lock files (`syncdata.<instrument>.lock`) to prevent concurrent syncs of the same instrument. Lock files are automatically managed using the `filelock` library.

## Notifications

Failed syncs or zero-byte transfers can be reported to a Mattermost channel. Configure the `channel` option in the instrument JSON file to enable notifications. Set `mattermostWebhookUrl` in `config.json`; it is not stored in the repository.

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

All keys are optional; the file itself is optional.

## Crontab Setup

Example crontab entry to run every hour:
```cron
0 * * * * /usr/bin/python3 /path/to/syncdata/syncdata.py cron >> /path/to/syncdata/syncdata.py.log 2>&1
```

## Directory Structure

```
syncdata/
├── syncdata.py           # Main synchronization script
├── realtimedata.py       # Real-time data handling (separate tool)
├── config.json.example   # Template for global settings (copy to config.json)
├── instruments/          # Instrument configuration files (git-ignored)
│   ├── template@site.json.example   # Template to copy
│   └── <name>@<site>.json           # Your configs
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

## Authors

(c) M. Maahn, 2012-2026

University of Leipzig - Meteorology Department

## License

GNU General Public License v3.0, see [LICENSE](LICENSE).
