"""verify_hermes_bridge.py — non-disruptive proof the delegate_to_hermes tool
is wired correctly and enforces the read+report scope.

Run from the Hermes terminal (no servers needed):
    python scripts/verify_hermes_bridge.py

Checks:
  1. Tool is declared in all 4 touchpoints: tools.TOOLS, tools.TOOL_MAP,
     brain_gemini.TOOL_DECLARATIONS, brain_gemini._openai_tools().
  2. A benign READ task actually reaches Hermes and returns a real reply.
  3. A DESTRUCTIVE task is refused with [NEEDS_CONFIRM] before Hermes is ever launched.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

passed = 0
failed = 0


def check(name, cond, detail=""):
    global passed, failed
    if cond:
        passed += 1
        print(f"[PASS] {name}")
    else:
        failed += 1
        print(f"[FAIL] {name}  {detail}")


print("=== 1. Wiring across all touchpoints ===")
import tools
import brain_gemini

# Aug 2026: the tool surface was unified — the ONE delegation primitive exposed
# to the brain is `delegate` (auto-routes music/desktop/web/specialist/hermes).
# delegate_to_hermes remains as the underlying executor, called by delegate().
TOOL_NAME = "delegate"

in_tools = any(t.get("name") == TOOL_NAME for t in tools.TOOLS)
check(f"tools.TOOLS has {TOOL_NAME}", in_tools)

check(f"tools.TOOL_MAP has {TOOL_NAME}", TOOL_NAME in tools.TOOL_MAP)
check("delegate_to_hermes executor retained in tools",
      hasattr(tools, "delegate_to_hermes") and hasattr(tools, "delegate"))

in_decl = any(getattr(fc, "name", None) == TOOL_NAME
              for fc in brain_gemini.TOOL_DECLARATIONS)
check(f"brain_gemini.TOOL_DECLARATIONS has {TOOL_NAME}", in_decl)

in_openai = any(f.get("function", {}).get("name") == TOOL_NAME
                for f in brain_gemini._openai_tools())
check(f"brain_gemini._openai_tools() has {TOOL_NAME}", in_openai)


print("\n=== 2. Destructive task is REFUSED before launch (no Hermes call) ===")
blocked = tools.delegate_to_hermes("delete all my files in Documents")
check("destructive task returns [NEEDS_CONFIRM]", "[NEEDS_CONFIRM]" in blocked, repr(blocked))

blocked2 = tools.delegate_to_hermes("install a package with pip install requests")
check("install task returns [NEEDS_CONFIRM]", "[NEEDS_CONFIRM]" in blocked2, repr(blocked2))

empty = tools.delegate_to_hermes("")
check("empty task returns [Error]", "[Error]" in empty, repr(empty))


print("\n=== 3. Benign READ task actually reaches Hermes ===")
# This is the only network/process step; Hermes cold-start is ~2min on this box.
reply = tools.delegate_to_hermes(
    "Reply with exactly the single word: BRIDGE_OK", timeout=200, max_turns=15)
check("read task returns a real Hermes reply", "BRIDGE_OK" in reply, repr(reply))
print(f"       reply: {reply!r}")


print(f"\n=== RESULT: {passed} passed, {failed} failed ===")
sys.exit(1 if failed else 0)
