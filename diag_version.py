"""Read chrome://version from the given port."""
import sys
sys.path.insert(0, r"C:\Users\deped\Documents\jarvis-demo")
from playwright.sync_api import sync_playwright

port = sys.argv[1] if len(sys.argv) > 1 else "9226"
with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", timeout=5000)
    ctx = b.contexts[0]
    p = ctx.new_page()
    p.goto("chrome://version", wait_until="domcontentloaded", timeout=15000)
    p.wait_for_timeout(800)
    txt = p.locator("body").inner_text()
    for line in txt.splitlines():
        ls = line.strip()
        if "Profile Path" in ls or ("Command Line" in ls and "user-data" in ls):
            print(ls[:200])
    p.close()
    b.close()
