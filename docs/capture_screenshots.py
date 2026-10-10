"""Capture the real interface with fictional data; never touch user settings."""

import asyncio
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server_dock import Dialog, Dock


async def capture():
    # CI and agent terminals may disable color; screenshots should show the theme.
    os.environ.pop("NO_COLOR", None)
    output = ROOT / "docs" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    app = Dock(ROOT / "examples" / "servers.json", ROOT / "themes.json")
    async with app.run_test(size=(108, 32)) as pilot:
        await pilot.pause()
        for key, node in app.nodes.items():
            if key[0] == "category":
                node.expand()
        await pilot.pause()
        await pilot.press("down")
        assert app.selected() == ("server", "web"), app.selected()
        app.status("Your servers. Your terminal. Your dock.")
        await pilot.pause()
        app.save_screenshot("server-dock.svg", path=str(output))
        app.title = "Kaan's Dock"
        app.query_one("#title").update(app.title)
        await pilot.press("e")
        await pilot.pause()
        assert isinstance(app.screen, Dialog)
        app.save_screenshot("custom-dock.svg", path=str(output))
    for screenshot in output.glob("*.svg"):
        screenshot.write_text("\n".join(line.rstrip() for line in screenshot.read_text().splitlines()) + "\n")
    print(f"Saved two screenshots to {output}")


if __name__ == "__main__":
    asyncio.run(capture())
