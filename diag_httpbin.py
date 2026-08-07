"""Echo what cookies this Chrome instance really sends."""
import sys
sys.path.insert(0, r"C:\Users\deped\Documents\jarvis-demo")
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp("http://127.0.0.1:9223", timeout=5000)
    ctx = b.contexts[0]
    page = ctx.new_page()
    page.goto("https://httpbin.org/cookies", wait_until="domcontentloaded", timeout=30000)
    page.wait_for_timeout(1500)
    body = page.locator("body").inner_text()
    print("HTTPBIN ECHO:", body.strip()[:400])
    page.close()
    # Also: which profile is this? visit chrome://version and read Profile Path
    p2 = ctx.new_page()
    p2.goto("chrome://version", wait_until="domcontentloaded", timeout=15000)
    txt = p2.locator("body").inner_text()
    for line in txt.splitlines():
        if "Profile Path" in line or "Command Line" in line:
            print("CHROME://VERSION:", line.strip()[:200])
    p2.close()
    b.close()
