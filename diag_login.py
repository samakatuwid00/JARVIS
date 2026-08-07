"""Diagnostic: attach to JARVIS Chrome, check what chatgpt.com shows."""
import sys
sys.path.insert(0, r"C:\Users\deped\Documents\jarvis-demo")
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    b = pw.chromium.connect_over_cdp("http://127.0.0.1:9223", timeout=5000)
    ctx = b.contexts[0]
    page = None
    for p in ctx.pages:
        if "chatgpt.com" in p.url:
            page = p
            break
    if page is None:
        page = ctx.new_page()
        page.goto("https://chatgpt.com/", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(4000)
    title = page.title()
    url = page.url
    login_btn = page.locator("button[data-testid='login-button']").count()
    login_link = page.locator("a[href*='/auth/login']").count()
    composer = page.locator("#prompt-textarea, div[contenteditable='true']").count()
    body_snippet = page.locator("body").inner_text()[:200].replace("\n", " | ")
    print(f"URL: {url}")
    print(f"TITLE: {title}")
    print(f"login-button: {login_btn}  auth/login link: {login_link}  composer: {composer}")
    print(f"BODY: {body_snippet}")
    b.close()
