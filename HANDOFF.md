# Handoff: syncdata Status, Alarme und Terminal-GUI

Aufgabe für die neue Session: Phase 1 bis 3 umsetzen. Phase 0 ist erledigt.
Danach Phase 4 nur nach Rückfrage. Commit und Push nur, wenn der Nutzer es sagt.

## Ziel

Die Syncs laufen auf dem Server per Cron oder per Hand. Der Nutzer will:

- sehen, was gerade synchronisiert wird, egal ob per Cron oder per Hand
- die letzten Fehler sehen
- keine Flut von Mattermost-Nachrichten mehr
- in einer Terminal-GUI auf ein Instrument klicken und es so starten können

## Stand

- Lokaler Ordner: `/Users/mmaahn/projectsSrv/develLibsSrv/syncdata`, jetzt ein
  Git-Repo auf `main`, verbunden mit https://github.com/maahn/syncdata
- Auf dem Server läuft der Code unter `/home/drops/syncdata`. Dort ist Phase 0
  noch nicht ausgerollt und noch nicht getestet.
- Tests: `python3 -m pytest tests -q`. `tests/conftest.py` ersetzt ssh, rsync,
  Mattermost, Lock-Pfad und `ROOT`. Echte Instrumente werden nie berührt.

Phase 0 hat in `syncdata.py` behoben:

- `checkConnection()`: ssh-Fehlergrund landet im Log. Die Ausgabe läuft bewusst
  über eine Datei, nicht über eine Pipe, weil ein ControlPersist-Master die Pipe
  offen halten würde. Dazu ein Timeout.
- `daySync` schreibt Verbindungsfehler jetzt ins Log, wie `monthSync`.
- `postToMattermost`: Timeout, Fehler stoppen den Lauf nicht mehr.
- `sync()`: gibt immer 4 Werte zurück, kein `NameError` mehr bei
  Startfehlern von rsync.
- rsync-Exit-Code ungleich 0: Bytes werden trotzdem gezählt, der Fehler steht in
  `error` und im Log. Das echte Log vom 22.10. bestätigt: Bei Code 23 gibt rsync
  die Statistik noch aus.
- `monthStrings()`: echte Kalendermonate statt 30-Tage-Schritte.

Noch offen aus Phase 0:

- `datetime.utcnow()` ist seit Python 3.12 als veraltet markiert und erzeugt
  Warnungen. Beim nächsten Umbau mit ersetzen, das Format muss gleich bleiben.
- 33 von 1.436 Läufen im Log haben keine `done`-Zeile. Ursache ungeklärt, in
  Phase 1 prüfen. Möglich ist der inzwischen behobene Abbruch durch Mattermost
  oder `mkdir`, belegt ist das nicht.
- Teilausfälle (etwa Code 30 an einem Tag, während andere Tage Daten liefern)
  stehen jetzt im Log, lösen aber noch keinen Alarm aus. Das kommt mit Phase 1/2.
- README nennt liegen gebliebene Lock-Dateien als Problem. Das ist irreführend:
  Die Dateien bleiben immer liegen, entscheidend ist, ob das Lock gehalten wird.

## Entscheidungen des Nutzers

- `CallOnComplete` läuft weiter auch nach fehlgeschlagenen Syncs. Ein Test sichert
  das ab (`testCallOnCompleteRunsAlsoAfterFailedSync`).
- Alarm-Schwellen: Störung öffnet nach 2 Fehlversuchen in Folge, schließt nach
  2 Erfolgen, Erinnerung alle 24 h, Alarm ohne neue Daten nach 48 h.
- Server: Python 3.12, also ist Textual möglich.
- Der Code liegt auf einem **NFS-Volume**.
- GUI: Ein Klick auf ein Instrument soll es ausführen können.

## Zahlen aus den Logs (24.08. bis 22.10., 59 Tage, 1.436 Läufe)

- 4.395 fehlgeschlagene SSH-Tests, erkannt am fehlenden `connection o.k.`, also
  rund 95 % der Fehlerfälle. Spitze ca. 220 pro Tag (Kalenderwoche 40).
- 3.441 davon entfallen auf 5 Hyytiälä-Konfigurationen mit nur 2 Endpunkten
  (derselbe Host, Ports 2204 und 2205).
- 223 Lock-Fehler, davon 211 in Kalenderwoche 39. Dazu 29 Läufe mit 0 Bytes.
- Laufdauer: Median 11 min, 90 % unter 21 min, maximal 61 min.
- `logs/`: 357 MB, 4,76 Mio. Zeilen, etwa die Hälfte Dateilisten.
- Obergrenze für Nachrichten: 6 der 12 aktiven Konfigurationen haben keinen Kanal.
  Die Kanäle der archivierten Konfigurationen sind nicht geprüft.
- Alte Logs enthalten bei Tageslauf-Fehlern keinen Grund. Das gilt erst ab Phase 0.

## Phase 1: Statusdaten (`syncstate.py`)

- `state/<instrument>.json` pro Instrument, dazu Ereignisse. `state/` in die
  `.gitignore` aufnehmen.
- Inhalt: Quelle (cron oder hand), Hostname, PID, Modus, aktueller Pfad und aktuelle
  Datei, Bytes, Start, Heartbeat, Ergebnis, Fehlerursache.
- Fehlerarten: `connection`, `rsync_failed`, `partial`, `no_new_data`, `locked`,
  `remote_dir_missing`. Ein noch fehlender Tagesordner ist kein Fehler
  (`change_dir ... No such file or directory`, gerade kurz nach Mitternacht).
- Ein Fehler beim Schreiben des Status darf nie den Sync stoppen.
- Doppelten Code in `monthSync` und `daySync` zusammenführen.
- Dateilisten aus der Historie herausnehmen, alte Logs komprimieren und später
  löschen.

### Wegen NFS beachten

- Keine SQLite-Datenbank.
- Status atomar schreiben: temporäre Datei mit Hostname und PID im selben Ordner,
  flushen, `fsync`, dann `os.replace`.
- Anhängen an eine gemeinsame Datei ist über mehrere Hosts nicht atomar. Vor dem
  Bau klären, ob Syncs von mehr als einem Host laufen. Im Zweifel eine
  Ereignisdatei pro Host oder Lauf.
- Änderungen können über Hosts verzögert sichtbar werden. Die GUI muss das
  aushalten, Heartbeat-Schwelle also großzügig wählen (etwa 2 min).
- Eine PID gilt nur zusammen mit dem Hostname.
- **Plan-Korrektur:** Die GUI soll das Lock nicht probeweise greifen. Sonst kann
  ein Cron-Start genau dann auf „belegt“ laufen (Timeout nur 1 s). Einen aktiven
  Lauf stattdessen über den Heartbeat erkennen.
- Prüfen, ob `filelock` auf diesem NFS-Volume zwischen Prozessen und, falls nötig,
  zwischen Hosts wirklich sperrt. Bisher nicht getestet.

## Phase 2: Alarme (`alerts.py`)

- Eine Störung pro Endpunkt (Host und Port) statt einer Meldung pro Instrument
  und Lauf. Bestand in `state/` speichern, damit er Cron-Läufe überdauert.
- Meldungen beim Öffnen, alle 24 h und bei Behebung mit Dauer, zum Beispiel:
  „Hyytiälä :2204/:2205 seit 14 h nicht erreichbar, 5 Instrumente betroffen“.
- Belegtes Lock heißt „läuft schon“. Alarm erst, wenn ein Lauf ungewöhnlich lange
  hängt.
- 0 Bytes: Alarm erst nach 48 h ohne neue Daten. Mit dem Nutzer klären, ob das auch
  für `logs` und `meta` passt.
- Teilausfälle hier berücksichtigen, ohne neue Nachrichtenflut.
- Einführung: Zuerst nur protokollieren, was gesendet würde. Die 59 Tage Logs
  nachspielen und dem Nutzer die neue Nachrichtenzahl zeigen, bevor scharf
  geschaltet wird.

## Phase 3: Terminal-GUI (`synctui.py`, Textual)

```
syncdata   läuft: 2   Störungen: 1   letzter Cron: 13:04 (11 min)
INSTRUMENT            STATUS     QUELLE  AKTUELL / ZULETZT      DATEN VOR
visss2_leader_data    ▶ läuft    cron    2026/11/05  142 MB     3 min
visss3_leader_data    ✖ Verb.    cron    seit 14 h              14 h
─ Störungen ──────────────────────────────────────────────
hyy :2204/:2205  Connection refused  seit 23:10  5 Instr.  gemeldet
[s] Sync starten  [l] Log  [e] Ereignisse  [a] bestätigen  [m] stumm  [q] Ende
```

- Liest nur `state/`, alle 1 bis 2 s. Nur die GUI importiert Textual, Cron nicht.
- Läuft per SSH auf dem Server. Bedienbar mit Maus und Tastatur.
- **Klick auf ein Instrument:** öffnet einen Dialog mit Status, letztem Fehler und
  Start-Knöpfen. Empfehlung: nicht direkt beim Klick starten, weil ein Fehlklick
  sonst einen Sync über 2 Monate auslöst. Mit dem Nutzer abstimmen.
- Der Dialog bietet „letzte 3 Tage“ und „letzte 2 Monate“. Dafür braucht die
  Befehlszeile eine neue Option, denn `syncdata.py <instrument>` kann heute nur
  2 Monate. Der bisherige Aufruf muss gleich bleiben.
- Gestartete Syncs laufen abgekoppelt (`start_new_session=True`, Ausgabe in eine
  Datei) und überleben das Schließen der GUI.
- Läuft das Instrument schon, ist der Start gesperrt und die GUI zeigt den Lauf.
- Zusätzlich `syncdata.py status` ohne GUI, mit sinnvollem Exit-Code.
- Testen mit Textuals `Pilot`, auch den Klick auf eine Zeile.

## Phase 4: nur nach Rückfrage

`realtimedata.py` einbinden, täglicher Überblick nach Mattermost,
rsync-Fortschritt in Prozent, README und `CLAUDE.md` aktualisieren.

## Vorsicht

- `instruments/*.json` und `config.json` enthalten Zugangsdaten und sind ignoriert.
  Webhook-URL nie ausgeben.
- Keine Testläufe gegen echte Instrumente oder den echten Webhook ohne Rückfrage.
- Änderungen am rsync-Befehl betreffen alle Instrumente.
- Die Zeilenangaben in `CLAUDE.md` zu `syncdata.py` sind veraltet.
- Lokal liegt nur eine Kopie der Logs bis 22.10., nicht der aktuelle Serverstand.
