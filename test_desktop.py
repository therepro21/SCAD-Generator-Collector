"""Integration checks on temporary collections. Never touches the real library."""
import hashlib
import os
import queue
import re
import sqlite3
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import scad_sammler as core
from scad_desktop import DesktopApi, NativeBridge, TrayController, render_html, PROJECT_URL


class DesktopIntegration(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="scad-ui-test-")
        self.base = Path(self.temp.name)
        self.library = self.base / "collection"
        self.program = self.base / "Programm"
        self.program.mkdir()
        self.patches = [patch.object(core, "PROGRAM_DIR", self.program),
                        patch.object(core, "DEFAULT_LIBRARY", self.library),
                        patch.object(core, "SETTINGS_FILE", self.program / "settings.json"),
                        patch.object(core, "TOKEN_FILE", self.program / "github-token.bin")]
        for p in self.patches:
            p.start()
        core.save_app_settings({"library": str(self.library), "target": 750000,
                                "refresh_catalog": True, "create_previews": False})
        store = core.Store(self.library)
        store.close()
        self.apis = []

    def tearDown(self):
        for api in self.apis:
            api.shutdown()
            api.refresh_thread.join(timeout=5)
        for p in reversed(self.patches):
            p.stop()
        core.remove_tree(self.base)
        self.temp.cleanup()

    def api(self):
        api = DesktopApi(core)
        self.apis.append(api)
        api._refresh_database()
        return api

    def git_repo(self, count=3):
        repo = self.base / ("git-" + str(count))
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        def git(*args, data=None):
            result = subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True,
                                    creationflags=flags, check=True)
            return result.stdout.strip()
        subprocess.run(["git", "init", "--bare", "--initial-branch=main", str(repo)], check=True,
                       capture_output=True, creationflags=flags)
        git("config", "user.name", "SCAD Test"); git("config", "user.email", "test@example.invalid")
        entries = []
        for i in range(count):
            blob = git("hash-object", "-w", "--stdin", data=f"cube({i+1});\n".encode())
            original = "设备:?.SCAD" if i == 0 else f"knob-{i}.scad"
            entries.append(b"100644 blob " + blob + b"\t" + original.encode() + b"\0")
        tree = git("mktree", "-z", data=b"".join(entries))
        commit = git("commit-tree", tree.decode(), data=b"Test\n")
        git("update-ref", "refs/heads/main", commit.decode())
        return repo

    def seed(self, name, url, count=None):
        store = core.Store(self.library)
        store.upsert_repos([{"full_name": name, "clone_url": str(url), "html_url": "https://github.com/" + name}])
        if count is not None:
            store.set_repo_scad_count(name, count)
        store.close()

    def test_real_git_import_windows_names_stop_after_source(self):
        repo = self.git_repo()
        self.seed("test/first", repo, 3)
        self.seed("test/second", repo, 4)
        stop = threading.Event(); events = []
        def activity(event):
            events.append(event)
            if event.get("phase") == "ingest":
                stop.set()
        collector = core.Collector(self.library, 750000, "", lambda _: None, stop, activity=activity)
        stats = collector.run(False, False)
        store = core.Store(self.library)
        self.assertEqual(stats.new, 3)
        self.assertEqual(store.asset_count(), 3)
        self.assertEqual(store.db.execute("SELECT status FROM repos WHERE full_name='test/first'").fetchone()[0], "ok")
        self.assertEqual(store.db.execute("SELECT status FROM repos WHERE full_name='test/second'").fetchone()[0], "neu")
        for row in store.db.execute("SELECT local_path FROM assets"):
            path = Path(row[0]); self.assertTrue(path.is_file()); self.assertTrue(path.name.isascii())
            self.assertNotRegex(path.name, r'[<>:"/\\|?*]')
            self.assertIn('content="0; url=https://github.com/test/first"', path.with_suffix('.html').read_text(encoding='utf-8'))
        store.close()
        self.assertTrue(any(e.get("current_file") == "设备:?.SCAD" for e in events))
        self.assertFalse(any(collector.work.iterdir()))

    def test_xxl_deferred_and_prefixed(self):
        repo = self.git_repo(101)
        self.seed("test/large", repo)
        messages = []
        collector = core.Collector(self.library, 750000, "", messages.append, threading.Event())
        stats = collector.run(False, False)
        self.assertEqual(stats.new, 101)
        self.assertTrue(any("Zurückgestellt" in message for message in messages))
        self.assertTrue((self.library / "Generatoren" / "XXL - test-large").is_dir())

    def test_start_modes_real_runner_and_single_retry(self):
        repo = self.git_repo()
        self.seed("test/only", repo, 3)
        api = self.api()
        with patch.object(core.Collector, "discover", return_value=0) as discover:
            self.assertTrue(api.start(False)["ok"])
            api.worker.join(timeout=20)
            self.assertFalse(api.worker.is_alive())
            discover.assert_not_called()
            self.assertTrue(api.start(True)["ok"])
            api.worker.join(timeout=20)
            self.assertFalse(api.worker.is_alive())
            discover.assert_called_once()
        self.assertTrue(api.retry("test/only")["ok"])
        api.worker.join(timeout=20)
        snap = api.snapshot()
        self.assertFalse(snap["running"])
        self.assertEqual(snap["total"], 3)
        self.assertEqual(api.state["selected_repos"], ["test/only"])
        self.assertEqual(api.detail("test/only")["source_count"], 3)

    def test_snapshot_filters_settings_and_safe_bridge(self):
        self.seed("test/small", "https://github.com/test/small.git", 12)
        self.seed("test/large", "https://github.com/test/large.git", 120)
        store = core.Store(self.library); store.repo_error("test/small", "Test failure"); store.close()
        api = self.api()
        self.assertEqual(api.snapshot(filter_name="xxl")["filtered_total"], 1)
        self.assertEqual(api.snapshot(filter_name="errors")["error_count"], 1)
        self.assertEqual(api.snapshot(query="small")["filtered_total"], 1)
        self.assertFalse(api.save_settings({"target": 0, "library": str(self.library)})["ok"])
        settings = {"target": 750001, "library": str(self.library), "create_previews": False,
                    "token": "test-token-never-real"}
        self.assertTrue(api.save_settings(settings)["ok"])
        self.assertEqual(core.load_saved_token(), settings["token"])
        self.assertNotIn(settings["token"], str(api.get_settings()))
        self.assertEqual(core.load_app_settings()["target"], 750001)
        self.assertTrue(api.save_settings({**settings, "token": "", "delete_token": True})["ok"])
        self.assertFalse(core.TOKEN_FILE.exists())
        bridge = NativeBridge(api)
        self.assertTrue(all(callable(getattr(bridge, name)) for name in dir(bridge) if not name.startswith('_')))
        self.assertTrue(api.export()["ok"])

    def test_git_timeout_and_portable_move(self):
        repo = self.git_repo()
        self.seed("test/repo", repo, 3)
        collector = core.Collector(self.library, 750000, "", lambda _: None, threading.Event())
        with self.assertRaises(TimeoutError):
            collector.git(["-c", "alias.delay=!sleep 10", "delay"], timeout=1)
        collector.run(False, False)
        moved = self.base / "moved"
        self.library.rename(moved)
        store = core.Store(moved)
        self.assertEqual(store.asset_count(), 3)
        for row in store.db.execute("SELECT local_path FROM assets"):
            self.assertTrue(Path(row[0]).is_relative_to(moved)); self.assertTrue(Path(row[0]).is_file())
        store.close()

    def test_reference_css_and_svg_preserved(self):
        resources = Path(core.__file__).parent
        reference = (resources / "ui-template.html").read_text(encoding="utf-8")
        product = render_html(resources)
        self.assertEqual(re.search(r"<style>(.*?)</style>", reference, re.S).group(1),
                         re.search(r"<style>(.*?)</style>", product, re.S).group(1))
        self.assertEqual(re.search(r"<defs>(.*?)</defs>", reference, re.S).group(1),
                         re.search(r"<defs>(.*?)</defs>", product, re.S).group(1))
        self.assertNotIn('<div class="chooser">', product)
        self.assertNotIn("Interaktiver Entwurf", product)
        self.assertIn('data:image/png;base64,', product)
        self.assertIn('SCAD Generator<br>Collector', product)
        self.assertIn('Michael P. Thiess', product)

    def test_tray_restore_and_exit(self):
        from unittest.mock import Mock
        from PIL import Image
        logo = Path(core.__file__).parent / 'assets' / 'logo.png'
        with Image.open(logo) as image:
            self.assertEqual(image.mode, 'RGBA')
        window = Mock()
        with patch('pystray.Icon') as icon:
            tray = TrayController(window, logo)
            tray.minimized()
            window.hide.assert_called_once()
            tray.restore()
            window.show.assert_called_once()
            window.restore.assert_called_once()
            tray.quit()
            window.destroy.assert_called_once()
            tray.close()
            icon.return_value.stop.assert_called_once()
        with patch('scad_desktop.webbrowser.open') as browser:
            NativeBridge(Mock()).open_project()
            browser.assert_called_once_with(PROJECT_URL)


if __name__ == '__main__':
    unittest.main(verbosity=2)
