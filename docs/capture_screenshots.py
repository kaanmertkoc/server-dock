"""Capture the real interface with fictional data; never touch user settings."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server_dock import Dock


async def capture():
    # CI and agent terminals may disable color; screenshots should show the theme.
    os.environ.pop("NO_COLOR", None)
    output = ROOT / "docs" / "screenshots"
    output.mkdir(parents=True, exist_ok=True)
    scenes = [("server-dock", "textual-dark", (108, 32)),
              ("theme-nord", "nord", (80, 24)),
              ("theme-dracula", "dracula", (80, 24)),
              ("theme-light", "textual-light", (80, 24))]
    config = json.loads((ROOT / "themes.json").read_text())
    with tempfile.TemporaryDirectory() as directory:
        theme_path = Path(directory) / "themes.json"
        for filename, theme, size in scenes:
            theme_path.write_text(json.dumps({**config, "default": theme}))
            app = Dock(ROOT / "examples" / "servers.json", theme_path)
            async with app.run_test(size=size) as pilot:
                await pilot.pause()
                for key, node in app.nodes.items():
                    if key[0] == "category":
                        node.expand()
                await pilot.pause()
                await pilot.press("down")
                assert app.selected() == ("server", "web"), app.selected()
                assert app.theme == theme, app.theme
                app.status("Your servers. Your terminal. Your dock.")
                await pilot.pause()
                screenshot = Path(app.save_screenshot(f"{filename}.svg", path=str(output)))
                screenshot.write_text("\n".join(line.rstrip() for line in screenshot.read_text().splitlines()) + "\n")
    print(f"Saved {len(scenes)} screenshots to {output}")


if __name__ == "__main__":
    asyncio.run(capture())
