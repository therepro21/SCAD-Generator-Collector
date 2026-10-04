# SCAD Generator Collector

<img src="assets/logo.png" alt="SCAD Generator Collector" width="160">

© 2026 Michael P. Thiess

Portable Windows-11-Anwendung für eine nachvollziehbare, deduplizierte Sammlung öffentlicher OpenSCAD-Generatoren.

## Funktionen

- Dunkle Synchronisationszentrale mit Live-Zahlen, Quellenliste, Suche, Filtern, Fehlerdetails und gezielten Wiederholungen.
- **Mit Aktualisierung starten:** neue GitHub-Quellen suchen und bekannte Quellen parallel abarbeiten.
- **Bekannten Rest laden:** ohne erneute Katalogsuche fortsetzen.
- SHA-256-Deduplizierung; SQLite merkt Quellen, Commits und lokale Dateien.
- Windows-taugliche lateinische Dateinamen; Quell-HTML öffnet direkt die ursprüngliche Repository-Seite.
- Repositories mit mehr als 100 SCAD-Dateien werden als XXL markiert und nach aufsteigender Größe zuletzt verarbeitet.
- Unabhängiger Vorschauprozess: OpenSCAD-Bilder, Folder.jpg und Windows-Ordnersymbole.
- Dauerhafte Einstellungen, Standardziel 750.000; GitHub-Token lokal Windows-DPAPI-verschlüsselt.
- Minimieren in den Windows-Infobereich mit Logo; **Öffnen** und **Beenden** im Tray-Menü. Aufgaben laufen beim Minimieren weiter.
- Quellenverzeichnis als CSV; portable Ablage mit einer Haupt-EXE und Unterordner Programm.

## Portable Ablage

```text
SCAD Generator Sammlung/
  SCAD Generator Collector.exe
  Programm/                   Laufzeit, Einstellungen, Token, Git und OpenSCAD
  Generatoren/                Gesammelte Dateien mit Quellverweisen
  .scad-sammler/              Sammlungskatalog und Vorschauzustand
```

Den gesamten Ordner gemeinsam verschieben. Auf einem anderen Windows-Konto den Token erneut eingeben. Microsoft Edge WebView2 Runtime muss verfügbar sein. Git for Windows ist für Quellenabrufe erforderlich; OpenSCAD für Vorschauen. Diese Fremdprogramme unterliegen ihren eigenen Lizenzen und sind nicht im Quellrepository enthalten.

## Aus Quellcode starten / bauen

Python 3.13 unter Windows, Git for Windows und optional OpenSCAD installieren.

```powershell
python -m pip install -r requirements.txt
python scad_sammler.py
python -m unittest test_desktop -v
pyinstaller --noconfirm --clean SCAD-Sammler.spec
```

Build-Ergebnis: `dist/SCAD Generator Collector/`. Für portable Fremdprogramme Git unter `Programm/Git` und OpenSCAD unter `Programm/OpenSCAD` ablegen; deren Lizenztexte beibehalten.

## Grenzen und Sicherheit

Aktuell sucht der automatische Katalog GitHub-Repositories; eine flächendeckende Suche in Reddit, Foren und Mitgliedschaftsportalen ist nicht implementiert. Die gefundenen Dateien sind nicht automatisch druckbar, deutsch benannt oder lizenzfrei. Fremde Bibliotheken können zusätzliche Abhängigkeiten benötigen. Vorschauen sind keine Sicherheits- oder Funktionsprüfung.

Keine Zugangsdaten, Sammlung, Datenbank oder persönlichen Einstellungen gehören ins Repository. Siehe [SECURITY.md](SECURITY.md), [DISCLAIMER.md](DISCLAIMER.md) und [LICENSE](LICENSE). Die öffentliche Bereitstellung erteilt keine pauschale Open-Source-Lizenz.
