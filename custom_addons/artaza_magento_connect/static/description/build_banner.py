#!/usr/bin/env python3
"""Render banner.src.html to banner.png, the artwork the App Store shows.

The banner is drawn by a browser rather than an image editor so that changing a
word is a text edit. It is rendered at 2400x1200 -- twice the size the store
displays -- so it stays sharp on a retina panel.

Any locally installed Chrome will do; nothing is added to the module's
dependencies. Usage:

    python3 build_banner.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = HERE / "banner.src.html"
OUT = HERE / "banner.png"
WIDTH, HEIGHT = 2400, 1200

CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "google-chrome",
    "chromium",
]


def find_browser() -> str:
    for candidate in CANDIDATES:
        if Path(candidate).exists():
            return candidate
        found = shutil.which(candidate)
        if found:
            return found
    # Whatever Playwright downloaded, if it is around.
    cache = Path.home() / "Library/Caches/ms-playwright"
    for path in sorted(cache.glob("chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium")):
        return str(path)
    sys.exit("No Chrome/Chromium found. Install one, or add its path to CANDIDATES.")


def main() -> None:
    if not SRC.exists():
        sys.exit(f"{SRC.name} is missing.")

    browser = find_browser()
    subprocess.run(
        [browser, "--headless", "--disable-gpu", "--hide-scrollbars",
         f"--screenshot={OUT}", f"--window-size={WIDTH},{HEIGHT}",
         SRC.as_uri()],
        check=True, capture_output=True,
    )

    if not OUT.exists():
        sys.exit("the browser produced no screenshot")
    print(f"{OUT.name}: {WIDTH}x{HEIGHT}, {OUT.stat().st_size:,} bytes "
          f"(rendered with {Path(browser).name})")


if __name__ == "__main__":
    main()
