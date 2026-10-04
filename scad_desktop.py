from __future__ import annotations

import collections
import base64
from contextlib import contextmanager
import hashlib
import json
import os
import queue
import re
import sqlite3
import threading
import time
import traceback
import webbrowser
from datetime import datetime
from pathlib import Path

PROJECT_URL = "https://github.com/therepro21/SCAD-Generator-Collector"


class DesktopApi:
    """Native API exposed only to the bundled, local HTML interface."""

    def __init__(self, core):
        self.core = core
        self.settings = core.load_app_settings()
        self.root = Path(self.settings["library"])
        self.lock = threading.RLock()
        self.start_lock = threading.Lock()
        self.stop_event = threading.Event()
        self.abort_event = threading.Event()
        self.retries = queue.Queue()
        self.worker = None
        self.window = None
        self.started = None
        self.changed = time.monotonic()
        self.state = {"status": "bereit", "phase": "idle", "current_repo": "", "current_file": "",
                      "file_done": 0, "file_total": None, "catalog_running": False, "catalog_finished": False,
                      "new": 0, "updated": 0, "duplicates": 0, "errors": 0, "mode": ""}
        self.repo_states = {}
        self.logs = collections.deque(maxlen=3000)
        self.log_file = core.PROGRAM_DIR / "activity.log"
        self.log_file.parent.mkdir(parents=True, exist_ok=True)
        if self.log_file.exists():
            try:
                with self.log_file.open("r", encoding="utf-8") as stream:
                    for line in collections.deque(stream, maxlen=500):
                        try:
                            self.logs.append(json.loads(line))
                        except ValueError:
                            pass
            except OSError:
                pass
        self.next_log_id = max((int(item.get("id", 0)) for item in self.logs), default=0) + 1
        self.db_cache = {}
        self.db_cache_time = 0
        self.refresh_stop = threading.Event()
        self.refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self.refresh_thread.start()

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.root / ".scad-sammler" / "sammlung.sqlite3", timeout=3)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def _refresh_loop(self):
        while not self.refresh_stop.is_set():
            try:
                self._refresh_database()
            except Exception as exc:
                with self.lock:
                    self.db_cache["read_error"] = str(exc)
            self.refresh_stop.wait(3)

    def _refresh_database(self):
        database = self.root / ".scad-sammler" / "sammlung.sqlite3"
        if not database.exists():
            with self.lock:
                self.db_cache = {"total": 0, "source_count": 0, "repos": [], "done": 0, "error_count": 0,
                                 "preview_count": 0, "folder_count": 0, "preview_status": "Noch nicht gestartet"}
            return
        with self._connect() as db:
            total = db.execute("SELECT COUNT(*) FROM assets").fetchone()[0]
            sources = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
            repos = [dict(row) for row in db.execute("""
                SELECT r.*,COALESCE(c.saved,0) AS saved FROM repos r
                LEFT JOIN (SELECT repo,COUNT(DISTINCT sha256) AS saved FROM sources GROUP BY repo) c
                ON c.repo=r.full_name ORDER BY
                CASE WHEN r.scad_count<=100 THEN 0 WHEN r.scad_count IS NULL THEN 1 ELSE 2 END,
                COALESCE(r.scad_count,2147483647),r.full_name
            """)]
        folders = list((self.root / "Generatoren").iterdir()) if (self.root / "Generatoren").exists() else []
        folders = [p for p in folders if p.is_dir()]
        previews = sum(1 for folder in folders if (folder / "Folder.jpg").exists())
        preview = "Noch nicht gestartet"
        status_path = self.root / ".scad-sammler" / "vorschauen-status.json"
        if status_path.exists():
            try:
                status = json.loads(status_path.read_text(encoding="utf-8"))
                preview = status.get("status", "Unbekannt")
                if status.get("pid") and not self.core._pid_running(int(status["pid"])):
                    preview = "Beendet"
            except (OSError, ValueError, TypeError):
                pass
        with self.lock:
            self.db_cache = {"total": total, "source_count": sources, "repos": repos,
                             "done": sum(r["status"] == "ok" for r in repos),
                             "error_count": sum(r["status"] == "fehler" for r in repos),
                             "preview_count": previews, "folder_count": len(folders), "preview_status": preview}
            self.db_cache_time = time.monotonic()

    def _log(self, message):
        level = "ERROR" if "FEHLER" in message or "ABBRUCH" in message else "WARN" if "pausiert" in message.lower() or "Zeitlimit" in message else "INFO"
        with self.lock:
            item = {"id": self.next_log_id, "time": datetime.now().strftime("%H:%M:%S"), "level": level, "message": message}
            self.next_log_id += 1
            self.logs.append(item)
            self.changed = time.monotonic()
            try:
                if self.log_file.exists() and self.log_file.stat().st_size > 5_000_000:
                    self.log_file.replace(self.log_file.with_name("activity.previous.log"))
                with self.log_file.open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(item, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def _activity(self, event):
        with self.lock:
            self.changed = time.monotonic()
            if event.get("repo_finished"):
                self.repo_states[event["repo_finished"]] = event["repo_status"]
            self.state.update({k: v for k, v in event.items() if k not in ("repo_finished", "repo_status")})

    def snapshot(self, view="overview", filter_name="all", query="", page=0):
        with self.lock:
            data = dict(self.db_cache)
            state = dict(self.state)
            all_repos = data.pop("repos", [])
            running = bool(self.worker and self.worker.is_alive())
            current = state.get("current_repo", "") if running else ""
            repos = []
            for row in all_repos:
                row = dict(row)
                repo = row["full_name"]
                row["live_status"] = self.repo_states.get(repo, "")
                if repo == current:
                    row["live_status"] = {"check": "Prüft", "download": "Lädt", "extract": "Extrahiert", "ingest": "Speichert"}.get(state.get("phase"), "In Arbeit")
                    row["current_file"] = state.get("current_file", "")
                    row["file_done"] = state.get("file_done", 0)
                    row["file_total"] = state.get("file_total")
                else:
                    row["current_file"] = ""
                    row["file_done"] = None
                    row["file_total"] = row.get("scad_count")
                if query and query.casefold() not in (repo + " " + row.get("description", "")).casefold():
                    continue
                if filter_name == "xxl" and (row.get("scad_count") or 0) <= 100:
                    continue
                if filter_name == "errors" and row["status"] != "fehler":
                    continue
                if view == "collection" and not row["saved"]:
                    continue
                repos.append(row)
            if view == "overview":
                repos.sort(key=lambda r: (0 if r["full_name"] == current else 1 if r["status"] == "fehler" else 2 if r["status"] != "ok" else 3,
                                          1 if (r.get("scad_count") or 0) > 100 else 0, r.get("scad_count") or 0))
            page = max(0, int(page))
            page_size = 6 if view == "overview" else 30
            data["filtered_total"] = len(repos)
            data["page_size"] = page_size
            data["rows"] = repos[page * page_size:(page + 1) * page_size]
            total_repos = len(all_repos)
            completed_this_run = len([value for value in self.repo_states.values() if value in ("fertig", "unverändert")])
            processed = completed_this_run if self.started else data.get("done", 0)
            denominator = len(self.state.get("selected_repos", [])) or total_repos
            data.update({"state": state, "running": running, "stop_requested": self.stop_event.is_set(),
                         "elapsed": int(time.monotonic() - self.started) if self.started else 0,
                         "last_activity_seconds": int(time.monotonic() - self.changed),
                         "total_repos": total_repos, "open_repos": total_repos - data.get("done", 0),
                         "progress_done": processed, "progress_total": denominator,
                         "progress_percent": round(100 * processed / denominator) if denominator else 0,
                         "settings": {"library": str(self.root), "target": self.settings["target"],
                                      "create_previews": self.settings["create_previews"],
                                      "token_saved": self.core.TOKEN_FILE.exists()},
                         "logs": list(self.logs)[-500:]})
            return data

    def start(self, refresh=True, selected=None):
        with self.start_lock:
            if self.worker and self.worker.is_alive():
                return {"ok": False, "message": "Ein Lauf ist bereits aktiv."}
            self.root.mkdir(parents=True, exist_ok=True)
            store = self.core.Store(self.root)
            store.close()
            self.stop_event.clear(); self.abort_event.clear()
            with self.lock:
                self.started = time.monotonic()
                self.changed = self.started
                self.repo_states.clear()
                self.state = {"status": "läuft", "phase": "start", "current_repo": "", "current_file": "",
                              "file_done": 0, "file_total": None, "new": 0, "updated": 0, "duplicates": 0, "errors": 0,
                              "catalog_running": bool(refresh), "catalog_finished": not refresh,
                              "mode": "Neue Quellen + bekannter Rest" if refresh else "Bekannter Rest",
                              "selected_repos": selected or []}
            def run():
                try:
                    if self.settings["create_previews"]:
                        started, detail = self.core.launch_preview_worker(self.root)
                        self._log(detail)
                    collector = self.core.Collector(self.root, self.settings["target"], self.core.load_saved_token(), self._log,
                                                    self.stop_event, activity=self._activity, repo_names=selected,
                                                    retries=self.retries, abort=self.abort_event)
                    stats = collector.run(bool(refresh), False)
                    with self.lock:
                        self.state.update({"status": "gestoppt" if self.stop_event.is_set() else "fertig",
                                           "phase": "idle", "new": stats.new, "duplicates": stats.duplicates,
                                           "updated": stats.updated, "errors": stats.errors})
                except Exception as exc:
                    self._log(f"ABBRUCH: {exc}")
                    with self.lock:
                        self.state.update({"status": "fehler", "phase": "idle", "last_error": str(exc)})
                finally:
                    self._refresh_database()
            self.worker = threading.Thread(target=run, name="SCAD-Download", daemon=True)
            self.worker.start()
            return {"ok": True}

    def stop(self):
        self.stop_event.set()
        self._log("Stop angefordert: aktuelle Quelle abschließen und temporäre Dateien bereinigen.")
        return {"ok": True}

    def retry(self, repo):
        with self.start_lock:
            with self._connect() as db:
                if not db.execute("SELECT 1 FROM repos WHERE full_name=?", (repo,)).fetchone():
                    return {"ok": False, "message": "Quelle ist nicht im lokalen Katalog."}
                if self.state.get("current_repo") == repo and self.worker and self.worker.is_alive():
                    return {"ok": False, "message": "Diese Quelle wird gerade verarbeitet."}
                db.execute("UPDATE repos SET last_commit=NULL,status='neu',error=NULL WHERE full_name=?", (repo,))
            self._log(f"Einzelwiederholung angefordert: {repo}")
            active = bool(self.worker and self.worker.is_alive())
            if active:
                self.retries.put(repo)
        self._refresh_database()
        if not active:
            return self.start(False, [repo])
        return {"ok": True, "message": "Quelle ist für diesen Lauf vorgemerkt."}

    def detail(self, repo):
        with self._connect() as db:
            row = db.execute("SELECT * FROM repos WHERE full_name=?", (repo,)).fetchone()
            if not row:
                return {"ok": False, "message": "Quelle nicht gefunden."}
            files = [dict(r) for r in db.execute("""SELECT s.source_path,s.sha256,a.local_path,a.size
                    FROM sources s JOIN assets a ON a.sha256=s.sha256 WHERE s.repo=? ORDER BY s.source_path LIMIT 100""", (repo,))]
            total = db.execute("SELECT COUNT(*) FROM sources WHERE repo=?", (repo,)).fetchone()[0]
        return {"ok": True, "repo": dict(row), "files": files, "source_count": total}

    def open_source(self, repo):
        with self._connect() as db:
            row = db.execute("SELECT full_name FROM repos WHERE full_name=?", (repo,)).fetchone()
        if not row:
            return {"ok": False, "message": "Quelle nicht gefunden."}
        # Always the original repository page, never the raw file/blob URL.
        webbrowser.open("https://github.com/" + row[0])
        return {"ok": True}

    def open_folder(self, repo=None):
        path = self.root
        if repo:
            with self._connect() as db:
                row = db.execute("SELECT a.local_path FROM assets a JOIN sources s ON s.sha256=a.sha256 WHERE s.repo=? LIMIT 1", (repo,)).fetchone()
            if row:
                path = Path(row[0]).parent
            else:
                return {"ok": False, "message": "Diese Quelle hat noch keine gespeicherten Dateien."}
        if not path.is_dir():
            return {"ok": False, "message": "Ordner ist noch nicht vorhanden."}
        os.startfile(path)
        return {"ok": True}

    def previews(self):
        started, detail = self.core.launch_preview_worker(self.root)
        self._log(detail)
        return {"ok": True, "message": detail}

    def get_settings(self):
        return {**self.settings, "library": str(self.root), "token_saved": self.core.TOKEN_FILE.exists()}

    def save_settings(self, values):
        if self.worker and self.worker.is_alive():
            return {"ok": False, "message": "Bitte den laufenden Download zuerst stoppen."}
        try:
            target = int(values["target"])
            if target < 1:
                raise ValueError("Bitte eine positive Zielzahl eingeben.")
            root = Path(values["library"]).resolve()
            root.mkdir(parents=True, exist_ok=True)
            settings = {"library": str(root), "target": target, "refresh_catalog": True,
                        "create_previews": bool(values.get("create_previews", True))}
            if values.get("delete_token"):
                self.core.delete_saved_token()
            elif str(values.get("token", "")).strip():
                self.core.save_token(values["token"])
            self.core.save_app_settings(settings)
            self.settings = settings
            self.root = root
            store = self.core.Store(root); store.close()
            self._refresh_database()
            self._log("Einstellungen dauerhaft im Unterordner Programm gespeichert.")
            return {"ok": True}
        except (OSError, ValueError, KeyError) as exc:
            return {"ok": False, "message": str(exc)}

    def browse_folder(self):
        import webview
        paths = self.window.create_file_dialog(webview.FileDialog.FOLDER, directory=str(self.root))
        return {"path": paths[0] if paths else ""}

    def export(self):
        store = self.core.Store(self.root)
        try:
            store.export_csv()
        finally:
            store.close()
        return {"ok": True, "message": "Quellenverzeichnis.csv aktualisiert."}

    def shutdown(self):
        self.abort_event.set(); self.stop_event.set(); self.refresh_stop.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=15)


class NativeBridge:
    """Expose methods only, never Python/.NET objects or secret-bearing state."""

    def __init__(self, backend):
        self._backend = backend

    def snapshot(self, view="overview", filter_name="all", query="", page=0):
        return self._backend.snapshot(view, filter_name, query, page)

    def start(self, refresh=True):
        return self._backend.start(refresh)

    def stop(self):
        return self._backend.stop()

    def retry(self, repo):
        return self._backend.retry(repo)

    def detail(self, repo):
        return self._backend.detail(repo)

    def open_source(self, repo):
        return self._backend.open_source(repo)

    def open_folder(self, repo=None):
        return self._backend.open_folder(repo)

    def previews(self):
        return self._backend.previews()

    def get_settings(self):
        return self._backend.get_settings()

    def save_settings(self, values):
        return self._backend.save_settings(values)

    def browse_folder(self):
        return self._backend.browse_folder()

    def export(self):
        return self._backend.export()

    def open_project(self):
        webbrowser.open(PROJECT_URL)
        return {"ok": True}


class TrayController:
    """Keep collection workers alive while the native window is hidden."""
    def __init__(self, window, logo):
        import pystray
        from PIL import Image
        self.window = window
        self.icon = pystray.Icon("SCADGeneratorCollector", Image.open(logo).convert("RGBA"),
            "SCAD Generator Collector", pystray.Menu(
                pystray.MenuItem("Öffnen", self.restore, default=True),
                pystray.MenuItem("Beenden", self.quit)))
        self.thread = threading.Thread(target=self.icon.run, daemon=True)
        self.thread.start()

    def minimized(self):
        self.window.hide()

    def restore(self, *args):
        self.window.show()
        self.window.restore()

    def quit(self, *args):
        self.window.destroy()

    def close(self):
        self.icon.stop()
        self.thread.join(timeout=3)


def render_html(resource_dir: Path) -> str:
    template = (resource_dir / "ui-template.html").read_text(encoding="utf-8")
    script = (resource_dir / "desktop.js").read_text(encoding="utf-8")
    # Keep the exact approved style and SVG sprite; replace prototype behavior.
    template = re.sub(r'<div class="chooser">.*?</div>', '', template, count=1, flags=re.S)
    template = re.sub(r'<script>.*?</script>', lambda _: '<script>' + script + '</script>', template, flags=re.S)
    extra_css = (resource_dir / "desktop.css").read_text(encoding="utf-8")
    logo = base64.b64encode((resource_dir / "assets" / "logo.png").read_bytes()).decode("ascii")
    template = template.replace('<div class="logo"><svg><use href="#cube"/></svg></div>',
        '<div class="logo"><img alt="SCAD Generator Collector" src="data:image/png;base64,' + logo + '"></div>')
    template = template.replace('<b>SCAD Sync</b>', '<b>SCAD Generator<br>Collector</b>')
    template = re.sub(r'<title>.*?</title>', '<title>SCAD Generator Collector</title>', template)
    return template.replace('</head>', '<style>' + extra_css + '</style></head>')


def run_desktop(core, autostart=False):
    import webview
    resource_dir = Path(getattr(__import__('sys'), '_MEIPASS', Path(__file__).parent))
    core.PROGRAM_DIR.mkdir(parents=True, exist_ok=True)
    initial_store = core.Store(Path(core.load_app_settings()["library"]))
    initial_store.close()
    api = DesktopApi(core)
    window = webview.create_window(f"SCAD Generator Collector · {core.VERSION}", html=render_html(resource_dir),
                                   js_api=NativeBridge(api), width=1440, height=900, min_size=(1050, 720),
                                   background_color="#101a25", text_select=True)
    api.window = window
    tray = None
    window.events.closing += api.shutdown
    def loaded():
        nonlocal tray
        if tray is None:
            tray = TrayController(window, resource_dir / "assets" / "logo.png")
            window.events.minimized += tray.minimized
        if autostart:
            api.start(True)
    window.events.loaded += loaded
    try:
        webview.start(gui="edgechromium", private_mode=False, storage_path=str(core.PROGRAM_DIR / "WebView"), debug=False,
                      icon=str(resource_dir / "assets" / "logo.ico"))
    except Exception:
        (core.PROGRAM_DIR / "startup-error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        raise
    finally:
        if tray:
            tray.close()
        api.shutdown()
