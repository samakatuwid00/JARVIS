"""Capture the live JARVIS wrapper page and dump the rendered layout boxes.

Run with the server already up on :8000:
    python capture_page.py
Writes page_capture.png (full page) and prints the getBoundingClientRect of the
HUD iframe, the chat panel and its parts as JSON.
"""
import json
import sys

from playwright.sync_api import sync_playwright

URL = "http://localhost:8000"
OUT = "page_capture.png"
BOOT_MS = 3500

BOXES_JS = """() => {
  const pick = sel => {
    const el = document.querySelector(sel);
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return {x: r.x, y: r.y, width: r.width, height: r.height,
            top: r.top, bottom: r.bottom, left: r.left, right: r.right};
  };
  return {
    'iframe#hud': pick('iframe#hud'),
    '#jv-panel': pick('#jv-panel'),
    '#jv-log':   pick('#jv-log'),
    '#jv-input': pick('#jv-input'),
    '#jv-form':  pick('#jv-form'),
    'body':      pick('body'),
    viewport: {w: innerWidth, h: innerHeight},
    scroll:   {w: document.documentElement.scrollWidth,
               h: document.documentElement.scrollHeight}
  };
}"""


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
        )
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.goto(URL, wait_until="load")
        page.wait_for_timeout(BOOT_MS)

        page.screenshot(path=OUT, full_page=True)
        boxes = page.evaluate(BOXES_JS)
        boxes["pageerrors"] = errors
        print(json.dumps(boxes, indent=2))

        hud = boxes.get("iframe#hud") or {}
        panel = boxes.get("#jv-panel") or {}
        if hud and panel:
            gap = panel["top"] - hud["bottom"]
            print(f"\n# hud.bottom={hud['bottom']:.1f}  panel.top={panel['top']:.1f}  gap={gap:.1f}px",
                  file=sys.stderr)
        browser.close()


if __name__ == "__main__":
    main()
