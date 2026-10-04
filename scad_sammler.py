from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import html
import json
import os
import queue
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from scad_previews import backfill_previews, ensure_folder_preview


APP_NAME = "SCAD Generator Collector"
VERSION = "2.0.1"
APP_ROOT = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
PROGRAM_DIR = APP_ROOT / "Programm"
DEFAULT_LIBRARY = APP_ROOT if getattr(sys, "frozen", False) else Path.home() / "Downloads" / "SCAD Generator Sammlung"
USER_AGENT = f"SCAD-Generator-Sammler/{VERSION}"
TOKEN_FILE = PROGRAM_DIR / "github-token.bin"
SETTINGS_FILE = TOKEN_FILE.with_name("settings.json")
os.environ["PATH"] = str(PROGRAM_DIR / "Git" / "cmd") + os.pathsep + os.environ.get("PATH", "")
os.environ["SCAD_PORTABLE_OPENSCAD"] = str(PROGRAM_DIR / "OpenSCAD" / "openscad.exe")
SEED_REPOS = [
    "kennetek/gridfinity-rebuilt-openscad", "revarbat/BOSL2", "revarbat/BOSL",
    "tanius/openscad-models", "mbugert/openscad-projects", "maxistar/3d_models",
    "n0ctu/OpenSCAD-Models", "ValterKasper/openscad-models", "AndyLevesque/QuackWorks",
    "nbr0wn/parapart", "jhgaylor/3d-models", "revarbat/snappy-reprap",
    "Irev-Dev/Round-Anything", "riskable/void_switch", "JennyList/Jennys-rectangular-project-box",
]

WORDS = {
    "adapter": "Adapter", "adjustable": "verstellbar", "air": "Luft", "arm": "Arm",
    "axle": "Achse", "base": "Basis", "bearing": "Lager", "belt": "Riemen",
    "bin": "Behaelter", "box": "Box", "bracket": "Halterung", "button": "Taste",
    "cable": "Kabel", "cap": "Kappe", "case": "Gehaeuse", "chain": "Kette",
    "clamp": "Klemme", "clip": "Clip", "connector": "Verbinder", "container": "Behaelter",
    "corner": "Ecke", "cover": "Abdeckung", "customizer": "Konfigurator",
    "desk": "Schreibtisch", "door": "Tuer", "drawer": "Schublade", "drill": "Bohrer",
    "enclosure": "Gehaeuse", "end": "Ende", "fan": "Luefter", "feet": "Fuesse",
    "foot": "Fuss", "frame": "Rahmen", "gear": "Zahnrad", "generator": "Generator",
    "grip": "Griff", "hinge": "Scharnier", "holder": "Halter", "hook": "Haken",
    "insert": "Einsatz", "joint": "Gelenk", "key": "Schluessel", "keyboard": "Tastatur",
    "knob": "Drehknopf", "label": "Etikett", "lid": "Deckel", "light": "Licht",
    "lock": "Schloss", "mount": "Halterung", "organizer": "Organizer", "panel": "Platte",
    "phone": "Telefon", "pipe": "Rohr", "plate": "Platte", "plug": "Stopfen",
    "rack": "Gestell", "ring": "Ring", "roller": "Rolle", "screw": "Schraube",
    "shaft": "Welle", "shelf": "Regal", "socket": "Fassung", "spacer": "Abstandhalter",
    "spool": "Spule", "stand": "Staender", "support": "Stuetzteil", "switch": "Schalter",
    "thread": "Gewinde", "tool": "Werkzeug", "tray": "Ablage", "tube": "Rohr",
    "wall": "Wand", "wheel": "Rad", "with": "mit", "without": "ohne",
    "rounded": "abgerundet", "round": "rund", "ribbed": "geriffelt",
    "parametric": "parametrisch", "printable": "druckbar", "small": "klein",
    "large": "gross", "mini": "mini", "modular": "modular", "test": "Test",
    "containers": "Behaelter", "decorative": "dekorativ", "gears": "Zahnraeder",
    "honeycomb": "Wabenstruktur", "lattice": "Gitterstruktur", "mechanical": "mechanisch",
    "practical": "praktisch", "primitives": "Grundformen", "threads": "Gewinde",
    "voronoi": "Voronoi-Muster", "parts": "Teile", "part": "Teil", "model": "Modell",
    "models": "Modelle", "storage": "Aufbewahrung", "handle": "Handgriff",
    "handles": "Handgriffe", "bottle": "Flasche", "bowl": "Schale", "cup": "Becher",
    "funnel": "Trichter", "nut": "Mutter", "bolt": "Bolzen", "washer": "Unterlegscheibe",
}

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS repos (
  full_name TEXT PRIMARY KEY, clone_url TEXT NOT NULL, html_url TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '', default_branch TEXT NOT NULL DEFAULT 'main',
  pushed_at TEXT NOT NULL DEFAULT '', topics_json TEXT NOT NULL DEFAULT '[]',
  last_commit TEXT, last_scan TEXT, status TEXT NOT NULL DEFAULT 'neu', error TEXT,
  scad_count INTEGER
);
CREATE TABLE IF NOT EXISTS assets (
  sha256 TEXT PRIMARY KEY, local_path TEXT NOT NULL UNIQUE, size INTEGER NOT NULL,
  german_name TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS sources (
  repo TEXT NOT NULL, source_path TEXT NOT NULL, source_url TEXT NOT NULL,
  sha256 TEXT NOT NULL REFERENCES assets(sha256), last_seen_run TEXT NOT NULL,
  PRIMARY KEY(repo, source_path)
);
CREATE INDEX IF NOT EXISTS sources_hash ON sources(sha256);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS runs (
  id TEXT PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
  downloaded INTEGER NOT NULL DEFAULT 0, duplicates INTEGER NOT NULL DEFAULT 0,
  updated INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dpapi(data: bytes, decrypt: bool = False) -> bytes:
    """Protect a secret with Windows DPAPI for the current Windows account."""
    if os.name != "nt":
        raise RuntimeError("Sichere Tokenspeicherung ist nur unter Windows verfügbar.")
    import ctypes
    from ctypes import wintypes

    class DATA_BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    target = DATA_BLOB()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    if decrypt:
        ok = crypt32.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 0, ctypes.byref(target))
    else:
        # CRYPTPROTECT_UI_FORBIDDEN: never show a system prompt in a background sync.
        ok = crypt32.CryptProtectData(ctypes.byref(source), APP_NAME, None, None, None, 1, ctypes.byref(target))
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return ctypes.string_at(target.pbData, target.cbData)
    finally:
        kernel32.LocalFree(target.pbData)


def save_token(token: str) -> None:
    token = token.strip()
    if not token:
        delete_saved_token()
        return
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = TOKEN_FILE.with_suffix(".part")
    temporary.write_bytes(_dpapi(token.encode("utf-8")))
    os.replace(temporary, TOKEN_FILE)


def load_saved_token() -> str:
    try:
        return _dpapi(TOKEN_FILE.read_bytes(), decrypt=True).decode("utf-8")
    except (OSError, UnicodeError, RuntimeError):
        return ""


def delete_saved_token() -> None:
    TOKEN_FILE.unlink(missing_ok=True)


def load_app_settings() -> dict:
    defaults = {"library": str(DEFAULT_LIBRARY), "target": 750000,
                "refresh_catalog": True, "create_previews": True}
    try:
        saved = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(saved, dict):
            defaults.update({key: saved[key] for key in defaults if key in saved})
            if saved.get("portable", True):
                defaults["library"] = str(DEFAULT_LIBRARY)
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        pass
    try:
        defaults["target"] = max(1, int(defaults["target"]))
    except (TypeError, ValueError):
        defaults["target"] = 750000
    return defaults


def save_app_settings(settings: dict) -> None:
    settings = dict(settings)
    settings["portable"] = Path(settings.get("library", DEFAULT_LIBRARY)).resolve() == DEFAULT_LIBRARY.resolve()
    SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
    temporary = SETTINGS_FILE.with_suffix(".part")
    temporary.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, SETTINGS_FILE)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def clean(value: str, limit: int = 120) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "-", value)
    value = re.sub(r"\s+", " ", value).strip(" .")
    value = re.sub(r"[^A-Za-z0-9 ._()\[\]+&@#'=-]", "-", value)
    value = re.sub(r"[-_. ]{2,}", "-", value).strip(" .-")
    value = (value or "Unbenannt")[:limit].rstrip(" .")
    # Windows device names remain reserved even with a file extension.
    stem = value.split(".", 1)[0].upper()
    if re.fullmatch(r"CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9]", stem):
        value = "Datei-" + value
    return value


def remove_tree(path: Path) -> None:
    """Remove a known temporary tree, including Git read-only files on Windows."""
    if not path.exists():
        return
    def unlock_and_retry(function, item, _error):
        os.chmod(item, stat.S_IWRITE)
        function(item)
    delete_path: Path | str = path
    if os.name == "nt":
        # The extended-length namespace is required to remove remnants made by
        # older releases whose checked-out repository paths exceeded MAX_PATH.
        delete_path = "\\\\?\\" + str(path.resolve())
    last_error: OSError | None = None
    for attempt in range(5):
        try:
            shutil.rmtree(delete_path, onexc=unlock_and_retry)
            return
        except OSError as exc:
            last_error = exc
            # Virus scanners and Git may release their final Windows handle a
            # fraction later. Retry the exact verified temp tree only.
            time.sleep(0.2 * (attempt + 1))
    if path.exists() and last_error:
        raise last_error


def german_name(stem: str) -> str:
    tokens = [part for part in re.split(r"[^A-Za-z0-9]+", stem) if part]
    translated = [WORDS.get(part.lower(), part) for part in tokens]
    return clean(" ".join(translated) or "OpenSCAD Modell", 145)


def github_request(url: str, token: str = "") -> tuple[dict, dict[str, str]]:
    headers = {"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.loads(response.read()), dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        remaining = exc.headers.get("X-RateLimit-Remaining", "?")
        reset = exc.headers.get("X-RateLimit-Reset", "?")
        raise RuntimeError(f"GitHub HTTP {exc.code} (Restbudget {remaining}, Reset {reset})") from exc


class Store:
    def __init__(self, root: Path):
        self.root = root
        self.meta = root / ".scad-sammler"
        self.files = root / "Generatoren"
        self.meta.mkdir(parents=True, exist_ok=True)
        self.files.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.meta / "sammlung.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(repos)")}
        if "scad_count" not in columns:
            with self.db:
                self.db.execute("ALTER TABLE repos ADD COLUMN scad_count INTEGER")
        # Restore collection paths when its containing folder/drive was moved.
        known_root = self.db.execute("SELECT value FROM settings WHERE key='library_root'").fetchone()
        if not known_root or known_root[0] != str(root.resolve()):
            rebased = []
            for row in self.db.execute("SELECT sha256,local_path FROM assets"):
                old = Path(row["local_path"])
                if "Generatoren" in old.parts:
                    index = old.parts.index("Generatoren")
                    new = self.root.joinpath(*old.parts[index:])
                    if new != old and new.is_file():
                        rebased.append((str(new), row["sha256"]))
            if rebased:
                with self.db:
                    self.db.executemany("UPDATE assets SET local_path=? WHERE sha256=?", rebased)
                self.export_csv()
            self.set_setting("library_root", str(root.resolve()))

    def close(self) -> None:
        self.db.close()

    def set_setting(self, key: str, value: str) -> None:
        with self.db:
            self.db.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))

    def upsert_repos(self, items: list[dict]) -> int:
        with self.db:
            for item in items:
                self.db.execute(
                    """INSERT INTO repos(full_name,clone_url,html_url,description,default_branch,pushed_at,topics_json)
                       VALUES(?,?,?,?,?,?,?) ON CONFLICT(full_name) DO UPDATE SET
                       clone_url=excluded.clone_url, html_url=excluded.html_url,
                       description=excluded.description, default_branch=excluded.default_branch,
                       pushed_at=excluded.pushed_at, topics_json=excluded.topics_json""",
                    (item["full_name"], item["clone_url"], item["html_url"], item.get("description") or "",
                     item.get("default_branch") or "main", item.get("pushed_at") or "",
                     json.dumps(item.get("topics") or [])),
                )
        return len(items)

    def repo_rows(self, maximum: int = 0) -> list[sqlite3.Row]:
        sql = """SELECT * FROM repos ORDER BY
                 CASE WHEN scad_count IS NOT NULL AND scad_count <= 100 THEN 0
                      WHEN scad_count IS NULL THEN 1 ELSE 2 END,
                 CASE WHEN topics_json LIKE '%3d-printing%' THEN 0 ELSE 1 END,
                 COALESCE(scad_count, 2147483647) ASC,
                 pushed_at DESC"""
        if maximum:
            sql += f" LIMIT {int(maximum)}"
        return self.db.execute(sql).fetchall()

    def set_repo_scad_count(self, repo: str, count: int) -> None:
        with self.db:
            self.db.execute("UPDATE repos SET scad_count=? WHERE full_name=?", (count, repo))

    def repo_folder_path(self, repo: str) -> Path:
        row = self.db.execute("SELECT scad_count FROM repos WHERE full_name=?", (repo,)).fetchone()
        prefix = "XXL - " if row and row[0] is not None and int(row[0]) > 100 else ""
        return self.files / (prefix + clean(repo.replace("/", " - ")))

    def migrate_large_repo_folders(self, progress: Callable[[str], None] | None = None) -> int:
        """Prefix every known >100-SCAD repository folder with XXL."""
        changed = 0
        rows = self.db.execute("SELECT full_name FROM repos WHERE scad_count > 100 ORDER BY scad_count").fetchall()
        for row in rows:
            repo = row[0]
            new = self.files / ("XXL - " + clean(repo.replace("/", " - ")))
            candidates = [self.files / repo.replace("/", " - "),
                          self.files / clean(repo.replace("/", " - "))]
            old = next((candidate for candidate in candidates if candidate.exists()), None)
            if old is None or old == new:
                continue
            if new.exists():
                for item in old.iterdir():
                    target = new / item.name
                    if not target.exists():
                        shutil.move(str(item), str(target))
                try:
                    old.rmdir()
                except OSError:
                    continue
            else:
                os.replace(old, new)
            old_prefix = str(old) + os.sep
            asset_rows = self.db.execute("SELECT sha256,local_path FROM assets").fetchall()
            with self.db:
                for asset in asset_rows:
                    if asset["local_path"].startswith(old_prefix):
                        replacement = str(new / Path(asset["local_path"]).relative_to(old))
                        self.db.execute("UPDATE assets SET local_path=? WHERE sha256=?", (replacement, asset["sha256"]))
            changed += 1
            if progress:
                progress(f"XXL-Ordner markiert: {repo}")
        if changed:
            self.export_csv()
        return changed

    def asset_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM assets").fetchone()[0])

    def source_count(self) -> int:
        return int(self.db.execute("SELECT COUNT(*) FROM sources").fetchone()[0])

    def write_source_page(self, digest: str) -> None:
        asset = self.db.execute("SELECT * FROM assets WHERE sha256=?", (digest,)).fetchone()
        if not asset:
            return
        sources = self.db.execute("SELECT repo,source_path,source_url FROM sources WHERE sha256=? ORDER BY repo,source_path", (digest,)).fetchall()
        if not sources:
            return
        primary_url = sources[0]["source_url"]
        links = "\n".join(
            f'<li><a href="{html.escape(row["source_url"], quote=True)}">Repository: {html.escape(row["repo"])}</a>'
            f'<br><small>Ursprünglicher Dateipfad: {html.escape(row["source_path"])}</small></li>'
            for row in sources
        )
        target = Path(asset["local_path"]).with_suffix(".html")
        target.write_text(
            "<!doctype html><html lang='de'><head><meta charset='utf-8'>"
            f'<meta http-equiv="refresh" content="0; url={html.escape(primary_url, quote=True)}">'
            f"<title>Quelle – {html.escape(asset['german_name'])}</title>"
            f'<script>location.replace({json.dumps(primary_url)});</script></head><body>'
            f"<h1>{html.escape(asset['german_name'])}</h1>"
            f"<p>Weiterleitung zur Projektseite …</p><p>SHA-256: <code>{digest}</code></p>"
            f"<p>Falls die Weiterleitung blockiert wurde:</p><ul>{links}</ul></body></html>",
            encoding="utf-8",
        )

    def ingest(self, repo: str, branch: str, source_root: Path, source_file: Path, run_id: str,
               original_rel: str | None = None) -> str:
        rel = original_rel or source_file.relative_to(source_root).as_posix()
        digest = sha256_file(source_file)
        # The companion HTML must lead to the project page where README,
        # description, licensing, and author notes are available—not to the
        # raw/file view, which provides little context.
        source_url = f"https://github.com/{repo}"
        old = self.db.execute("SELECT sha256 FROM sources WHERE repo=? AND source_path=?", (repo, rel)).fetchone()
        asset = self.db.execute("SELECT local_path FROM assets WHERE sha256=?", (digest,)).fetchone()
        result = "duplikat" if asset else ("aktualisiert" if old else "neu")
        if not asset:
            repo_folder = self.repo_folder_path(repo)
            repo_folder.mkdir(parents=True, exist_ok=True)
            base = german_name(Path(rel).stem)
            target = repo_folder / f"{base}.scad"
            counter = 2
            while target.exists():
                if sha256_file(target) == digest:
                    break
                target = repo_folder / f"{base} ({counter}).scad"
                counter += 1
            temp = target.with_suffix(target.suffix + ".part")
            shutil.copy2(source_file, temp)
            os.replace(temp, target)
            with self.db:
                self.db.execute("INSERT INTO assets(sha256,local_path,size,german_name) VALUES(?,?,?,?)",
                                (digest, str(target), target.stat().st_size, target.stem))
        with self.db:
            self.db.execute(
                """INSERT INTO sources(repo,source_path,source_url,sha256,last_seen_run) VALUES(?,?,?,?,?)
                   ON CONFLICT(repo,source_path) DO UPDATE SET source_url=excluded.source_url,
                   sha256=excluded.sha256,last_seen_run=excluded.last_seen_run""",
                (repo, rel, source_url, digest, run_id),
            )
        self.write_source_page(digest)
        if old and old["sha256"] != digest:
            self.write_source_page(old["sha256"])
        return result

    def finish_repo(self, repo: str, commit: str, run_id: str) -> None:
        with self.db:
            # A successful full scan defines the current source set for this repository.
            stale = self.db.execute("SELECT DISTINCT sha256 FROM sources WHERE repo=? AND last_seen_run<>?", (repo, run_id)).fetchall()
            self.db.execute("DELETE FROM sources WHERE repo=? AND last_seen_run<>?", (repo, run_id))
            self.db.execute("UPDATE repos SET last_commit=?,last_scan=?,status='ok',error=NULL WHERE full_name=?", (commit, now_iso(), repo))
        for row in stale:
            self.write_source_page(row[0])

    def repo_error(self, repo: str, error: str) -> None:
        with self.db:
            self.db.execute("UPDATE repos SET status='fehler',error=? WHERE full_name=?", (error[:1000], repo))

    def prune_orphans(self) -> int:
        rows = self.db.execute("SELECT sha256,local_path FROM assets WHERE sha256 NOT IN (SELECT DISTINCT sha256 FROM sources)").fetchall()
        removed = 0
        with self.db:
            for row in rows:
                path = Path(row["local_path"])
                try:
                    path.unlink(missing_ok=True); path.with_suffix(".html").unlink(missing_ok=True)
                    self.db.execute("DELETE FROM assets WHERE sha256=?", (row["sha256"],))
                    removed += 1
                except OSError:
                    pass
        return removed

    def export_csv(self) -> None:
        import csv
        with (self.root / "Quellenverzeichnis.csv").open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.writer(handle, delimiter=";")
            writer.writerow(["Deutscher Dateiname", "SHA-256", "Repository", "Originalpfad", "Quellseite"])
            for row in self.db.execute(
                """SELECT a.local_path,a.sha256,s.repo,s.source_path,s.source_url
                   FROM assets a JOIN sources s ON s.sha256=a.sha256 ORDER BY a.local_path,s.repo"""
            ):
                writer.writerow([str(Path(row[0]).relative_to(self.root)), row[1], row[2], row[3], row[4]])

    def migrate_source_links_to_projects(self, progress: Callable[[str], None] | None = None) -> int:
        rows = self.db.execute(
            "SELECT DISTINCT sha256 FROM sources WHERE source_url LIKE 'https://github.com/%/blob/%'"
        ).fetchall()
        if not rows:
            return 0
        with self.db:
            self.db.execute(
                """UPDATE sources SET source_url='https://github.com/' || repo
                   WHERE source_url LIKE 'https://github.com/%/blob/%'"""
            )
        total = len(rows)
        for index, row in enumerate(rows, 1):
            self.write_source_page(row[0])
            if progress and (index == 1 or index % 1000 == 0 or index == total):
                progress(f"Quelllinks korrigiert: {index}/{total}")
        self.export_csv()
        return total


@dataclass
class Stats:
    new: int = 0
    updated: int = 0
    duplicates: int = 0
    errors: int = 0
    skipped: int = 0


class Collector:
    def __init__(self, root: Path, target: int, token: str, log: Callable[[str], None], stop: threading.Event, max_repos: int = 0,
                 activity: Callable[[dict], None] | None = None, repo_names: list[str] | None = None,
                 retries: queue.Queue | None = None, abort: threading.Event | None = None):
        self.root, self.target, self.token = root, target, token.strip()
        self.log, self.stop, self.max_repos = log, stop, max_repos
        self.work = Path(tempfile.gettempdir()) / "scad-generator-sammler"
        self.work.mkdir(parents=True, exist_ok=True)
        self.activity = activity or (lambda event: None)
        self.repo_names = repo_names
        self.retries = retries
        self.abort = abort or threading.Event()

    def git(self, args: list[str], timeout: int = 240, binary: bool = False) -> subprocess.CompletedProcess:
        """Bound network work and kill the whole Git process tree on timeout."""
        proc = subprocess.Popen(["git", *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                                env={**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"})
        stdout_parts: list[bytes] = []
        stderr_parts: list[bytes] = []
        def read_stdout():
            while block := proc.stdout.read(65536):
                stdout_parts.append(block)
        def read_stderr():
            line = bytearray(); last_report = 0.0
            while block := proc.stderr.read(1):
                stderr_parts.append(block)
                if block in (b"\r", b"\n"):
                    message = line.decode("utf-8", "replace").strip(); line.clear()
                    if message and time.monotonic() - last_report > 0.5:
                        self.activity({"git_activity": message[-180:]})
                        last_report = time.monotonic()
                elif len(line) < 1024:
                    line.extend(block)
        readers = [threading.Thread(target=read_stdout, daemon=True), threading.Thread(target=read_stderr, daemon=True)]
        for reader in readers:
            reader.start()
        started = time.monotonic()
        try:
            while proc.poll() is None:
                if self.abort.is_set() or time.monotonic() - started > timeout:
                    if os.name == "nt":
                        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True,
                                       creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
                    else:
                        proc.kill()
                    proc.wait(timeout=10)
                    if self.abort.is_set():
                        raise RuntimeError("Programm wird beendet; Git-Vorgang abgebrochen")
                    raise TimeoutError(f"Git-Zeitlimit nach {timeout} Sekunden erreicht; nächste Quelle wird verarbeitet")
                time.sleep(0.15)
        finally:
            for reader in readers:
                reader.join(timeout=3)
            proc.stdout.close(); proc.stderr.close()
        out, err = b"".join(stdout_parts), b"".join(stderr_parts)
        return subprocess.CompletedProcess(args, proc.returncode, out if binary else out.decode("utf-8", "replace"),
                                           err if binary else err.decode("utf-8", "replace"))

    def discover(self, store: Store) -> int:
        queries = ["topic:openscad topic:3d-printing", "topic:openscad", "openscad 3d printing in:name,description,readme"]
        all_items: dict[str, dict] = {
            name: {"full_name": name, "clone_url": f"https://github.com/{name}.git",
                   "html_url": f"https://github.com/{name}", "description": "Startquelle",
                   "default_branch": "main", "pushed_at": "", "topics": ["openscad", "3d-printing"]}
            for name in SEED_REPOS
        }
        store.upsert_repos(list(all_items.values()))
        for query in queries:
            for page in range(1, 11):
                if self.stop.is_set():
                    return len(all_items)
                url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
                    {"q": query, "sort": "updated", "order": "desc", "per_page": 100, "page": page}
                )
                try:
                    data, headers = github_request(url, self.token)
                except RuntimeError as exc:
                    self.log(f"Katalogsuche pausiert: {exc}. Vorhandener Katalog wird weiterbenutzt.")
                    return len(all_items)
                items = data.get("items", [])
                for item in items:
                    all_items[item["full_name"]] = item
                store.upsert_repos(items)
                remain = headers.get("X-RateLimit-Remaining", "?")
                self.log(f"Katalog: {len(all_items)} Repositories (GitHub-Restbudget {remain})")
                self.activity({"catalog_count": len(all_items), "catalog_running": True})
                if len(items) < 100:
                    break
                time.sleep(1.5)
        if all_items:
            store.upsert_repos(list(all_items.values()))
        return len(all_items)

    def remote_commit(self, clone_url: str) -> str:
        proc = self.git(["ls-remote", clone_url, "HEAD"], timeout=45)
        if proc.returncode:
            raise RuntimeError(proc.stderr.strip()[-400:] or "git ls-remote fehlgeschlagen")
        return proc.stdout.split()[0]

    def clone(self, repo: str, clone_url: str) -> tuple[Path, str, list[tuple[Path, str]]]:
        # A unique directory per attempt prevents interrupted/locked earlier
        # clones from poisoning later runs with "already exists and is not empty".
        # Keep the checkout root extremely short; some public repositories
        # contain deeply nested files close to Windows' path-length boundary.
        target = Path(tempfile.mkdtemp(prefix="r-", dir=self.work))
        try:
            # Never check a foreign Git tree out on Windows. Git permits names
            # such as "Misc.:" and arbitrary Unicode which NTFS/our collection
            # deliberately does not. We read tree objects and materialise only
            # SCAD blobs under short numeric, ASCII-only temporary names.
            self.activity({"phase": "download", "current_repo": repo, "current_file": "Git-Objekte herunterladen",
                           "file_done": 0, "file_total": None})
            proc = self.git(["-c", "core.longpaths=true", "-c", "http.lowSpeedLimit=1",
                                   "-c", "http.lowSpeedTime=60", "clone", "--depth", "1",
                                   "--no-checkout", "--progress", clone_url, str(target)], timeout=240)
            if proc.returncode:
                raise RuntimeError(proc.stderr.strip()[-500:] or "git clone fehlgeschlagen")
            branch_proc = self.git(["-C", str(target), "branch", "--show-current"], timeout=15)
            tree = self.git(["-C", str(target), "ls-tree", "-r", "-z", "HEAD"], timeout=120, binary=True)
            if tree.returncode:
                raise RuntimeError(tree.stderr.decode("utf-8", "replace")[-500:] or "Git-Baum konnte nicht gelesen werden")
            blobs: list[tuple[str, str]] = []
            for entry in tree.stdout.split(b"\0"):
                if not entry or b"\t" not in entry:
                    continue
                metadata, raw_path = entry.split(b"\t", 1)
                fields = metadata.split()
                if len(fields) >= 3 and fields[1] == b"blob" and raw_path.lower().endswith(b".scad"):
                    blobs.append((fields[2].decode("ascii"), raw_path.decode("utf-8", "replace")))

            materialized: list[tuple[Path, str]] = []
            self.activity({"phase": "extract", "current_repo": repo, "file_total": len(blobs), "file_done": 0})
            blob_dir = target / "_scad"
            blob_dir.mkdir()
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            batch = subprocess.Popen(["git", "-C", str(target), "cat-file", "--batch"],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     creationflags=flags)
            assert batch.stdin is not None and batch.stdout is not None
            batch_error = ""
            try:
                for number, (object_id, original_path) in enumerate(blobs, 1):
                    if self.abort.is_set():
                        raise RuntimeError("Programm wird beendet; Extraktion abgebrochen")
                    batch.stdin.write((object_id + "\n").encode("ascii")); batch.stdin.flush()
                    header = batch.stdout.readline().decode("ascii", "replace").strip().split()
                    if len(header) < 3 or header[1] != "blob":
                        raise RuntimeError(f"Git-BLOB nicht lesbar: {object_id}")
                    size = int(header[2])
                    data = batch.stdout.read(size)
                    if len(data) != size:
                        raise RuntimeError(f"Git-BLOB unvollständig: {object_id}")
                    batch.stdout.read(1)  # protocol newline
                    local = blob_dir / f"{number:08d}.scad"
                    local.write_bytes(data)
                    materialized.append((local, original_path))
                    if number == 1 or number % 20 == 0 or number == len(blobs):
                        self.activity({"phase": "extract", "current_repo": repo, "current_file": original_path,
                                       "file_done": number, "file_total": len(blobs)})
            finally:
                if self.abort.is_set():
                    batch.kill()
                if batch.stdin:
                    batch.stdin.close()
                try:
                    batch.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    batch.kill(); batch.wait()
                if batch.stderr:
                    batch_error = batch.stderr.read().decode("utf-8", "replace")[-500:]
                    batch.stderr.close()
                batch.stdout.close()
            if batch.returncode:
                raise RuntimeError(batch_error or "Git-BLOB-Extraktion fehlgeschlagen")
            return target, (branch_proc.stdout.strip() or "HEAD"), materialized
        except BaseException:
            try:
                remove_tree(target)
            except OSError:
                pass
            raise

    def cleanup_stale_work(self) -> int:
        """Remove only children of this application's dedicated temp folder."""
        expected = (Path(tempfile.gettempdir()) / "scad-generator-sammler").resolve()
        if self.work.resolve() != expected:
            raise RuntimeError(f"Unsicherer Temp-Pfad abgelehnt: {self.work}")
        removed = 0
        for child in self.work.iterdir():
            try:
                if child.is_dir():
                    remove_tree(child)
                else:
                    child.unlink()
                removed += 1
            except OSError:
                # A genuinely active child may still own a file; never broaden deletion.
                continue
        return removed

    def run(self, refresh_catalog: bool = True, create_previews: bool = True) -> Stats:
        stats = Stats(); store = Store(self.root); run_id = now_iso() + "-" + str(os.getpid())
        catalog_thread = None
        try:
            if shutil.which("git") is None:
                raise RuntimeError("Git wurde nicht gefunden. Bitte Git for Windows installieren.")
            stale = self.cleanup_stale_work()
            if stale:
                self.log(f"Temp-Bereinigung: {stale} alte Arbeitsordner entfernt.")
            corrected = store.migrate_source_links_to_projects(self.log)
            if corrected:
                self.log(f"{corrected} bestehende HTML-Quelllinks auf Repository-Seiten umgestellt.")
            xxl_changed = store.migrate_large_repo_folders(self.log)
            if xxl_changed:
                self.log(f"{xxl_changed} bestehende Großrepository-Ordner mit XXL markiert.")
            if refresh_catalog:
                self.activity({"catalog_running": True})
                def refresh_in_parallel() -> None:
                    refresh_store = Store(self.root)
                    try:
                        self.log("Neue Quellen werden parallel gesucht …")
                        self.discover(refresh_store)
                        self.log("Parallele Quellensuche abgeschlossen.")
                    except Exception as exc:
                        self.log(f"Quellensuche beendet: {exc}")
                    finally:
                        self.activity({"catalog_running": False, "catalog_finished": True})
                        refresh_store.close()
                catalog_thread = threading.Thread(target=refresh_in_parallel, daemon=True)
                catalog_thread.start()
            elif not store.repo_rows(1):
                self.log("Noch kein lokaler Katalog vorhanden; Startquellen werden einmalig geladen.")
                self.discover(store)
            rows = list(store.repo_rows(self.max_repos))
            if self.repo_names is not None:
                rows = [row for row in rows if row["full_name"] in self.repo_names]
            self.log(f"Synchronisation startet mit {len(rows)} bekannten Repositories; Ziel {self.target} eindeutige Dateien.")
            processed_repos: set[str] = set()
            deferred_large: list[tuple[sqlite3.Row, int]] = []
            allowed_large: set[str] = set()
            index = 0
            while True:
                if self.retries is not None:
                    while True:
                        try:
                            retry_repo = self.retries.get_nowait()
                        except queue.Empty:
                            break
                        retry_row = store.db.execute("SELECT * FROM repos WHERE full_name=?", (retry_repo,)).fetchone()
                        if retry_row:
                            rows.append(retry_row)
                if self.abort.is_set():
                    break
                if index >= len(rows):
                    if catalog_thread is not None:
                        catalog_thread.join()
                        catalog_thread = None
                        additions = [row for row in store.repo_rows(self.max_repos)
                                     if row["full_name"] not in processed_repos]
                        rows.extend(additions)
                        if additions:
                            self.log(f"{len(additions)} neu entdeckte Quellen werden jetzt ergänzt.")
                            continue
                    if deferred_large:
                        deferred_large.sort(key=lambda item: item[1])
                        allowed_large.update(row["full_name"] for row, _count in deferred_large)
                        rows.extend(row for row, _count in deferred_large)
                        self.log(f"Großrepository-Phase startet: {len(deferred_large)} Quellen mit jeweils mehr als 100 SCAD-Dateien.")
                        deferred_large.clear()
                        continue
                    break
                row = rows[index]
                index += 1
                if self.stop.is_set() or store.asset_count() >= self.target:
                    break
                repo = row["full_name"]
                processed_repos.add(repo)
                self.activity({"phase": "check", "current_repo": repo, "current_file": "Quellstand prüfen",
                               "source_index": index, "source_total": len(rows), "file_done": 0, "file_total": None})
                checkout = None
                try:
                    commit = self.remote_commit(row["clone_url"])
                    if row["last_commit"] == commit:
                        stats.skipped += 1
                        if index % 25 == 0:
                            self.log(f"Geprüft {index}/{len(rows)} – unverändert; {store.asset_count()} Dateien")
                        self.activity({"repo_finished": repo, "repo_status": "unverändert", "sources_processed": index})
                        continue
                    self.log(f"[{index}/{len(rows)}] Lade {repo} …")
                    checkout, branch, files = self.clone(repo, row["clone_url"])
                    store.set_repo_scad_count(repo, len(files))
                    if len(files) > 100 and repo not in allowed_large:
                        deferred_large.append((row, len(files)))
                        self.log(f"Zurückgestellt: {repo} enthält {len(files)} SCAD-Dateien und wird erst in der Großrepository-Phase geladen.")
                        self.activity({"repo_finished": repo, "repo_status": "XXL · wartet", "sources_processed": index})
                        continue
                    ingested = 0
                    for number, (file, original_path) in enumerate(files, 1):
                        if self.abort.is_set() or store.asset_count() >= self.target:
                            break
                        result = store.ingest(repo, branch, checkout, file, run_id, original_rel=original_path)
                        if result == "neu": stats.new += 1
                        elif result == "aktualisiert": stats.updated += 1
                        else: stats.duplicates += 1
                        ingested += 1
                        if number == 1 or number % 10 == 0 or number == len(files):
                            self.activity({"phase": "ingest", "current_repo": repo, "current_file": original_path,
                                           "file_done": number, "file_total": len(files), "new": stats.new,
                                           "duplicates": stats.duplicates, "updated": stats.updated})
                    # Only mark commit complete if this repo was scanned without target/stop interruption.
                    if not self.abort.is_set() and ingested == len(files):
                        store.finish_repo(repo, commit, run_id)
                    self.activity({"repo_finished": repo, "repo_status": "fertig" if ingested == len(files) else "teilweise",
                                   "sources_processed": index})
                    self.log(f"{repo}: {len(files)} SCAD gefunden – Sammlung: {store.asset_count()} eindeutig")
                    if create_previews and files:
                        preview_status = ensure_folder_preview(store.repo_folder_path(repo))
                        if preview_status == "created":
                            self.log(f"Vorschau erstellt: {repo}")
                except Exception as exc:
                    stats.errors += 1; store.repo_error(repo, str(exc)); self.log(f"FEHLER {repo}: {exc}")
                    self.activity({"repo_finished": repo, "repo_status": "fehler", "errors": stats.errors,
                                   "sources_processed": index})
                finally:
                    if checkout is not None:
                        try:
                            remove_tree(checkout)
                        except OSError as cleanup_error:
                            self.log(f"TEMP-FEHLER {repo}: {cleanup_error}")
                time.sleep(0.15)
            removed = store.prune_orphans()
            store.export_csv()
            (self.root / "README.txt").write_text(
                f"{APP_NAME} {VERSION}\n\nEindeutige SCAD-Dateien: {store.asset_count()}\n"
                f"Erfasste Quellvorkommen: {store.source_count()}\n"
                "Jede SCAD-Datei hat eine gleichnamige HTML-Datei mit ihren Quellseiten.\n"
                "Dubletten werden per SHA-256 erkannt. Zustand und Updates stehen in .scad-sammler.\n"
                "Die Dateinamen werden regelbasiert ins Deutsche übertragen. Originalpfade stehen im Quellenverzeichnis.\n"
                "Bitte Lizenzbedingungen des jeweiligen Quellprojekts vor Weitergabe oder kommerzieller Nutzung prüfen.\n",
                encoding="utf-8",
            )
            self.log(f"Fertig: {store.asset_count()} eindeutige Dateien, {stats.duplicates} Dubletten, {stats.updated} Updates, {stats.errors} Fehler, {removed} veraltete Dateien entfernt.")
            return stats
        finally:
            if catalog_thread is not None:
                catalog_thread.join()
            store.close()


def _pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        import ctypes
        handle = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)
        if handle:
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def launch_preview_worker(root: Path) -> tuple[bool, str]:
    state = root / ".scad-sammler"
    state.mkdir(parents=True, exist_ok=True)
    pid_file = state / "preview-worker.pid"
    try:
        pid = int(pid_file.read_text(encoding="ascii").strip())
        if _pid_running(pid):
            return False, f"Vorschau-Dienst läuft bereits (PID {pid})."
    except (OSError, ValueError):
        pass
    if getattr(sys, "frozen", False):
        command = [sys.executable, "--preview-worker", "--output", str(root)]
    else:
        command = [sys.executable, str(Path(__file__).resolve()), "--preview-worker", "--output", str(root)]
    flags = 0
    if os.name == "nt":
        flags = subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, close_fds=True, creationflags=flags)
    pid_file.write_text(str(process.pid), encoding="ascii")
    return True, f"Unabhängiger Vorschau-Dienst gestartet (PID {process.pid})."


def run_preview_worker(root: Path) -> int:
    state = root / ".scad-sammler"
    state.mkdir(parents=True, exist_ok=True)
    pid_file = state / "preview-worker.pid"
    log_file = state / "vorschauen.log"
    status_file = state / "vorschauen-status.json"
    pid_file.write_text(str(os.getpid()), encoding="ascii")

    def log(message: str) -> None:
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {message}\n"
        with log_file.open("a", encoding="utf-8") as handle:
            handle.write(line)

    try:
        library = root / "Generatoren"
        log("Vorschau-Dienst gestartet.")
        result = backfill_previews(library, log, threading.Event())
        seen = {item.parent for item in library.rglob("*.scad")}
        status_file.write_text(json.dumps({"status": "beobachtet", "pid": os.getpid(), "updated": now_iso(), **result}, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"Erster Durchlauf fertig: {result}")
        while True:
            time.sleep(30)
            current = {item.parent for item in library.rglob("*.scad")}
            new_folders = sorted(current - seen)
            for folder in new_folders:
                try:
                    outcome = ensure_folder_preview(folder)
                    log(f"Neu: {folder.name}: {outcome}")
                except Exception as exc:
                    log(f"FEHLER {folder}: {exc}")
            seen |= current
            status_file.write_text(json.dumps({"status": "beobachtet", "pid": os.getpid(), "updated": now_iso(),
                                               "bekannte_ordner": len(seen)}, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as exc:
        log(f"ABBRUCH: {exc}")
        status_file.write_text(json.dumps({"status": "fehler", "pid": os.getpid(), "updated": now_iso(), "error": str(exc)}, ensure_ascii=False, indent=2), encoding="utf-8")
        return 1
    finally:
        try:
            if pid_file.read_text(encoding="ascii").strip() == str(os.getpid()):
                pid_file.unlink(missing_ok=True)
        except OSError:
            pass


def run_gui(autostart: bool = False) -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk(); root.title(f"{APP_NAME} {VERSION}"); root.geometry("920x650"); root.minsize(760, 520)
    style = ttk.Style(root)
    try: style.theme_use("vista")
    except tk.TclError: pass
    settings = load_app_settings()
    path_var = tk.StringVar(value=str(settings["library"])); target_var = tk.StringVar(value=str(settings["target"]))
    saved_token = load_saved_token()
    token_var = tk.StringVar(value=os.environ.get("GITHUB_TOKEN", "") or saved_token)
    save_token_var = tk.BooleanVar(value=True); previews_var = tk.BooleanVar(value=bool(settings["create_previews"]))
    status_var = tk.StringVar(value="Bereit"); messages: queue.Queue[str] = queue.Queue(); stop_event = threading.Event()
    worker: list[threading.Thread | None] = [None]
    run_baseline: list[int | None] = [None]
    start_with_refresh = [False]
    last_activity = [time.monotonic()]
    last_action = ["Kein Lauf aktiv"]

    outer = ttk.Frame(root, padding=14); outer.pack(fill="both", expand=True)
    header = ttk.Frame(outer); header.pack(fill="x")
    heading = ttk.Frame(header); heading.pack(side="left", fill="x", expand=True)
    ttk.Label(heading, text="SCAD Generator Sammlung", font=("Segoe UI", 18, "bold")).pack(anchor="w")
    ttk.Label(heading, text="Automatische, deduplizierte OpenSCAD-Sammlung mit Quellen- und Vorschauverwaltung.").pack(anchor="w", pady=(2, 12))
    live = ttk.Frame(header); live.pack(side="right", anchor="ne", padx=(14, 0))
    live_var = tk.StringVar(value="● BEREIT")
    live_detail_var = tk.StringVar(value="Kein Lauf aktiv")
    live_label = tk.Label(live, textvariable=live_var, font=("Segoe UI", 11, "bold"), fg="#666666")
    live_label.pack(anchor="e")
    ttk.Label(live, textvariable=live_detail_var, anchor="e").pack(anchor="e")

    stats_frame = ttk.Frame(outer)
    stats_frame.pack(fill="x", pady=(0, 12))
    stat_vars = {name: tk.StringVar(value="0") for name in
                 ("Gesamt", "Neu in diesem Lauf", "Quellen erledigt", "Noch abzuarbeiten", "Dubletten", "Fehler", "Vorschauen")}
    for column, (name, variable) in enumerate(stat_vars.items()):
        card = ttk.LabelFrame(stats_frame, text=name, padding=(10, 7))
        card.grid(row=0, column=column, sticky="nsew", padx=(0 if column == 0 else 4, 0))
        ttk.Label(card, textvariable=variable, font=("Segoe UI", 15, "bold")).pack()
        stats_frame.columnconfigure(column, weight=1)

    buttons = ttk.Frame(outer); buttons.pack(fill="x", pady=10)
    start_button = ttk.Button(buttons, text="Mit Aktualisierung starten")
    start_button.pack(side="left")
    known_button = ttk.Button(buttons, text="Bekannten Rest laden")
    known_button.pack(side="left", padx=(8, 0))
    stop_button = ttk.Button(buttons, text="Nach aktueller Quelle stoppen", state="disabled")
    stop_button.pack(side="left", padx=8)
    preview_button = ttk.Button(buttons, text="Vorschauen nachholen")
    preview_button.pack(side="left")
    settings_button = ttk.Button(buttons, text="Einstellungen")
    settings_button.pack(side="right", padx=(8, 0))
    ttk.Button(buttons, text="Ordner öffnen", command=lambda: os.startfile(Path(path_var.get()))).pack(side="right")
    progress = ttk.Progressbar(outer, mode="determinate"); progress.pack(fill="x")
    ttk.Label(outer, textvariable=status_var).pack(anchor="w", pady=(5, 8))
    logbox = tk.Text(outer, wrap="word", height=20, font=("Consolas", 9), state="disabled")
    scrollbar = ttk.Scrollbar(outer, orient="vertical", command=logbox.yview); logbox.configure(yscrollcommand=scrollbar.set)
    logbox.pack(side="left", fill="both", expand=True); scrollbar.pack(side="right", fill="y")

    def append(message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        last_activity[0] = time.monotonic()
        last_action[0] = message
        logbox.configure(state="normal"); logbox.insert("end", f"[{stamp}] {message}\n"); logbox.see("end"); logbox.configure(state="disabled")

    def compact_activity(message: str) -> str:
        source = re.search(r"\[(\d+)/(\d+)\]", message)
        if source:
            return f"Quelle {source.group(1)}/{source.group(2)}"
        catalog = re.search(r"Katalog:\s*(\d+)", message)
        if catalog:
            return f"Katalogsuche: {catalog.group(1)}"
        if "Großrepository-Phase" in message:
            return "XXL-Repositories"
        if "Quellensuche" in message:
            return "Quellensuche"
        if "Temp-Bereinigung" in message:
            return "Bereinigung"
        if message.startswith("Fertig"):
            return "Abgeschlossen"
        return "Synchronisierung"

    def read_stats() -> None:
        database = Path(path_var.get()) / ".scad-sammler" / "sammlung.sqlite3"
        total = sources = repos = done = errors = 0
        if database.is_file():
            try:
                connection = sqlite3.connect(database, timeout=0.25)
                total = int(connection.execute("SELECT COUNT(*) FROM assets").fetchone()[0])
                sources = int(connection.execute("SELECT COUNT(*) FROM sources").fetchone()[0])
                repos = int(connection.execute("SELECT COUNT(*) FROM repos").fetchone()[0])
                done = int(connection.execute("SELECT COUNT(*) FROM repos WHERE status='ok'").fetchone()[0])
                errors = int(connection.execute("SELECT COUNT(*) FROM repos WHERE status='fehler'").fetchone()[0])
                connection.close()
            except sqlite3.Error:
                pass
        stat_vars["Gesamt"].set(f"{total:,}".replace(",", "."))
        new_this_run = 0 if run_baseline[0] is None else max(0, total - run_baseline[0])
        stat_vars["Neu in diesem Lauf"].set(f"{new_this_run:,}".replace(",", "."))
        stat_vars["Quellen erledigt"].set(f"{done:,}".replace(",", "."))
        stat_vars["Noch abzuarbeiten"].set(f"{max(0, repos-done-errors):,}".replace(",", "."))
        stat_vars["Dubletten"].set(f"{max(0, sources-total):,}".replace(",", "."))
        stat_vars["Fehler"].set(f"{errors:,}".replace(",", "."))
        generator_root = Path(path_var.get()) / "Generatoren"
        preview_count = len(list(generator_root.rglob("Folder.jpg"))) if generator_root.exists() else 0
        folder_count = len([item for item in generator_root.iterdir() if item.is_dir()]) if generator_root.exists() else 0
        stat_vars["Vorschauen"].set(f"{preview_count:,}/{folder_count:,}".replace(",", "."))
        try:
            target = max(1, int(target_var.get()))
            progress.configure(maximum=target, value=min(total, target))
        except ValueError:
            pass

    def poll() -> None:
        try:
            while True:
                message = messages.get_nowait(); append(message)
                match = re.search(r"Sammlung: (\d+) eindeutig", message)
                if match:
                    progress.configure(value=min(int(match.group(1)), int(progress.cget("maximum"))))
        except queue.Empty: pass
        if worker[0] and worker[0].is_alive():
            idle_seconds = int(time.monotonic() - last_activity[0])
            phase = (int(time.monotonic() * 2) % 3) + 1
            live_var.set("● AKTIV " + ("•" * phase))
            live_label.configure(fg="#07852f" if idle_seconds < 60 else "#b06a00")
            live_detail_var.set(f"{compact_activity(last_action[0])} · vor {idle_seconds} s")
        elif worker[0] and not worker[0].is_alive():
            worker[0] = None; start_button.configure(state="normal"); known_button.configure(state="normal"); preview_button.configure(state="normal"); settings_button.configure(state="normal"); stop_button.configure(state="disabled"); status_var.set("Fertig")
            live_var.set("● FERTIG"); live_label.configure(fg="#555555"); live_detail_var.set("Abgeschlossen")
        read_stats()
        root.after(1000, poll)

    def work() -> None:
        try:
            if previews_var.get():
                started, detail = launch_preview_worker(Path(path_var.get()))
                messages.put(detail)
            Collector(Path(path_var.get()), int(target_var.get()), token_var.get(), messages.put, stop_event).run(start_with_refresh[0], False)
        except Exception as exc:
            messages.put(f"ABBRUCH: {exc}")

    def start(refresh_catalog: bool) -> None:
        try:
            target = int(target_var.get())
            if target < 1: raise ValueError
        except ValueError:
            messagebox.showerror(APP_NAME, "Bitte unter Einstellungen eine positive Zielzahl eingeben."); return
        save_app_settings({"library": path_var.get(), "target": target,
                           "refresh_catalog": refresh_catalog, "create_previews": previews_var.get()})
        try:
            if save_token_var.get():
                save_token(token_var.get())
            else:
                delete_saved_token()
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"Token konnte nicht sicher gespeichert werden:\n{exc}"); return
        stop_event.clear(); Path(path_var.get()).mkdir(parents=True, exist_ok=True)
        read_stats(); run_baseline[0] = int(stat_vars["Gesamt"].get().replace(".", ""))
        start_with_refresh[0] = refresh_catalog
        progress.configure(maximum=target)
        start_button.configure(state="disabled"); known_button.configure(state="disabled"); settings_button.configure(state="disabled"); stop_button.configure(state="normal")
        mode = "neue Quellen + bekannter Rest" if refresh_catalog else "nur bekannter Rest"
        status_var.set((f"Synchronisierung läuft ({mode}) – Ziel: {target:,}").replace(",", "."))
        last_activity[0] = time.monotonic(); last_action[0] = "Start wird vorbereitet"
        live_var.set("● AKTIV"); live_label.configure(fg="#07852f"); live_detail_var.set("Start · vor 0 s")
        worker[0] = threading.Thread(target=work, daemon=True); worker[0].start()

    def start_previews() -> None:
        started, detail = launch_preview_worker(Path(path_var.get()))
        append(detail)
        status_var.set("Unabhängiger Vorschau-Dienst läuft" if started else detail)

    def forget_token() -> None:
        delete_saved_token(); token_var.set("")

    def open_settings() -> None:
        dialog = tk.Toplevel(root); dialog.title("Einstellungen"); dialog.geometry("760x360"); dialog.transient(root); dialog.grab_set()
        form = ttk.Frame(dialog, padding=16); form.pack(fill="both", expand=True)
        ttk.Label(form, text="Sammlungsordner:").grid(row=0, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=path_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Button(form, text="Auswählen…", command=lambda: path_var.set(filedialog.askdirectory(parent=dialog, initialdir=path_var.get()) or path_var.get())).grid(row=0, column=2)
        ttk.Label(form, text="Zielzahl eindeutiger Dateien:").grid(row=1, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=target_var, width=15).grid(row=1, column=1, sticky="w", padx=8)
        ttk.Label(form, text="GitHub-Token:").grid(row=2, column=0, sticky="w", pady=6)
        ttk.Entry(form, textvariable=token_var, show="•").grid(row=2, column=1, sticky="ew", padx=8)
        ttk.Label(form, text="Windows-verschlüsselt gespeichert" if load_saved_token() else "Noch nicht gespeichert").grid(row=2, column=2, sticky="w")
        ttk.Checkbutton(form, text="Token dauerhaft speichern", variable=save_token_var).grid(row=3, column=1, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(form, text="Vorschauen und Windows-Ordnersymbole automatisch erstellen", variable=previews_var).grid(row=4, column=1, columnspan=2, sticky="w", padx=8, pady=4)
        form.columnconfigure(1, weight=1)
        actions = ttk.Frame(form); actions.grid(row=5, column=0, columnspan=3, sticky="ew", pady=(18, 0))
        ttk.Button(actions, text="Token löschen", command=forget_token).pack(side="left")

        def commit_settings() -> None:
            try:
                target = int(target_var.get())
                if target < 1: raise ValueError
            except ValueError:
                messagebox.showerror(APP_NAME, "Bitte eine positive Zielzahl eingeben.", parent=dialog); return
            try:
                if save_token_var.get(): save_token(token_var.get())
                else: delete_saved_token()
                save_app_settings({"library": path_var.get(), "target": target,
                                   "refresh_catalog": False, "create_previews": previews_var.get()})
            except Exception as exc:
                messagebox.showerror(APP_NAME, f"Einstellungen konnten nicht gespeichert werden:\n{exc}", parent=dialog); return
            read_stats(); status_var.set(f"Einstellungen gespeichert – Ziel: {target:,}".replace(",", ".")); dialog.destroy()

        ttk.Button(actions, text="Speichern", command=commit_settings).pack(side="right")
        ttk.Button(actions, text="Abbrechen", command=dialog.destroy).pack(side="right", padx=8)

    preview_button.configure(command=start_previews)
    settings_button.configure(command=open_settings)
    start_button.configure(command=lambda: start(True)); known_button.configure(command=lambda: start(False))
    stop_button.configure(command=lambda: (stop_event.set(), status_var.set("Stop wird vorbereitet …")))
    root.protocol("WM_DELETE_WINDOW", lambda: (stop_event.set(), root.destroy()))
    poll()
    if autostart:
        root.after(750, lambda: start(True))
    root.mainloop()


def main() -> int:
    parser = argparse.ArgumentParser(description=APP_NAME)
    parser.add_argument("--headless", action="store_true"); parser.add_argument("--output", type=Path, default=DEFAULT_LIBRARY)
    parser.add_argument("--target", type=int, default=5000); parser.add_argument("--max-repos", type=int, default=0)
    parser.add_argument("--token", default=os.environ.get("GITHUB_TOKEN", "")); parser.add_argument("--no-refresh", action="store_true")
    parser.add_argument("--preview-worker", action="store_true")
    parser.add_argument("--autostart", action="store_true")
    args = parser.parse_args()
    if args.preview_worker:
        return run_preview_worker(args.output)
    if not args.headless:
        from scad_desktop import run_desktop
        run_desktop(sys.modules[__name__], args.autostart)
        return 0
    Collector(args.output, args.target, args.token, print, threading.Event(), args.max_repos).run(not args.no_refresh)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
