"""Run with: python -m unittest -v (no extra test dependencies)."""

import copy
from contextlib import nullcontext
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from textual.command import CommandPalette
from textual.widgets import Button, Input, Label, OptionList, Select, Static, Tree

from server_dock import (Dialog, Dock, clean_data, clean_port, clean_server, host_type,
                  init_themes, load_data, load_themes, save_data, save_theme, ssh_args)


def server(**changes):
    return {"id": "one", "name": "Home NAS", "host": "home-nas",
            "username": "", "port": None, "category": None, **changes}


def data():
    return {"version": 1, "categories": ["Home Lab", "Public"],
            "servers": [server(), server(id="two", name="Router", host="192.168.1.1",
                                         category="Home Lab")]}


class DataTests(unittest.TestCase):
    def test_hosts(self):
        for host, kind in [("192.168.1.20", "IPv4"), ("2001:db8::1", "IPv6"),
                           ("nas.local", "Hostname / SSH alias"),
                           ("my-vps", "Hostname / SSH alias"), ("lab_box", "Hostname / SSH alias")]:
            with self.subTest(host=host):
                self.assertEqual(host_type(host), kind)
        for host in ["", "999.1.1.1", "192.168.1", "2001:::1", "-oProxyCommand=bad",
                     "fe80::1%some interface", "fe80::1%$(whoami)",
                     "host name", "a;whoami", "$(whoami)", "user@host", "a..b", "a\n"]:
            with self.subTest(host=host), self.assertRaises(ValueError):
                host_type(host)

    def test_ports_and_record_validation(self):
        self.assertIsNone(clean_port(""))
        self.assertEqual(clean_port("22"), 22)
        self.assertEqual(clean_port(65535), 65535)
        for port in [0, 65536, -1, "abc", "22.5", True, [], "9" * 5000]:
            with self.subTest(port=str(port)[:30]), self.assertRaises(ValueError):
                clean_port(port)
        for fields in [{"name": " "}, {"username": "-root"}, {"username": "root user"},
                       {"category": "Missing"}, {"host": "bad host"}]:
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                clean_server(server(**fields), [])

    def test_ssh_defaults_and_explicit_overrides(self):
        self.assertEqual(ssh_args(server()), ["ssh", "home-nas"])
        self.assertEqual(ssh_args(server(host="2001:db8::1", port=2222, username="kaan")),
                         ["ssh", "-p", "2222", "-l", "kaan", "2001:db8::1"])

    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config" / "servers.json"
            self.assertEqual(load_data(path)["servers"], [])
            save_data(path, data())
            self.assertEqual(load_data(path), data())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_corruption_never_changes_file(self):
        invalid = ['{"broken":', '{}', '{"version":1,"version":1}',
                   json.dumps({**data(), "version": 99}),
                   json.dumps({**data(), "categories": ["Home Lab", "Home Lab"]}),
                   json.dumps({**data(), "servers": [server(), server()]})]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "servers.json"
            for contents in invalid:
                with self.subTest(contents=contents):
                    path.write_text(contents)
                    with self.assertRaises(ValueError):
                        Dock(path)
                    self.assertEqual(path.read_text(), contents)

    def test_atomic_save_failure_preserves_previous_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "servers.json"
            save_data(path, data())
            previous = path.read_bytes()
            with patch("server_dock.os.replace", side_effect=OSError("Disk error")):
                with self.assertRaises(OSError):
                    save_data(path, {"version": 1, "categories": [], "servers": []})
            self.assertEqual(path.read_bytes(), previous)
            self.assertEqual(list(path.parent.iterdir()), [path])


class InterfaceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "servers.json"

    async def test_create_edit_and_form_shortcut_isolation(self):
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.press("a")
            self.assertIsInstance(app.screen, Dialog)
            await pilot.press("a", "f", "q", "s")
            self.assertEqual(app.screen.query_one("#name", Input).value, "afqs")
            await pilot.press("ctrl+s")
            self.assertIsInstance(app.screen, Dialog)
            self.assertIn("required", str(app.screen.query_one("#form-error", Static).render()))
            app.screen.query_one("#name", Input).value = "Home NAS"
            await pilot.press("tab")
            self.assertEqual(app.screen.focused.id, "host")
            await pilot.press(*"home-nas")
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(load_data(self.path)["servers"][0]["name"], "Home NAS")
            self.assertIsNone(app.data["servers"][0]["category"])
            self.assertIsNone(app.data["servers"][0]["port"])
            await pilot.press("e")
            app.screen.query_one("#port", Input).value = "70000"
            await pilot.press("ctrl+s")
            self.assertIsInstance(app.screen, Dialog)
            app.screen.query_one("#port", Input).value = "2222"
            app.screen.query_one("#username", Input).value = "kaan"
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(load_data(self.path)["servers"][0]["port"], 2222)

    async def test_tree_help_and_clickable_actions(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app.selected_server()["name"], "Home NAS")
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertTrue(app.nodes[("category", "Home Lab")].is_expanded)
            await pilot.press("down")
            self.assertEqual(app.selected_server()["name"], "Router")
            self.assertIn("Home Lab", str(app.query_one("#details", Static).render()))
            await pilot.press("left", "left")
            self.assertFalse(app.nodes[("category", "Home Lab")].is_expanded)
            await pilot.press("?")
            self.assertFalse(app.query_one("#help").display)
            await pilot.click("#help-toggle")
            self.assertTrue(app.query_one("#help").display)
            await pilot.click("#key-rename")
            self.assertIsInstance(app.screen, Dialog)
            await pilot.press("escape")

    async def test_create_category_and_duplicate_validation(self):
        app = Dock(self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("f")
            await pilot.press(*"Tailscale", "ctrl+s")
            await pilot.pause()
            self.assertEqual(app.data["categories"], ["Tailscale"])
            await pilot.press("a")
            self.assertEqual(app.screen.query_one("#category", Select).value, "Tailscale")
            for expected in ["host", "category", "username", "port", "cancel", "save", "name"]:
                await pilot.press("tab")
                focused = app.screen.focused
                if expected == "category":
                    self.assertIsInstance(focused, Select)
                else:
                    self.assertEqual(focused.id, expected)
            await pilot.press("escape", "f")
            await pilot.press(*"Tailscale", "ctrl+s")
            self.assertIsInstance(app.screen, Dialog)
            self.assertIn("already exists", str(app.screen.query_one("#form-error", Static).render()))
            self.assertEqual(app.data["categories"], ["Tailscale"])

    async def test_categories_move_rename_and_safe_delete(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("m")
            app.screen.query_one("#category", Select).value = "Home Lab"
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual(app.selected_server()["category"], "Home Lab")
            await pilot.press("left", "r")
            app.screen.query_one("#name", Input).value = "Home"
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual([record["category"] for record in app.data["servers"]], ["Home", "Home"])
            await pilot.press("d")
            self.assertNotIsInstance(app.screen, Dialog)
            self.assertIn("not empty", str(app.query_one("#status", Static).render()))
            await pilot.press("right", "down", "d")
            self.assertEqual(app.screen.focused.id, "cancel")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(len(app.data["servers"]), 2)
            await pilot.press("d")
            await pilot.click("#save")
            await pilot.pause()
            self.assertEqual(len(app.data["servers"]), 1)
            # Remove the category from the remaining server via the move dialog.
            tree = app.query_one("#servers", Tree)
            tree.move_cursor(app.nodes[("server", "two")])
            await pilot.pause()
            await pilot.press("m")
            app.screen.query_one("#category", Select).value = ""
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIsNone(app.selected_server()["category"])
            tree.move_cursor(app.nodes[("category", "Home")])
            await pilot.pause()
            await pilot.press("d")
            await pilot.click("#save")
            await pilot.pause()
            self.assertNotIn("Home", load_data(self.path)["categories"])

    async def test_clipboard_and_ssh_return_without_shell(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            with patch("server_dock.subprocess.run") as run:
                await pilot.press("c")
                self.assertEqual(run.call_args.args[0], ["pbcopy"])
                self.assertEqual(run.call_args.kwargs["input"], "home-nas")
                await pilot.press("y")
                self.assertEqual(run.call_args.kwargs["input"], "ssh home-nas")
            with patch.object(app, "suspend", return_value=nullcontext()), patch("server_dock.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess(["ssh"], 0)
                await pilot.press("s")
                run.assert_called_once_with(["ssh", "home-nas"])
                self.assertEqual(app.focused.id, "servers")
            with patch.object(app, "suspend", return_value=nullcontext()), patch("server_dock.subprocess.run", side_effect=OSError("Missing ssh")):
                await pilot.press("s")
                self.assertIn("Could not launch SSH", str(app.query_one("#status", Static).render()))

    async def test_failed_save_keeps_memory_and_disk_unchanged(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            original = copy.deepcopy(app.data)
            with patch("server_dock.save_data", side_effect=OSError("Read-only disk")):
                await pilot.press("e")
                app.screen.query_one("#name", Input).value = "Changed"
                await pilot.press("ctrl+s")
                await pilot.pause()
            self.assertEqual(app.data, original)
            self.assertEqual(load_data(self.path), original)
            self.assertIn("Not saved", str(app.query_one("#status", Static).render()))

    async def test_shared_result_handler_preserves_order_and_rolls_back_category_changes(self):
        original = data()
        original["servers"].append(server(id="three", name="Backup", host="backup"))
        save_data(self.path, original)
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.pause()
            await pilot.press("e")
            app.screen.query_one("#name", Input).value = "Updated NAS"
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertEqual([record["id"] for record in app.data["servers"]], ["one", "two", "three"])
            app.query_one("#servers", Tree).move_cursor(app.nodes[("category", "Home Lab")])
            await pilot.pause()
            before = copy.deepcopy(app.data)
            with patch("server_dock.save_data", side_effect=OSError("Read-only disk")):
                await pilot.press("r")
                app.screen.query_one("#name", Input).value = "Renamed"
                await pilot.press("ctrl+s")
                await pilot.pause()
            self.assertEqual(app.data, before)
            self.assertEqual(load_data(self.path), before)
            app.apply_result("delete", before["servers"][0], False)
            self.assertEqual(app.data, before)

    async def test_form_arrows_tab_and_text_editing(self):
        app = Dock(self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("a")
            name = app.screen.query_one("#name", Input)
            await pilot.press(*"server", "left", "left")
            self.assertEqual(name.cursor_position, 4)
            await pilot.press("right")
            self.assertEqual(name.cursor_position, 5)
            await pilot.press("enter")
            self.assertEqual(app.screen.focused.id, "name")
            await pilot.press("down", "up", "tab", "shift+tab")
            self.assertEqual(app.screen.focused.id, "name")
            for expected in ["host", "category", "username", "port", "cancel", "save", "name"]:
                await pilot.press("down")
                self.assertEqual(app.screen.focused.id, expected)
            for expected in ["save", "cancel", "port", "username", "category", "host", "name"]:
                await pilot.press("up")
                self.assertEqual(app.screen.focused.id, expected)
            self.assertEqual(name.value, "server")
            # Up/down activates neither button; Enter must still activate Cancel.
            app.screen.query_one("#cancel", Button).focus()
            await pilot.press("enter")
            self.assertNotIsInstance(app.screen, Dialog)
            self.assertFalse(self.path.exists())

    async def test_dropdown_closed_navigation_and_open_options(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("a", "down", "down")
            dropdown = app.screen.query_one("#category", Select)
            self.assertEqual(app.screen.focused, dropdown)
            await pilot.press("up")
            self.assertEqual(app.screen.focused.id, "host")
            self.assertFalse(dropdown.expanded)
            await pilot.press("down", "down")
            self.assertEqual(app.screen.focused.id, "username")
            self.assertFalse(dropdown.expanded)
            await pilot.press("up", "enter")
            options = dropdown.query_one(OptionList)
            self.assertTrue(dropdown.expanded)
            self.assertEqual(app.screen.focused, options)
            await pilot.press("down", "down", "up")
            self.assertEqual(options.highlighted, 1)
            self.assertEqual(app.screen.focused, options)
            await pilot.press("enter")
            self.assertEqual(dropdown.value, "Home Lab")
            self.assertFalse(dropdown.expanded)
            self.assertEqual(app.screen.focused, dropdown)
            await pilot.press("space")
            self.assertTrue(dropdown.expanded)
            await pilot.press("down", "enter")
            self.assertEqual(dropdown.value, "Public")
            await pilot.press("space", "escape")
            self.assertIsInstance(app.screen, Dialog)
            self.assertFalse(dropdown.expanded)
            await pilot.press("up")
            self.assertEqual(app.screen.focused.id, "host")

    async def test_other_forms_keep_help_visible_and_buttons_keyboard_accessible(self):
        save_data(self.path, data())
        for key, kind, controls in [("f", "category", ["cancel", "save", "name"]),
                                    ("m", "move", ["cancel", "save", "category"]),
                                    ("d", "delete", ["save", "cancel"])]:
            with self.subTest(kind=kind):
                app = Dock(self.path)
                async with app.run_test(size=(40, 16)) as pilot:
                    await pilot.pause()
                    await pilot.press(key)
                    self.assertEqual(app.screen.kind, kind)
                    dialog = app.screen.query_one("#dialog")
                    help_label = app.screen.query_one("#form-help")
                    self.assertTrue(dialog.content_region.contains_region(help_label.region))
                    for control_id in controls:
                        await pilot.press("down")
                        self.assertEqual(app.screen.focused.id, control_id)
                    await pilot.press("up")
                    self.assertEqual(app.screen.focused.id, "save")
                    await pilot.press("up")
                    self.assertEqual(app.screen.focused.id, "cancel")
                    await pilot.press("enter")
                    self.assertNotIsInstance(app.screen, Dialog)
                    self.assertEqual(load_data(self.path), data())

    async def test_server_list_up_and_down(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            await pilot.press("down", "right", "down")
            self.assertEqual(app.selected_server()["name"], "Router")
            await pilot.press("up")
            self.assertEqual(app.selected(), ("category", "Home Lab"))
            await pilot.press("up")
            self.assertEqual(app.selected_server()["name"], "Home NAS")
            await pilot.press("enter")
            self.assertNotIsInstance(app.screen, Dialog)

    async def test_form_wrapping_and_reachable_controls_at_multiple_sizes(self):
        for size in [(100, 36), (80, 24), (60, 20), (40, 16)]:
            with self.subTest(size=size):
                app = Dock(self.path)
                async with app.run_test(size=size) as pilot:
                    await pilot.press("a")
                    help_label = app.screen.query_one("#form-help", Label)
                    dialog = app.screen.query_one("#dialog")
                    self.assertTrue(dialog.content_region.contains_region(help_label.region))
                    explanation = next(label for label in app.screen.query(Label)
                                       if "IPv4 and IPv6" in str(label.render()))
                    for label in [explanation, help_label]:
                        self.assertLessEqual(label.size.width, label.parent.content_size.width)
                        rendered = " ".join(label.render_line(index).text.strip()
                                            for index in range(label.size.height))
                        self.assertEqual(" ".join(rendered.split()),
                                         " ".join(str(label.render()).split()))
                    self.assertGreater(explanation.size.height, 1)
                    # Focus-driven scrolling must expose the fields and both buttons.
                    for expected in ["host", "category", "username", "port", "cancel", "save"]:
                        await pilot.press("down")
                        await pilot.pause()
                        control = app.screen.focused
                        self.assertEqual(control.id, expected)
                        viewport = app.screen.query_one("#fields").scrollable_content_region
                        self.assertTrue(viewport.contains_region(control.region))
                        self.assertTrue(dialog.content_region.contains_region(help_label.region))
                    app.screen.query_one("#name", Input).value = "Home NAS"
                    app.screen.query_one("#host", Input).value = "home-nas"
                    await pilot.press("enter")
                    await pilot.pause()
                    self.assertNotIsInstance(app.screen, Dialog)
                    self.assertEqual(load_data(self.path)["servers"][0]["name"], "Home NAS")

    async def test_shortcuts_resize_without_truncating_actions(self):
        save_data(self.path, data())
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            for width, height, columns in [(100, 36, 3), (80, 24, 3), (60, 20, 2), (40, 16, 1)]:
                await pilot.resize_terminal(width, height)
                await pilot.pause()
                self.assertEqual(app.query_one("#actions").styles.grid_size_columns, columns)
                self.assertGreaterEqual(app.query_one("#main").size.height, 4)
                for button in app.query("#actions Button"):
                    if button.display:
                        self.assertLessEqual(button.label.cell_length, button.content_size.width)
                await pilot.press("?")
                self.assertFalse(app.query_one("#help").display)
                self.assertLess(app.query_one("#help-toggle").region.bottom, height)
                await pilot.click("#help-toggle")
                self.assertTrue(app.query_one("#help").display)

    async def test_open_form_adapts_to_live_resize(self):
        app = Dock(self.path)
        async with app.run_test(size=(100, 36)) as pilot:
            await pilot.press("a")
            app.screen.query_one("#name", Input).value = "Unsaved server"
            await pilot.press("down", "down", "down", "down")
            for size in [(80, 24), (60, 20), (40, 16), (100, 36)]:
                await pilot.resize_terminal(*size)
                await pilot.pause()
                self.assertEqual(app.screen.focused.id, "port")
                dialog = app.screen.query_one("#dialog")
                self.assertTrue(dialog.content_region.contains_region(app.screen.query_one("#form-help").region))
                viewport = app.screen.query_one("#fields").scrollable_content_region
                self.assertTrue(viewport.contains_region(app.screen.focused.region))
            self.assertEqual(app.screen.query_one("#name", Input).value, "Unsaved server")
            await pilot.press("escape")
            self.assertFalse(self.path.exists())

    async def test_custom_theme_registration_and_builtin_selector(self):
        theme_path = self.path.with_name("themes.json")
        config = {"default": "my-ocean", "themes": {"my-ocean": {
            "primary": "#49A6B8", "background": "#15232D", "dark": True}}}
        theme_path.write_text(json.dumps(config))
        app = Dock(self.path, themes_path=theme_path)
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertEqual(app.theme, "my-ocean")
            self.assertIn("my-ocean", app.available_themes)
            self.assertIn("textual-light", app.available_themes)
            await pilot.press("t")
            self.assertIsInstance(app.screen, CommandPalette)
            await pilot.press(*"textual-light", "enter")
            await pilot.pause()
            self.assertEqual(app.theme, "textual-light")
            self.assertNotIsInstance(app.screen, CommandPalette)
            self.assertEqual(json.loads(theme_path.read_text()), {**config, "default": "textual-light"})
            self.assertEqual(theme_path.stat().st_mode & 0o777, 0o600)
            self.assertFalse(self.path.exists())
            await pilot.press("ctrl+p")
            self.assertIsInstance(app.screen, CommandPalette)

    async def test_theme_selection_survives_restart_and_cancel_keeps_choice(self):
        theme_path = self.path.with_name("themes.json")
        config = {"default": "textual-dark", "themes": {"my-ocean": {"primary": "#49A6B8"}}}
        theme_path.write_text(json.dumps(config))
        save_data(self.path, data())
        before = self.path.read_bytes()
        for choice in ["nord", "my-ocean"]:
            app = Dock(self.path, themes_path=theme_path)
            async with app.run_test() as pilot:
                await pilot.press("t")
                await pilot.press(*choice, "enter")
                await pilot.pause()
                self.assertEqual(app.theme, choice)
                self.assertEqual(json.loads(theme_path.read_text()), {**config, "default": choice})
            restarted = Dock(self.path, themes_path=theme_path)
            async with restarted.run_test() as pilot:
                self.assertEqual(restarted.theme, choice)
                saved = theme_path.read_bytes()
                await pilot.press("t")
                await pilot.press(*"textual-light", "escape")
                await pilot.pause()
                self.assertEqual(restarted.theme, choice)
                self.assertEqual(theme_path.read_bytes(), saved)
        self.assertEqual(self.path.read_bytes(), before)

    async def test_startup_does_not_rewrite_theme_config(self):
        theme_path = self.path.with_name("themes.json")
        contents = '{"default":"nord", "themes":{}}'
        theme_path.write_text(contents)
        app = Dock(self.path, themes_path=theme_path)
        with patch("server_dock.write_atomic") as write:
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertEqual(app.theme, "nord")
            write.assert_not_called()
        self.assertEqual(theme_path.read_text(), contents)

    async def test_theme_save_failure_warns_and_preserves_previous_config(self):
        theme_path = self.path.with_name("themes.json")
        contents = '{"default":"textual-dark","themes":{}}'
        theme_path.write_text(contents)
        app = Dock(self.path, themes_path=theme_path)
        async with app.run_test() as pilot:
            with patch("server_dock.os.replace", side_effect=OSError("Disk error")):
                await pilot.press("t")
                await pilot.press(*"nord", "enter")
                await pilot.pause()
            self.assertEqual(app.theme, "nord")
            self.assertIn("Cannot save theme", str(app.query_one("#status", Static).render()))
            self.assertIn("Disk error", str(app.query_one("#status", Static).render()))
        self.assertEqual(theme_path.read_text(), contents)
        self.assertEqual(list(theme_path.parent.iterdir()), [theme_path])

    async def test_theme_choice_creates_missing_config_but_preserves_corrupt_config(self):
        theme_path = self.path.with_name("themes.json")
        for contents in [None, "{broken"]:
            with self.subTest(contents=contents):
                if contents is not None:
                    theme_path.write_text(contents)
                app = Dock(self.path, themes_path=theme_path)
                async with app.run_test() as pilot:
                    await pilot.press("t")
                    await pilot.press(*"nord", "enter")
                    await pilot.pause()
                    self.assertEqual(app.theme, "nord")
                    if contents is None:
                        self.assertEqual(json.loads(theme_path.read_text()), {"default": "nord", "themes": {}})
                    else:
                        self.assertEqual(theme_path.read_text(), contents)
                        self.assertIn("Cannot save theme", str(app.query_one("#status", Static).render()))
                self.assertFalse(self.path.exists())

    async def test_bad_theme_config_warns_without_affecting_server_data(self):
        save_data(self.path, data())
        before = self.path.read_bytes()
        theme_path = self.path.with_name("themes.json")
        for contents in [None, "{broken"]:
            with self.subTest(contents=contents):
                if contents is not None:
                    theme_path.write_text(contents)
                app = Dock(self.path, themes_path=theme_path)
                async with app.run_test(size=(80, 24)) as pilot:
                    await pilot.pause()
                    self.assertEqual(app.theme, "textual-dark")
                    self.assertIn("Cannot load themes", str(app.query_one("#status", Static).render()))
                    self.assertEqual(app.data, data())
                    await pilot.press("a")
                    self.assertIsInstance(app.screen, Dialog)
                self.assertEqual(self.path.read_bytes(), before)
                self.assertEqual(theme_path.read_text() if theme_path.exists() else None, contents)


class ThemeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.path = Path(self.temporary.name) / "themes.json"

    def test_valid_builtin_and_custom_defaults(self):
        for name in ["textual-dark", "textual-light", "example"]:
            config = {"default": name, "themes": {"example": {"primary": "#123456", "dark": False}}}
            self.path.write_text(json.dumps(config))
            default, themes, warning = load_themes(self.path)
            self.assertEqual(default, name)
            self.assertEqual(themes[0].name, "example")
            self.assertFalse(themes[0].dark)
            self.assertEqual(warning, "")

    def test_invalid_configs_fall_back_without_writes(self):
        invalid = ["{broken", "[]", "{}", '{"default":"textual-dark","default":"textual-light","themes":{}}']
        for fields in [
            {"default": []}, {"default": "missing"}, {"themes": []},
            {"themes": {"example": {"primary": "not-a-color"}}},
            {"themes": {"example": {"primary": 123}}},
            {"themes": {"example": {"primary": "#123456", "dark": "yes"}}},
            {"themes": {"example": {"primary": "#123456", "typo": "red"}}},
            {"themes": {"textual-dark": {"primary": "red"}}},
            {"themes": {"Bad Name": {"primary": "red"}}},
            {"themes": {"example": {"background": "black"}}},
            {"themes": {"example": None}},
        ]:
            invalid.append(json.dumps({"default": "textual-dark", "themes": {}, **fields}))
        for contents in invalid:
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                default, themes, warning = load_themes(self.path)
                self.assertEqual(default, "textual-dark")
                self.assertEqual(themes, [])
                self.assertIn(str(self.path), warning)
                self.assertEqual(self.path.read_text(), contents)

    def test_missing_unreadable_and_non_utf8_configs(self):
        default, themes, warning = load_themes(self.path)
        self.assertEqual((default, themes), ("textual-dark", []))
        self.assertTrue(warning)
        self.assertFalse(self.path.exists())
        with patch.object(Path, "read_text", side_effect=PermissionError("Access denied")):
            self.assertIn("Access denied", load_themes(self.path)[2])
        self.path.write_bytes(b"\xff\xfe")
        self.assertEqual(load_themes(self.path)[:2], ("textual-dark", []))
        self.assertEqual(self.path.read_bytes(), b"\xff\xfe")

    def test_saving_theme_preserves_palettes_edited_while_app_is_open(self):
        self.path.write_text('{"default":"textual-dark","themes":{}}')
        app = Dock(self.path.with_name("servers.json"), themes_path=self.path)
        config = {"default": "textual-dark", "themes": {"added": {"primary": "#123456"}}}
        self.path.write_text(json.dumps(config))
        save_theme(app.themes_path, "nord")
        self.assertEqual(json.loads(self.path.read_text()), {**config, "default": "nord"})

    def test_saving_theme_preserves_broken_symlink_and_rejects_unknown_theme(self):
        self.path.symlink_to(self.path.with_name("missing.json"))
        with self.assertRaises(ValueError):
            save_theme(self.path, "nord")
        self.assertTrue(self.path.is_symlink())
        self.assertFalse(self.path.exists())
        self.path.unlink()
        contents = '{"default":"textual-dark","themes":{}}'
        self.path.write_text(contents)
        with self.assertRaises(ValueError):
            save_theme(self.path, "unknown-theme")
        self.assertEqual(self.path.read_text(), contents)

    def test_theme_migration_preserves_customizations_and_owner_only_permissions(self):
        source = self.path.with_name("legacy-themes.json")
        contents = '{"default":"nord","themes":{}}\n'
        source.write_text(contents)
        init_themes(self.path, source)
        self.assertEqual(self.path.read_text(), contents)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.path.write_text("{broken user config")
        init_themes(self.path, source)
        self.assertEqual(self.path.read_text(), "{broken user config")
        self.assertEqual(source.read_text(), contents)

    def test_theme_initialization_never_overwrites_a_racing_write_or_broken_symlink(self):
        source = self.path.with_name("legacy-themes.json")
        source.write_text('{"default":"textual-dark","themes":{}}')
        link = os.link
        def racing_link(temporary, target):
            target.write_text("Someone else's settings")
            link(temporary, target)
        with patch("server_dock.os.link", side_effect=racing_link):
            init_themes(self.path, source)
        self.assertEqual(self.path.read_text(), "Someone else's settings")
        self.assertFalse(any(path.name.startswith(".themes.json.") for path in self.path.parent.iterdir()))
        symlink = self.path.with_name("broken-link.json")
        symlink.symlink_to(self.path.with_name("missing.json"))
        init_themes(symlink, source)
        self.assertTrue(symlink.is_symlink())
        self.assertFalse(symlink.exists())

    def test_theme_initialization_failure_leaves_no_partial_configuration(self):
        source = self.path.with_name("legacy-themes.json")
        source.write_text('{"default":"textual-dark","themes":{}}')
        with patch("server_dock.os.link", side_effect=OSError("Disk error")):
            with self.assertRaises(OSError):
                init_themes(self.path, source)
        self.assertFalse(self.path.exists())
        self.assertEqual(list(self.path.parent.iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
