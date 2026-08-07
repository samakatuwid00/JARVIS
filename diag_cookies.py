"""Check live cookie jar + profile of the 9223 Chrome instance."""
import sys
sys.path.insert(0, r"C:\Users\deped\Documents\jarvis-demo")
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp("http://127.0.0.1:9223", timeout=5000)
    ctx = b.contexts[0]
    cookies = ctx.cookies()
    cgpt = [c for c in cookies if "chatgpt" in c["domain"] or "openai" in c["domain"]]
    names = sorted({c["name"] for c in cgpt})
    print("LIVE cookies on chatgpt/openai domains:", names or "NONE")
    toks = [c["name"] for c in cgpt if "session" in c["name"].lower() or "token" in c["name"].lower()]
    print("Session/token cookies:", toks or "NONE")
    for p in ctx.pages:
        if p.url:
            print("tab:", p.url[:80])
    b.close()
