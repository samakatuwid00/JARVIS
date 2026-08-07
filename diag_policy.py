"""Dump chrome://policy of the 9224 (Profile 7) instance."""
import sys
sys.path.insert(0, r"C:\Users\deped\Documents\jarvis-demo")
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp("http://127.0.0.1:9224", timeout=5000)
    ctx = b.contexts[0]
    p = ctx.new_page()
    p.goto("chrome://policy", wait_until="domcontentloaded", timeout=15000)
    p.wait_for_timeout(1500)
    txt = p.locator("body").inner_text()
    print(txt[:1500])
    p.close()
    b.close()
