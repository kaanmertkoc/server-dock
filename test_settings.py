"""Regressions for the rename, saved titles, and existing address books."""

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from textual.widgets import Static

import server_dock as dock


class SettingsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.settings = self.root / "settings.json"

    def test_default_and_saved_name(self):
        self.assertEqual(dock.load_settings(self.settings), {"name": "Server Dock"})
        with patch.object(dock.Dock, "run"):
            self.assertEqual(dock.main(["--data", str(self.root / "servers.json"),
                                       "--themes", str(dock.THEMES_PATH),
                                       "--settings", str(self.settings), "--name", "Kaan's Dock"]), 0)
        self.assertEqual(dock.load_settings(self.settings), {"name": "Kaan's Dock"})
        self.assertEqual(self.settings.stat().st_mode & 0o777, 0o600)

    def test_invalid_settings_and_names_do_not_overwrite(self):
        for text in ['{broken', '{"name":"one","name":"two"}', '{"name":""}',
                     '{"name":"okay","unknown":true}']:
            self.settings.write_text(text)
            with self.subTest(text=text), self.assertRaises(ValueError):
                dock.load_settings(self.settings)
            self.assertEqual(self.settings.read_text(), text)
        self.settings.write_text('{"name":"My Dock"}')
        with patch.object(dock.Dock, "run") as run, patch("sys.stderr"):
            self.assertEqual(dock.main(["--settings", str(self.settings), "--name", "\n"]), 1)
            run.assert_not_called()
        self.assertEqual(json.loads(self.settings.read_text()), {"name": "My Dock"})

    def test_broken_settings_symlink_is_not_overwritten(self):
        self.settings.symlink_to(self.root / "missing")
        with self.assertRaises(ValueError):
            dock.load_settings(self.settings)
        self.assertTrue(self.settings.is_symlink())

    def test_legacy_paths_and_new_paths_take_precedence(self):
        legacy = self.root / "kaans-dock"
        current = self.root / "server-dock"
        legacy.mkdir()
        current.mkdir()
        with patch.object(dock, "LEGACY_DIR", legacy), \
                patch.object(dock, "DATA_PATH", current / "servers.json"), \
                patch.object(dock, "USER_THEMES", current / "themes.json"):
            self.assertEqual(dock.default_paths(), (current / "servers.json", current / "themes.json"))
            for filename in ["servers.json", "themes.json"]:
                (legacy / filename).write_text("legacy")
            self.assertEqual(dock.default_paths(), (legacy / "servers.json", legacy / "themes.json"))
            for filename in ["servers.json", "themes.json"]:
                (current / filename).symlink_to(current / "missing")
            self.assertEqual(dock.default_paths(), (current / "servers.json", current / "themes.json"))
            self.assertEqual((legacy / "servers.json").read_text(), "legacy")

    def test_legacy_startup_preserves_personal_title(self):
        legacy = self.root / "kaans-dock"
        legacy.mkdir()
        data = legacy / "servers.json"
        dock.save_data(data, {"version": 1, "categories": [], "servers": []})
        with patch.object(dock, "LEGACY_DIR", legacy), \
                patch.object(dock, "default_paths", return_value=(data, dock.THEMES_PATH)), \
                patch.object(dock.Dock, "run") as run, \
                patch.object(dock, "Dock", wraps=dock.Dock) as app:
            self.assertEqual(dock.main(["--settings", str(self.settings)]), 0)
            self.assertEqual(app.call_args.kwargs["name"], "Kaan's Dock")
            run.assert_called_once()
        self.assertFalse(self.settings.exists())


class DisplayTests(unittest.IsolatedAsyncioTestCase):
    async def test_custom_name_is_rendered_as_literal_text(self):
        with tempfile.TemporaryDirectory() as directory:
            app = dock.Dock(Path(directory) / "servers.json", name="[red]My Dock[/red]")
            async with app.run_test(size=(80, 24)):
                title = app.query_one("#title", Static)
                self.assertFalse(title._render_markup)
                self.assertEqual(str(title.render()), "[red]My Dock[/red]")


if __name__ == "__main__":
    unittest.main()
