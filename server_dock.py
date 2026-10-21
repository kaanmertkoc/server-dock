#!/usr/bin/env python3
"""Server Dock: a local, keyboard-first SSH address book."""

import argparse
import copy
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import uuid

from rich.text import Text
from textual.actions import parse as parse_action
from textual.app import App, ComposeResult, SuspendNotSupported
from textual.binding import Binding
from textual.color import Color, ColorParseError
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.theme import BUILTIN_THEMES, Theme
from textual.widgets import Button, Input, Label, Select, Static, Tree


__version__ = "0.1.1"

CONFIG_DIR = Path.home() / ".config" / "server-dock"
DATA_PATH = CONFIG_DIR / "servers.json"
LEGACY_DIR = Path.home() / ".config" / "kaans-dock"
THEMES_PATH = Path(__file__).resolve().with_name("themes.json")
USER_THEMES = DATA_PATH.with_name("themes.json")
SETTINGS_PATH = CONFIG_DIR / "settings.json"
FIELDS = [("name", "Name *", ""), ("host", "Host / IP *", "IP, hostname, or SSH alias"),
          ("category", "Category (optional)", ""), ("username", "SSH username (optional)", "SSH default"),
          ("port", "SSH port (optional)", "SSH config, otherwise 22")]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def clean_name(value, label="Name"):
    require(isinstance(value, str) and value.strip(), f"{label} is required.")
    require(not any(ord(char) < 32 or ord(char) == 127 for char in value),
            f"{label} must not contain control characters.")
    return value.strip()


def default_paths():
    """Keep existing users on their address book without copying private data."""
    legacy_data = LEGACY_DIR / "servers.json"
    legacy_themes = LEGACY_DIR / "themes.json"
    data = DATA_PATH if DATA_PATH.exists() or DATA_PATH.is_symlink() else legacy_data
    if not data.exists() and not data.is_symlink():
        data = DATA_PATH
    themes = USER_THEMES
    if not themes.exists() and not themes.is_symlink() and (legacy_themes.exists() or legacy_themes.is_symlink()):
        themes = legacy_themes
    return data, themes


def load_settings(path, default_name="Server Dock"):
    try:
        settings = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("The settings file is a broken symbolic link.") from None
        return {"name": default_name}
    require(isinstance(settings, dict) and set(settings) == {"name"}, "Settings must contain a name field.")
    return {"name": clean_name(settings["name"], "Dock name")}


def host_type(host):
    """Validate a literal address or a conservative hostname / SSH alias."""
    host = clean_name(host, "Host / IP")
    require(not any(char.isspace() for char in host) and not host.startswith("-"),
            "Host / IP must not contain spaces or start with '-'.")
    require("%" not in host or re.fullmatch(r"[A-Za-z0-9_.-]+", host.split("%", 1)[1]),
            "An IPv6 scope must be a simple interface name, such as en0.")
    try:
        return f"IPv{ipaddress.ip_address(host).version}"
    except ValueError:
        if ":" in host or re.fullmatch(r"[0-9.]+", host):
            raise ValueError("Enter a valid IPv4 or IPv6 address.") from None
    labels = host.rstrip(".").split(".")
    require(all(re.fullmatch(r"[A-Za-z0-9_](?:[A-Za-z0-9_-]*[A-Za-z0-9_])?", part) for part in labels),
            "Use an IP, hostname, or SSH alias (no spaces or shell symbols).")
    return "Hostname / SSH alias"


def clean_port(value):
    if value is None or value == "":
        return None
    require(type(value) is int or isinstance(value, str) and re.fullmatch(r"[0-9]{1,5}", value),
            "Port must be a whole number from 1 to 65535, or blank.")
    port = int(value)
    require(1 <= port <= 65535, "Port must be between 1 and 65535.")
    return port


def clean_server(record, categories):
    require(isinstance(record, dict) and set(record) == {"id", "name", "host", "username", "port", "category"},
            "Each server must have id, name, host, username, port, and category.")
    record = dict(record)
    record["id"] = clean_name(record["id"], "Server ID")
    record["name"] = clean_name(record["name"])
    record["host"] = clean_name(record["host"], "Host / IP")
    host_type(record["host"])
    username = record["username"]
    require(isinstance(username, str), "Username must be text (or an empty string).")
    username = username.strip()
    require(not username or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@$-]*", username),
            "Username must not start with '-' or contain spaces / control characters.")
    record["username"] = username
    record["port"] = clean_port(record["port"])
    category = record["category"]
    require(category is None or isinstance(category, str) and category in categories,
            "The server's category does not exist.")
    return record


def clean_data(data):
    require(isinstance(data, dict) and set(data) == {"version", "categories", "servers"},
            "Expected an object with version, categories, and servers.")
    require(type(data["version"]) is int and data["version"] == 1, "Unsupported data version (expected 1).")
    require(isinstance(data["categories"], list) and isinstance(data["servers"], list), "Categories and servers must be lists.")
    categories = [clean_name(name, "Category name") for name in data["categories"]]
    require(len(set(categories)) == len(categories), "Category names must be unique.")
    servers = [clean_server(record, categories) for record in data["servers"]]
    require(len({record["id"] for record in servers}) == len(servers), "Server IDs must be unique.")
    return {"version": 1, "categories": categories, "servers": servers}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON field: {key}")
        result[key] = value
    return result


def clean_themes(config):
    """Validate everything before registering or saving any palette."""
    colors = {"primary", "secondary", "accent", "foreground", "background",
              "surface", "panel", "boost", "success", "warning", "error"}
    require(isinstance(config, dict) and set(config) == {"default", "themes"}, "Expected default and themes fields.")
    require(isinstance(config["themes"], dict), "Themes must be a mapping of names to palettes.")
    themes = []
    for name, palette in config["themes"].items():
        require(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", name) and name not in BUILTIN_THEMES, f"Invalid or reserved theme name: {name}")
        require(isinstance(palette, dict) and "primary" in palette, f"Theme {name} needs a palette with a primary color.")
        require(not set(palette) - colors - {"dark"} and type(palette.get("dark", True)) is bool,
                f"Theme {name}: unsupported fields or non-boolean dark value.")
        for field, value in palette.items():
            if field != "dark":
                require(isinstance(value, str), f"Theme {name}: {field} must be a color string.")
                Color.parse(value)
        themes.append(Theme(name=name, **palette))
    default = config["default"]
    require(isinstance(default, str) and default in set(BUILTIN_THEMES) | set(config["themes"]),
            "Default theme is not a built-in or configured theme.")
    return default, themes


def read_theme_config(path):
    config = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=unique_object)
    default, themes = clean_themes(config)
    return config, default, themes


def load_themes(path):
    try:
        _, default, themes = read_theme_config(path)
        return default, themes, ""
    except (OSError, ValueError, ColorParseError, RecursionError) as error:
        return "textual-dark", [], f"Cannot load themes from {path}: {error}. Using textual-dark."


def save_theme(path, name):
    # Re-read to preserve palettes edited since startup. Never replace corrupt config.
    try:
        config, _, _ = read_theme_config(path)
    except FileNotFoundError:
        if path.is_symlink():
            raise ValueError("The themes file is a broken symbolic link.") from None
        config = {"default": "textual-dark", "themes": {}}
    config["default"] = name
    clean_themes(config)
    write_atomic(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")


def load_data(path):
    try:
        with path.open(encoding="utf-8") as source:
            return clean_data(json.load(source, object_pairs_hook=unique_object))
    except FileNotFoundError:
        # Only a genuinely missing file is a new address book, not a broken symlink.
        if path.is_symlink():
            raise ValueError("The data file is a broken symbolic link.") from None
        return {"version": 1, "categories": [], "servers": []}


def write_atomic(path, text, overwrite=True):
    """Owner-only writes; link instead of replace when initializing user config."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=f".{path.name}.", delete=False) as target:
            temporary = Path(target.name)
            target.write(text)
            target.flush()
            os.fsync(target.fileno())
        if overwrite:
            os.replace(temporary, path)
        else:
            os.link(temporary, path)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def save_data(path, data):
    write_atomic(path, json.dumps(clean_data(data), indent=2, ensure_ascii=False) + "\n")


def init_themes(path=USER_THEMES, source=None):
    if path.exists() or path.is_symlink():
        return
    source = source or (THEMES_PATH if THEMES_PATH.exists() else Path(sys.prefix) / "share/server-dock/themes.json")
    try:
        write_atomic(path, source.read_text(encoding="utf-8"), overwrite=False)
    except FileExistsError:
        pass  # Another launch initialized it first; never overwrite user settings.


def ssh_args(record):
    args = ["ssh"]
    if record["port"] is not None:
        args.extend(["-p", str(record["port"])])
    if record["username"]:
        args.extend(["-l", record["username"]])
    args.append(record["host"])
    return args


class ServerTree(Tree):
    BINDINGS = [("enter", "toggle_node", "Toggle category"),
                ("right", "open_category", "Expand"),
                ("left", "close_category", "Collapse")]

    def action_open_category(self):
        node = self.cursor_node
        if node and node.allow_expand:
            node.expand()

    def action_close_category(self):
        node = self.cursor_node
        if node and node.allow_expand and node.is_expanded:
            node.collapse()
        elif node and node.parent is not self.root:
            self.move_cursor(node.parent)


class FormScroll(VerticalScroll, can_focus=False):
    # Ordinary bindings let inputs and open dropdowns handle their own keys first.
    BINDINGS = [("up", "app.focus_previous", "Previous field"),
                ("down", "app.focus_next", "Next field")]


class FormSelect(Select):
    BINDINGS = FormScroll.BINDINGS


class Dialog(ModalScreen):
    """Small form shared by server, category, move, and confirmation dialogs."""

    BINDINGS = [("escape", "cancel", "Cancel"), ("ctrl+s", "save", "Save")]

    def __init__(self, kind, title, categories, record=None):
        super().__init__()
        self.kind, self.title, self.categories = kind, title, categories
        self.record = record or {}
        self.fields = {"server": FIELDS, "category": FIELDS[:1], "move": FIELDS[2:3], "delete": []}[kind]

    def compose(self) -> ComposeResult:
        with Vertical(id="dialog", classes=self.kind):
            yield Label(Text(self.title), id="dialog-title")
            with FormScroll(id="fields"):
                for name, label, placeholder in self.fields:
                    yield Label(label)
                    value = self.record.get(name)
                    if name == "category":
                        yield FormSelect([("No category", "")] + [(name, name) for name in self.categories],
                                         value=value or "", allow_blank=False, id=name)
                    else:
                        yield Input("" if value is None else str(value), placeholder=placeholder, id=name)
                    if name == "host":
                        yield Label("IPv4 and IPv6 supported; aliases use your existing SSH config.")
                if self.kind == "delete":
                    yield Label("This cannot be undone. Cancel keeps your data.")
                yield Static("", id="form-error", markup=False)
                with Horizontal(id="dialog-buttons"):
                    yield Button("Cancel", id="cancel")
                    yield Button("Delete" if self.kind == "delete" else "Save", id="save",
                                 variant="error" if self.kind == "delete" else "primary")
            yield Label("↑/↓ or Tab/Shift+Tab: fields · Esc: cancel\n"
                        + ("Enter: activate button" if self.kind == "delete" else
                           "Enter/Space: open dropdown · Ctrl+S: save"), id="form-help")

    def on_mount(self):
        self.query_one("#cancel" if self.kind == "delete" else
                       "#category" if self.kind == "move" else "#name").focus()

    def on_resize(self):
        if isinstance(self.focused, (Input, Select, Button)):
            self.call_after_refresh(self.focused.scroll_visible, animate=False)

    def action_cancel(self):
        self.dismiss(None)

    def action_save(self):
        # Deleting requires explicitly activating the Delete button, not Ctrl+S.
        if self.kind == "delete":
            return
        try:
            result = dict(self.record)
            for name, _, _ in self.fields:
                value = self.query_one(f"#{name}").value
                if name == "name":
                    value = clean_name(value)
                result[name] = (value or None) if name == "category" else value.strip()
            if self.kind == "server":
                result = clean_server(result, self.categories)
            if self.kind == "category":
                result["name"] = clean_name(result["name"])
                require(result["name"] == self.record.get("name") or result["name"] not in self.categories,
                        "A category with that name already exists.")
        except ValueError as error:
            self.query_one("#form-error", Static).update(str(error))
        else:
            self.dismiss(result)

    def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        if event.button.id == "cancel":
            self.action_cancel()
        elif self.kind == "delete":
            self.dismiss(True)
        else:
            self.action_save()


class Dock(App):
    TITLE = "Server Dock"
    BINDINGS = [
        Binding("a", "form('add')", "Add server", id="add"),
        Binding("e", "form('server')", "Edit server", id="edit"),
        Binding("m", "form('move')", "Change category", id="move"),
        Binding("d", "form('delete')", "Delete", id="delete"),
        Binding("s", "ssh", "Connect with SSH", id="ssh"),
        Binding("c", "copy(False)", "Copy host / IP", id="copy_host"),
        Binding("y", "copy(True)", "Copy SSH command", id="copy_command"),
        Binding("f", "form('category')", "Create category", id="folder"),
        Binding("r", "form('rename')", "Rename category", id="rename"),
        Binding("t", "change_theme", "Themes", id="themes"),
        Binding("question_mark", "help", "Toggle help", id="help"),
        Binding("q", "quit", "Quit", id="quit"),
    ]

    CSS = """
    Screen { background: $background; }
    #title { height: 1; background: $primary; color: $text-primary; text-style: bold; padding: 0 1; }
    #main { height: 1fr; }
    #servers { width: 42%; border: round $primary; }
    #details-scroll { width: 1fr; border: round $primary; padding: 0 1; }
    #details { height: auto; }
    #help { height: auto; max-height: 45%; border: round $primary; padding: 0 1; }
    #navigation { height: auto; }
    #actions { layout: grid; grid-size: 3; grid-rows: 1; height: auto; }
    #actions Button, #help-toggle {
        height: 1; min-width: 1; width: 1fr; border: none; padding: 0;
        background: $surface; text-align: left;
    }
    #actions Button:hover, #help-toggle:hover { background: $primary; }
    #help-toggle { width: 100%; }
    #status { height: auto; max-height: 3; padding: 0 1; color: $text-muted; }
    Dialog { align: center middle; }
    #dialog { width: 64; max-width: 95%; height: 95%; max-height: 20;
              border: thick $primary; background: $surface; padding: 1 2; }
    #dialog.server { max-height: 40; }
    #fields { height: 1fr; min-height: 3; }
    #dialog-title { text-style: bold; margin-bottom: 1; }
    #dialog Label { width: 1fr; height: auto; text-wrap: wrap; }
    #dialog Input, #dialog Select { margin-bottom: 1; }
    #form-error { height: auto; color: $error; }
    #form-help { margin-top: 1; }
    #dialog-buttons { height: 3; margin-top: 1; }
    #dialog-buttons Button { width: 1fr; min-width: 1; padding: 0 1; }
    """

    def __init__(self, path=DATA_PATH, themes_path=THEMES_PATH, name="Server Dock"):
        super().__init__()
        self.title = clean_name(name, "Dock name")
        self.path = Path(path).expanduser()
        self.data = load_data(self.path)
        self.themes_path = Path(themes_path).expanduser()
        self.default_theme, self.custom_themes, self.theme_warning = load_themes(self.themes_path)
        self.help_visible = True
        self.nodes = {}

    def compose(self) -> ComposeResult:
        yield Static(self.title, id="title", markup=False)
        with Horizontal(id="main"):
            yield ServerTree("Servers", id="servers")
            with VerticalScroll(id="details-scroll"):
                yield Static("", id="details", markup=False)
        with VerticalScroll(id="help"):
            yield Static("↑ ↓ Select · ← → Collapse / expand · Enter Toggle category", id="navigation")
            with Vertical(id="actions"):
                for binding in self.BINDINGS:
                    key = "?" if binding.key == "question_mark" else binding.key
                    yield Button(f"{key}  {binding.description}", id=f"key-{binding.id}")
        yield Button("? Hide keys & actions", id="help-toggle")
        yield Static("", id="status", markup=False)

    def on_mount(self):
        for theme in self.custom_themes:
            self.register_theme(theme)
        self.theme = self.default_theme
        self.query_one("#servers", ServerTree).border_title = "Servers"
        self.query_one("#details-scroll").border_title = "Selected item"
        self.query_one("#help").border_title = "Keys & actions"
        self.rebuild()
        self.status(self.theme_warning or f"Saved locally: {self.path}")
        if self.theme_warning:
            self.notify(self.theme_warning, severity="warning", timeout=10)
        self.theme_changed_signal.subscribe(self, self.persist_theme)

    def persist_theme(self, theme):
        # Ignore startup and stale notifications queued before the current choice.
        if theme.name == self.default_theme or theme.name != self.theme:
            return
        try:
            save_theme(self.themes_path, theme.name)
        except (OSError, ValueError, ColorParseError, RecursionError) as error:
            message = f"Cannot save theme to {self.themes_path}: {error}. Using it for this session only."
            self.status(message)
            self.notify(message, severity="warning", timeout=10)
        else:
            self.default_theme = theme.name
            self.status(f"Theme saved: {theme.name}")

    def on_resize(self, event):
        columns = 3 if event.size.width >= 80 else 2 if event.size.width >= 55 else 1
        self.query_one("#actions").styles.grid_size_columns = columns

    def status(self, message):
        self.query_one("#status", Static).update(message)

    def selected(self):
        node = self.query_one("#servers", ServerTree).cursor_node
        return node.data if node and node.data else None

    def selected_server(self):
        item = self.selected()
        if item and item[0] == "server":
            return next((record for record in self.data["servers"] if record["id"] == item[1]), None)
        return None

    def check_action(self, action, parameters):
        # Main-screen bindings must never fire inside a form or confirmation.
        if isinstance(self.screen, ModalScreen) and action in {parse_action(binding.action)[1] for binding in self.BINDINGS}:
            return False
        kind = parameters[0] if action == "form" else None
        if action in {"ssh", "copy"} or kind in {"server", "move"}:
            return self.selected_server() is not None
        if kind == "rename":
            return bool(self.selected() and self.selected()[0] == "category")
        if kind == "delete":
            return self.selected() is not None
        return True

    def rebuild(self, selection=None):
        tree = self.query_one("#servers", ServerTree)
        selection = selection or self.selected()
        expanded = {key[1] for key, node in self.nodes.items()
                    if key[0] == "category" and node.is_expanded}
        tree.clear()
        tree.show_root = False
        tree.root.expand()
        self.nodes = {}
        for category in [None] + self.data["categories"]:
            parent = tree.root
            if category is not None:
                key = ("category", category)
                parent = tree.root.add(Text(category), data=key, expand=category in expanded)
                self.nodes[key] = parent
            for record in self.data["servers"]:
                if record["category"] == category:
                    key = ("server", record["id"])
                    self.nodes[key] = parent.add_leaf(Text(record["name"]), data=key)
        node = self.nodes.get(selection) or next(iter(self.nodes.values()), None)
        if node and node.parent is not tree.root:
            node.parent.expand()
        tree.call_after_refresh(tree.move_cursor, node)
        tree.focus()
        self.update_details()

    def on_tree_node_highlighted(self, event: Tree.NodeHighlighted):
        self.update_details()

    def update_details(self):
        record = self.selected_server()
        item = self.selected()
        if record:
            details = (f"Name:      {record['name']}\n\nHost / IP: {record['host']}\n"
                       f"Type:      {host_type(record['host'])}\n"
                       f"Category:  {record['category'] or 'None'}\n"
                       f"Username:  {record['username'] or 'SSH default'}\n"
                       f"Port:      {record['port'] or 'SSH config, otherwise 22'}\n\n"
                       f"{shlex.join(ssh_args(record))}")
        elif item and item[0] == "category":
            count = sum(record["category"] == item[1] for record in self.data["servers"])
            details = f"Category: {item[1]}\n\n{count} server(s)\n\nEnter: open / close\na: add a server here\nr: rename\nd: delete (only when empty)"
        else:
            details = "No servers yet.\n\nPress a to add your first server.\nCategories are optional; press f to create one."
        self.query_one("#details", Static).update(details)
        for binding in self.BINDINGS:
            self.query_one(f"#key-{binding.id}", Button).display = bool(self.check_action(*parse_action(binding.action)[1:]))
        self.query_one("#key-delete", Button).label = "d  Delete empty category" if item and item[0] == "category" else "d  Delete server"
        self.query_one("#key-help", Button).label = "?  Hide this panel"

    def commit(self, candidate, message, selection=None):
        try:
            save_data(self.path, candidate)
        except (OSError, ValueError) as error:
            self.status(f"Not saved: {error}. Your previous data is unchanged.")
            return
        self.data = candidate
        self.rebuild(selection)
        self.status(message)

    def action_form(self, kind):
        item = self.selected()
        record = self.selected_server() or {"name": item[1] if item else ""}
        if kind == "add":
            category = item[1] if item and item[0] == "category" else record.get("category")
            record = {"id": uuid.uuid4().hex, "name": "", "host": "", "username": "", "port": None, "category": category}
        if kind == "category":
            record = {"name": ""}
        if kind == "delete" and "id" not in record and any(r["category"] == record["name"] for r in self.data["servers"]):
            self.status("This category is not empty. Move or delete its servers first.")
            return
        titles = {"add": "Add server", "server": "Edit server", "move": "Change category",
                  "category": "Create category", "rename": "Rename category", "delete": f"Delete {record['name']}?"}
        form = "server" if kind == "add" else "category" if kind == "rename" else kind
        self.push_screen(Dialog(form, titles[kind], self.data["categories"], record),
                         lambda result: self.apply_result(form, record, result))

    def apply_result(self, kind, original, result):
        if result is None or (kind == "delete" and result is not True):
            return
        candidate = copy.deepcopy(self.data)
        selection = None
        if kind in {"server", "move"}:
            # Dict replacement preserves order; an added server is appended.
            records = {record["id"]: record for record in candidate["servers"]}
            records[result["id"]] = result
            candidate["servers"] = list(records.values())
            selection = ("server", result["id"])
        elif kind == "category":
            old, name = original["name"], result["name"]
            if old:
                candidate["categories"] = [name if category == old else category for category in candidate["categories"]]
            else:
                candidate["categories"].append(name)
            for record in candidate["servers"]:
                if old and record["category"] == old:
                    record["category"] = name
            selection = ("category", name)
        elif "id" in original:
            candidate["servers"] = [record for record in candidate["servers"] if record["id"] != original["id"]]
        else:
            candidate["categories"].remove(original["name"])
        self.commit(candidate, f"Deleted {original['name']}." if kind == "delete" else "Saved.", selection)

    def action_copy(self, command=False):
        if not (record := self.selected_server()):
            return
        value = shlex.join(ssh_args(record)) if command else record["host"]
        try:
            subprocess.run(["pbcopy"], input=value, text=True, check=True,
                           capture_output=True, timeout=3)
        except (OSError, subprocess.SubprocessError) as error:
            self.status(f"Could not copy to the macOS clipboard: {error}")
        else:
            self.status("SSH command copied." if command else "Host / IP copied.")

    def action_ssh(self):
        if not (record := self.selected_server()):
            return
        try:
            with self.suspend():
                # Catch errors INSIDE suspend so Textual always resumes terminal mode.
                try:
                    result = subprocess.run(ssh_args(record))
                    message = f"SSH session ended (exit {result.returncode})."
                except OSError as error:
                    message = f"Could not launch SSH: {error}"
                except KeyboardInterrupt:
                    message = "SSH session interrupted."
        except SuspendNotSupported:
            message = "Interactive SSH needs a local terminal. You can still copy its command."
        self.status(message)
        self.query_one("#servers", ServerTree).focus()

    def action_help(self):
        self.help_visible = not self.help_visible
        self.query_one("#help").display = self.help_visible
        self.query_one("#help-toggle", Button).label = "? Hide keys & actions" if self.help_visible else "? Show keys & actions"
        self.query_one("#servers", ServerTree).focus()

    async def on_button_pressed(self, event: Button.Pressed):
        event.stop()
        action = "help" if event.button.id == "help-toggle" else next(
            binding.action for binding in self.BINDINGS if f"key-{binding.id}" == event.button.id)
        await self.run_action(action)
        if not isinstance(self.screen, ModalScreen):
            self.query_one("#servers", ServerTree).focus()


def main(argv=None):
    default_data, default_themes = default_paths()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--data", type=Path, default=default_data, help="JSON file (default: %(default)s)")
    parser.add_argument("--themes", type=Path, default=default_themes, help="Theme configuration (default: %(default)s)")
    parser.add_argument("--settings", type=Path, default=SETTINGS_PATH, help="Display settings (default: %(default)s)")
    parser.add_argument("--name", help="Set and remember your dock's display name")
    args = parser.parse_args(argv)
    settings_path = args.settings.expanduser()
    try:
        default_name = "Kaan's Dock" if default_data == LEGACY_DIR / "servers.json" else "Server Dock"
        settings = load_settings(settings_path, default_name)
        if args.name is not None:
            settings["name"] = clean_name(args.name, "Dock name")
    except (OSError, ValueError, RecursionError) as error:
        print(f"Cannot load display settings from {settings_path}: {error}", file=sys.stderr)
        return 1
    try:
        # Migrate legacy/source palettes once; installed wheels ship the same example.
        if args.themes == USER_THEMES:
            try:
                init_themes()
            except (OSError, ValueError) as error:
                print(f"Could not initialize themes: {error}", file=sys.stderr)
        app = Dock(args.data, themes_path=args.themes.expanduser(), name=settings["name"])
    except (OSError, ValueError, RecursionError) as error:
        print(f"Cannot load {args.data.expanduser()}: {error}\n"
              "The file has NOT been changed. Fix it or choose another file with --data.", file=sys.stderr)
        return 1
    if args.name is not None:
        try:
            write_atomic(settings_path, json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        except OSError as error:
            print(f"Cannot save display settings to {settings_path}: {error}", file=sys.stderr)
            return 1
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
