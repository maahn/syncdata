#!/usr/bin/env python
# -*- coding: utf-8 -*-

"""Terminal GUI for syncdata. Reads only state/ — it never syncs itself.

Run it on the server via ssh:

    python3 synctui.py

Requires textual; the cron paths never import this module. A click on an
instrument opens a dialog with the status, the last error and start buttons;
a sync started from here runs detached and survives closing the GUI.
"""

import os
import subprocess
import sys

try:
    from textual.app import App, ComposeResult
    from textual.containers import Horizontal, VerticalScroll
    from textual.screen import ModalScreen
    from textual.widgets import Button, DataTable, Footer, Static
except ImportError:
    print("synctui needs textual: pip install textual")
    sys.exit(1)

import alerts
import syncstate
import statusview


def handLogPath(instrument, root=None):
    return os.path.join(
        root or syncstate.ROOT, "logs", "hand_%s.log" % instrument
    )


def startSync(instrument, extraArgs, root=None, popen=None):
    """Start one instrument detached, output in its own file.

    start_new_session=True survives closing the GUI and the ssh session.
    """
    root = root or syncstate.ROOT
    popen = popen or subprocess.Popen
    logPath = handLogPath(instrument, root)
    os.makedirs(os.path.dirname(logPath), exist_ok=True)
    logFile = open(logPath, "a")
    logFile.write(
        "### START %s: syncdata.py %s %s\n"
        % (syncstate.utcNow().strftime("%Y-%m-%d %H:%M:%S"), instrument, " ".join(extraArgs))
    )
    logFile.flush()
    process = popen(
        [sys.executable, os.path.join(root, "syncdata.py"), instrument] + list(extraArgs),
        stdout=logFile,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        start_new_session=True,
        cwd=root,
    )
    logFile.close()
    return process


class StartDialog(ModalScreen):
    """Status of one instrument, last error and start buttons."""

    BINDINGS = [("escape", "cancel", "Abbrechen")]

    def __init__(self, instrument, raw, running):
        self.instrument = instrument
        self.raw = raw or {}
        self.running = running
        super().__init__()

    def compose(self) -> ComposeResult:
        raw = self.raw
        lines = [
            "[b]%s[/b]" % self.instrument,
            "",
            "Status:   %s" % statusview.statusLabel(self.running and raw or raw),
            "Quelle:   %s" % (raw.get("source") or "-"),
            "Modus:    %s" % (raw.get("mode") or "-"),
            "Host:     %s" % (raw.get("hostname") or "-"),
            "Start:    %s" % (raw.get("start") or "-"),
            "Pfad:     %s" % (raw.get("currentPath") or "-"),
            "Bytes:    %s" % statusview.humanBytes(raw.get("bytes") or 0),
            "Ergebnis: %s" % (raw.get("result") or "-"),
        ]
        if raw.get("error"):
            lines += ["", "Letzter Fehler:", raw["error"].split("\n")[0][:200]]
        if self.running:
            lines += ["", "Läuft schon — der Start ist gesperrt."]
        with VerticalScroll(id="startDialog"):
            yield Static("\n".join(lines), id="dialogText")
            with Horizontal(id="dialogButtons"):
                yield Button("letzte 3 Tage", id="startDays", disabled=self.running)
                yield Button("letzte 2 Monate", id="startMonths", disabled=self.running)
                yield Button("Abbrechen", id="cancel")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "cancel":
            self.dismiss(None)
            return
        # re-check: the status may have changed since the dialog opened
        if statusview.isRunning(syncstate.readStatus(self.instrument)):
            self.app.notify("%s läuft schon" % self.instrument, severity="warning")
            self.dismiss(None)
            return
        args = ["--days", "3"] if event.button.id == "startDays" else ["--months", "2"]
        process = startSync(self.instrument, args)
        self.dismiss("gestartet (PID %i)" % process.pid)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ViewerScreen(ModalScreen):
    """A scrollable text viewer (log or events)."""

    BINDINGS = [("escape", "cancel", "Zurück"), ("q", "cancel", "Zurück")]

    def __init__(self, viewTitle, lines):
        self.viewTitle = viewTitle
        self.lines = lines
        super().__init__()

    def compose(self) -> ComposeResult:
        with VerticalScroll(id="viewer"):
            yield Static("[b]%s[/b]\n\n%s" % (self.viewTitle, "\n".join(self.lines)))

    def action_cancel(self) -> None:
        self.dismiss(None)


def historyTail(root=None, limit=200):
    """The last lines of the newest history log."""
    root = root or syncstate.ROOT
    logDir = os.path.join(root, "logs")
    try:
        files = sorted(
            f for f in os.listdir(logDir) if f.startswith("syncdata_") and f.endswith(".log")
        )
    except OSError:
        return ["kein Log gefunden"]
    if not files:
        return ["kein Log gefunden"]
    try:
        with open(os.path.join(logDir, files[-1]), errors="replace") as f:
            return [line.rstrip("\n") for line in f][-limit:]
    except OSError:
        return ["Log konnte nicht gelesen werden"]


def eventLines(limit=100):
    """The last events, formatted."""
    lines = []
    for event in syncstate.readEvents(limit):
        lines.append(
            "%s  %-24s %s  %s"
            % (
                event.get("time", "")[:19].replace("T", " "),
                event.get("instrument") or "-",
                event.get("kind", ""),
                event.get("text", "")[:80],
            )
        )
    return lines or ["keine Ereignisse"]


class SyncTui(App):
    TITLE = "syncdata"
    CSS = """
    #summary { height: 1; padding: 0 1; background: $surface; }
    #instruments { height: 1fr; }
    #disturbances { height: auto; max-height: 40%; padding: 0 1; }
    #dialogText { padding: 1 2; }
    #dialogButtons { height: auto; padding: 1 2; }
    #viewer { padding: 1 2; }
    """
    BINDINGS = [
        ("s", "start", "Sync starten"),
        ("l", "log", "Log"),
        ("e", "events", "Ereignisse"),
        ("a", "ack", "bestätigen"),
        ("m", "mute", "stumm"),
        ("q", "quit", "Ende"),
    ]

    def compose(self) -> ComposeResult:
        yield Static("", id="summary")
        yield DataTable(id="instruments")
        yield Static("", id="disturbances", markup=False)
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#instruments", DataTable)
        table.cursor_type = "row"
        for column in ("INSTRUMENT", "STATUS", "QUELLE", "AKTUELL / ZULETZT", "DATEN VOR"):
            table.add_column(column, key=column)
        self.refreshData()
        self.set_interval(1.5, self.refreshData)

    def refreshData(self) -> None:
        try:
            view = statusview.build()
        except Exception as err:
            self.query_one("#summary", Static).update(
                "syncdata   Fehler beim Lesen von state/: %s" % err
            )
            return
        self.query_one("#summary", Static).update(view["summary"])
        self.query_one("#disturbances", Static).update(
            "\n".join(view["disturbances"]) or "keine Störungen"
        )
        table = self.query_one("#instruments", DataTable)
        table.clear()
        for row in view["rows"]:
            table.add_row(
                row["instrument"],
                row["status"],
                row["source"],
                row["current"],
                row["dataAge"],
                key=row["instrument"],
            )

    def selectedInstrument(self):
        table = self.query_one("#instruments", DataTable)
        return table.coordinate_to_cell_key(table.cursor_coordinate).row_key.value

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        self.openStartDialog(event.row_key.value)

    def openStartDialog(self, instrument) -> None:
        if not instrument:
            self.notify("kein Instrument gewählt", severity="warning")
            return
        raw = syncstate.readStatus(instrument)
        running = statusview.isRunning(raw)
        self.push_screen(StartDialog(instrument, raw, running), self.startDialogClosed)

    def startDialogClosed(self, result) -> None:
        if result:
            self.notify(result)
        self.refreshData()

    def action_start(self) -> None:
        self.openStartDialog(self.selectedInstrument())

    def action_log(self) -> None:
        self.push_screen(ViewerScreen("Letzter Log", historyTail()))

    def action_events(self) -> None:
        self.push_screen(ViewerScreen("Ereignisse", eventLines()))

    def action_ack(self) -> None:
        count = alerts.acknowledgeAll()
        self.notify(
            "%i Störung(en) bestätigt" % count if count else "nichts offen"
        )
        self.refreshData()

    def action_mute(self) -> None:
        until = alerts.mute()
        self.notify("stumm bis %s" % until.replace("+00:00", " UTC"))
        self.refreshData()


def main():
    """Entry point for the syncdata-tui console script."""
    SyncTui().run()


if __name__ == "__main__":
    main()
