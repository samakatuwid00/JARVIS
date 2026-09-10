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
        r"async def (post_apps_toggle|post_apps_rules|post_apps_hide|get_apps"
        r"|post_apps_rules_begin|post_apps_rules_clarify|get_apps_rules_list)\b", src))
    check("get_apps" in names, "get_apps defined in source")
    check("post_apps_toggle" in names, "post_apps_toggle defined in source")
    check("post_apps_rules" in names, "post_apps_rules defined in source")
    check("post_apps_hide" in names, "post_apps_hide defined in source")
    check("post_apps_rules_begin" in names, "post_apps_rules_begin defined in source")
    check("post_apps_rules_clarify" in names, "post_apps_rules_clarify defined in source")
    check("get_apps_rules_list" in names, "get_apps_rules_list defined in source")
    check('"curated"' in src and '"all"' in src, "/apps returns curated + all")


# --------------------------------------------- Phase 4: voice rule authoring --
# rules_voice is pure stdlib + rules_compiler, so it is exercised directly
# against a throwaway registry whether or not jarvis_web imported.
import rules_voice  # noqa: E402

voice_reg = os.path.join(tmpdir, "voice_registry.json")
with open(voice_reg, "w", encoding="utf-8") as f:
    json.dump({"apps": {"spotify": {"name": "Spotify"}}}, f)

# 1) explicit phrase -> proposal, no clarification, nothing committed yet
step = rules_voice.begin_rule_setup("spotify", "no explicit stuff", registry_path=voice_reg)
check(step["status"] == "proposal", "begin_rule_setup: explicit phrase returns a proposal")
check("no explicit stuff" in (step.get("proposal") or ""),
      "proposal text quotes the source phrase")
check(len(step.get("proposed") or []) == 1, "explicit phrase yields one rule")
check(step["proposed"][0]["adapter_check"] == "pre_play: skip if track.explicit",
      "explicit rule carries the pre_play adapter check")
saved = json.load(open(voice_reg, encoding="utf-8"))
check("compiled_rules" not in saved["apps"]["spotify"],
      "begin_rule_setup does not commit — the user owns that")

# confirming a proposal (empty rule_id) commits it
step = rules_voice.handle_clarification("spotify", "", "yes", registry_path=voice_reg)
check(step["status"] == "committed", "confirming a proposal commits it")
saved = json.load(open(voice_reg, encoding="utf-8"))
check(len(saved["apps"]["spotify"].get("compiled_rules") or []) == 1,
      "confirmed proposal reaches the registry")

# 2) ambiguous phrase -> clarification question
step = rules_voice.begin_rule_setup(
    "spotify", "no explicit stuff and keep it quiet", registry_path=voice_reg)
check(step["status"] == "clarify", "begin_rule_setup: ambiguous phrase asks for clarification")
check(step["rule_id"] == "keep_it_quiet", "clarification targets the ambiguous clause")
check("%" in (step.get("question") or ""), "clarification question asks for a volume %")
check(step.get("remaining") == 1, "one open question remains")

# 3) answering the question commits the whole finalized set
step = rules_voice.handle_clarification(
    "spotify", "keep_it_quiet", "under 40% volume all sessions", registry_path=voice_reg)
check(step["status"] == "committed", "handle_clarification commits once nothing is open")
check(step["count"] == 2, "both clauses are committed")
saved = json.load(open(voice_reg, encoding="utf-8"))
committed = saved["apps"]["spotify"]["compiled_rules"]
quiet = [r for r in committed if r["rule_id"] == "keep_it_quiet"][0]
check(quiet["adapter_check"] == "pre_play: cap spotify volume at 40%",
      "the answer is compiled into the adapter check")
check(quiet["scope"] == "all_sessions", "the answer sets the rule scope")
check(quiet["needs_clarification"] is False, "committed rule is no longer pending")
check(rules_voice.pending_setup("spotify") is None, "pending state is cleared after commit")

# 4) error paths
check(rules_voice.handle_clarification("spotify", "x", "y", registry_path=voice_reg)["error"]
      == "no rule setup in progress", "clarify without a setup is rejected")
rules_voice.begin_rule_setup("spotify", "keep it quiet", registry_path=voice_reg)
check(rules_voice.handle_clarification("spotify", "nope_rule", "x",
                                       registry_path=voice_reg)["error"] == "unknown rule_id",
      "clarify rejects an unknown rule_id")
check(rules_voice.handle_clarification("spotify", "keep_it_quiet", "cancel",
                                       registry_path=voice_reg)["status"] == "cancelled",
      "a negative answer cancels the setup")
check(rules_voice.begin_rule_setup("spotify", "", registry_path=voice_reg)["status"] == "error",
      "begin_rule_setup rejects an empty phrase")

# hard escalation survives the voice path
rules_voice.begin_rule_setup("spotify", "never play explicit tracks even if I ask",
                             registry_path=voice_reg)
step = rules_voice.handle_clarification("spotify", "", "yes", registry_path=voice_reg)
check(step["rules"][0]["enforcement"] == "hard",
      "'never … even if I ask' is escalated to hard enforcement")

# 5) list_rules
out = rules_voice.list_rules("spotify", registry_path=voice_reg)
check(out["status"] == "ok" and out["count"] == 1, "list_rules reports the committed rules")
check("Rules for spotify" in out["summary"], "list_rules summary is human-readable")
check("hard" in out["summary"], "list_rules summary shows enforcement")
out = rules_voice.list_rules("chrome", registry_path=voice_reg)
check(out["count"] == 0 and "No rules set" in out["summary"],
      "list_rules handles an app with no rules")

if live:
    # the endpoints drive the same flow through the live registry
    res = body_of(run(jarvis_web.post_apps_rules_begin(
        FakeRequest({"app_key": "spotify", "phrase": "no explicit stuff and keep it quiet"}))))
    check(res.get("status") == "clarify", "/apps/rules/begin returns a clarification")
    rid = res.get("rule_id")
    res = body_of(run(jarvis_web.post_apps_rules_clarify(
        FakeRequest({"app_key": "spotify", "rule_id": rid,
                     "answer": "under 25% volume all sessions"}))))
    check(res.get("status") == "committed", "/apps/rules/clarify commits the rule set")
    saved = json.load(open(reg_path, encoding="utf-8"))
    check(any("25%" in (r.get("adapter_check") or "")
              for r in saved["apps"]["spotify"]["compiled_rules"]),
          "/apps/rules/clarify persists to the registry")

    res = body_of(run(jarvis_web.get_apps_rules_list(key="spotify")))
    check(res.get("ok") and res.get("count") == 2, "/apps/rules/list returns the rules")
    check("Rules for spotify" in (res.get("summary") or ""),
          "/apps/rules/list returns a readable summary")

    res = body_of(run(jarvis_web.get_apps_rules_list(key="")))
    check(res.get("error") == "missing key", "/apps/rules/list rejects a missing key")
    res = body_of(run(jarvis_web.post_apps_rules_begin(
        FakeRequest({"app_key": "nope", "phrase": "no explicit stuff"}))))
    check(res.get("error") == "unknown app", "/apps/rules/begin rejects an unknown app")
    res = body_of(run(jarvis_web.post_apps_rules_begin(
        FakeRequest({"app_key": "spotify", "phrase": ""}))))
    check(res.get("error") == "missing phrase", "/apps/rules/begin rejects an empty phrase")


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
    # Phase 4 voice section
    check("Voice Rule Setup" in html, "panel has the Voice Rule Setup section")
    check("Current Rules" in html, "panel shows a Current Rules list")
    check(">Propose Rules<" in html, "panel offers a Propose Rules button")
    check("/apps/rules/begin" in html, "panel references /apps/rules/begin")
    check("/apps/rules/clarify" in html, "panel references /apps/rules/clarify")
    check("/apps/rules/list" in html, "panel references /apps/rules/list")
    check('class="v-question question"' in html, "panel renders the clarification question")
    check('class="v-answer"' in html, "panel has a clarification answer input")
    check(">Confirm<" in html, "panel offers a Confirm button")


if missing:
    print("\nFAILED checks:", missing)
    sys.exit(1)
print("\nALL CHECKS PASSED")
