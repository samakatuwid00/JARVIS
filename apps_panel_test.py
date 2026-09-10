"""Apps panel checks: /apps payload shape, curation filtering, toggle, hide.

Runs without a live server: the FastAPI handlers are called directly against a
temporary registry, and the panel HTML is checked as source.
"""
import asyncio
import json
import os
import re
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "jarvis_web.py")
PANEL = os.path.join(HERE, "apps_panel.html")
CURATE = os.path.join(HERE, "curate.py")

missing = []


def check(cond, label):
    if not cond:
        missing.append(label)
        print("FAIL:", label)
    else:
        print("ok:", label)


# ---------------------------------------------------------------- curate.py --
import curate  # noqa: E402  (pure stdlib, safe to import)

check(
    {"spotify", "chrome", "obsidian", "vlc", "dbeaver", "omniroute"}
    <= curate.SEED_REGISTERED,
    "SEED_REGISTERED covers the verified app set",
)
check(len(curate.SEED_REGISTERED) >= 20, "SEED_REGISTERED has ~20 apps")

for noisy in ("asusfeatureservice", "glidexservice", "aslogdumptool2",
              "epsonscan2", "es2launcher", "e_yrgcae", "docker-ai",
              "officeclicktorun", "appvcleaner"):
    check(curate.is_noise(noisy), "is_noise rejects %s" % noisy)

for good in ("spotify", "chrome", "obsidian", "code", "excel"):
    check(not curate.is_noise(good), "is_noise keeps %s" % good)


# ------------------------------------------------------- registry fixtures --
FIXTURE = {
    "generated": "test",
    "apps": {
        "spotify": {"name": "Spotify", "bin": "", "category": "media"},
        "chrome": {"name": "Chrome", "bin": "", "category": "browser"},
        "randomtool": {"name": "Random Tool", "bin": "", "category": "other"},
        "asusfeatureservice": {"name": "ASUS Feature Service", "bin": ""},
        "epsonscan2": {"name": "Epson Scan 2", "bin": ""},
        "docker-ai": {"name": "Docker AI", "bin": ""},
    },
}


def write_fixture(path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(FIXTURE, f)


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def body_of(response):
    return json.loads(response.body.decode("utf-8"))


class FakeRequest:
    """Minimal stand-in for starlette Request — the handlers only call .json()."""

    def __init__(self, payload):
        self._payload = payload

    async def json(self):
        return self._payload


tmpdir = tempfile.mkdtemp(prefix="apps_panel_test_")
reg_path = os.path.join(tmpdir, "app_registry.json")
write_fixture(reg_path)

try:
    import machine_capabilities
    import jarvis_web

    machine_capabilities.REGISTRY_PATH = reg_path
    live = True
except Exception as e:  # heavy deps missing on this box
    print("note: import jarvis_web failed (%s) — falling back to source grep" % e)
    live = False


if live:
    payload = body_of(run(jarvis_web.get_apps()))

    # 1) payload structure
    for field in ("curated", "all", "curated_count", "all_count", "hidden_count"):
        check(field in payload, "/apps payload has %s" % field)
    check(payload["curated_count"] == len(payload["curated"]), "curated_count matches list")
    check(payload["all_count"] == len(payload["all"]), "all_count matches list")

    # 2) curation filtering: seeds curated, noise dropped from both lists
    curated_keys = {a["key"] for a in payload["curated"]}
    all_keys = {a["key"] for a in payload["all"]}
    check({"spotify", "chrome"} <= curated_keys, "curated holds the seeded apps")
    check("randomtool" in all_keys, "raw list keeps unregistered detected apps")
    check("randomtool" not in curated_keys, "unregistered app is not curated")
    for noisy in ("asusfeatureservice", "epsonscan2", "docker-ai"):
        check(noisy not in all_keys, "noise %s hidden from raw list" % noisy)
    check(payload["hidden_count"] == 3, "hidden_count counts the noise entries")
    check(all(a["registered"] for a in payload["curated"]), "curated entries flagged registered")
    check(not any(a["hidden"] for a in payload["all"]), "raw list carries no hidden entries")

    # 3) toggle endpoint
    res = body_of(run(
        jarvis_web.post_apps_toggle(FakeRequest({"key": "spotify", "enabled": False}))))
    check(res.get("ok") and res.get("enabled") is False, "/apps/toggle returns ok")
    saved = json.load(open(reg_path, encoding="utf-8"))
    check(saved["apps"]["spotify"]["enabled"] is False, "/apps/toggle persists enabled=False")

    res = body_of(run(
        jarvis_web.post_apps_toggle(FakeRequest({"key": "nope", "enabled": True}))))
    check(res.get("error") == "unknown app", "/apps/toggle rejects unknown key")

    # 4) hide endpoint
    res = body_of(run(
        jarvis_web.post_apps_hide(FakeRequest({"key": "randomtool", "hidden": True}))))
    check(res.get("ok") and res.get("hidden") is True, "/apps/hide returns ok")
    saved = json.load(open(reg_path, encoding="utf-8"))
    check(saved["apps"]["randomtool"]["hidden"] is True, "/apps/hide persists hidden=True")

    payload = body_of(run(jarvis_web.get_apps()))
    check("randomtool" not in {a["key"] for a in payload["all"]},
          "hidden app drops out of the raw list")

    # un-hiding works, including for a noise-family key
    run(
        jarvis_web.post_apps_hide(FakeRequest({"key": "randomtool", "hidden": False})))
    run(
        jarvis_web.post_apps_hide(FakeRequest({"key": "epsonscan2", "hidden": False})))
    payload = body_of(run(jarvis_web.get_apps()))
    raw_keys = {a["key"] for a in payload["all"]}
    check("randomtool" in raw_keys, "/apps/hide un-hides an app")
    check("epsonscan2" in raw_keys, "explicit hidden=False rescues a noise-family app")

    res = body_of(run(
        jarvis_web.post_apps_hide(FakeRequest({"key": "nope", "hidden": True}))))
    check(res.get("error") == "unknown app", "/apps/hide rejects unknown key")
else:
    with open(WEB, encoding="utf-8") as f:
        src = f.read()
    names = set(re.findall(
        r"async def (post_apps_toggle|post_apps_rules|post_apps_hide|get_apps)\b", src))
    check("get_apps" in names, "get_apps defined in source")
    check("post_apps_toggle" in names, "post_apps_toggle defined in source")
    check("post_apps_rules" in names, "post_apps_rules defined in source")
    check("post_apps_hide" in names, "post_apps_hide defined in source")
    check('"curated"' in src and '"all"' in src, "/apps returns curated + all")


# ------------------------------------------------------------- panel HTML --
check(os.path.exists(PANEL), "apps_panel.html exists")
if os.path.exists(PANEL):
    html = open(PANEL, encoding="utf-8").read()
    check(
        ("fetch(\"/apps\")" in html) or ("GET /apps" in html) or ("\"/apps\"" in html),
        "panel references /apps"
    )
    check("/apps/toggle" in html, "panel references /apps/toggle")
    check("/apps/hide" in html, "panel references /apps/hide")
    check("Curated Apps" in html, "panel has the curated tab")
    check("All Detected (Raw)" in html, "panel has the raw tab")
    check('id="count-curated"' in html and 'id="count-all"' in html,
          "panel has a count badge on each tab")
    check('id="tab-curated" class="tab active"' in html, "curated tab is active by default")
    check(">Hide<" in html, "panel raw tab offers a Hide button")


if missing:
    print("\nFAILED checks:", missing)
    sys.exit(1)
print("\nALL CHECKS PASSED")
