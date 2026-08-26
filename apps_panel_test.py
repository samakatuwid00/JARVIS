import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "jarvis_web.py")
PANEL = os.path.join(HERE, "apps_panel.html")

missing = []


def check(cond, label):
    if not cond:
        missing.append(label)
        print("FAIL:", label)
    else:
        print("ok:", label)


# 1) Endpoint functions exist & callable (or defined in source).
def get_route_names(src):
    return set(re.findall(r"async def (post_apps_toggle|post_apps_rules|get_apps)\b", src))


try:
    import jarvis_web  # may fail on heavy deps

    check(callable(getattr(jarvis_web, "get_apps", None)), "get_apps callable")
    check(callable(getattr(jarvis_web, "post_apps_toggle", None)), "post_apps_toggle callable")
    check(callable(getattr(jarvis_web, "post_apps_rules", None)), "post_apps_rules callable")
except Exception as e:
    print("note: import jarvis_web failed (%s) — falling back to source grep" % e)
    with open(WEB, encoding="utf-8") as f:
        src = f.read()
    names = get_route_names(src)
    check("get_apps" in names, "get_apps defined in source")
    check("post_apps_toggle" in names, "post_apps_toggle defined in source")
    check("post_apps_rules" in names, "post_apps_rules defined in source")


# 2) Panel HTML exists and references the right endpoints.
check(os.path.exists(PANEL), "apps_panel.html exists")
if os.path.exists(PANEL):
    html = open(PANEL, encoding="utf-8").read()
    check(
        ("fetch(\"/apps\")" in html) or ("GET /apps" in html) or ("\"/apps\"" in html),
        "panel references /apps"
    )
    check("/apps/toggle" in html, "panel references /apps/toggle")


if missing:
    print("\nFAILED checks:", missing)
    sys.exit(1)
print("\nALL CHECKS PASSED")
