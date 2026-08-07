#!/usr/bin/env python3
"""Verification script for Jarvis demo."""
import sys
import os

# Add project to path
sys.path.insert(0, "C:/Users/deped/Documents/jarvis-demo")

results = []

def test(name, fn):
    try:
        ok = fn()
        results.append((name, ok, ""))
    except Exception as e:
        results.append((name, False, str(e)))

# Test 1: imports
def test_imports():
    import tools
    import brain_gemini
    import voice_engine
    import config
    return True
test("All modules import", test_imports)

# Test 2: tools registered
def test_tools():
    from tools import TOOLS, TOOL_MAP
    assert len(TOOLS) == 8, f"Expected 8 tools, got {len(TOOLS)}"
    assert len(TOOL_MAP) == 8, f"Expected 8 tools in map"
    tool_names = {t["name"] for t in TOOLS}
    assert tool_names == set(TOOL_MAP.keys()), "TOOLS/TOOL_MAP mismatch"
    return True
test("8 tools registered correctly", test_tools)

# Test 3: run_shell
def test_shell():
    from tools import run_shell
    out = run_shell("echo hello")
    assert "hello" in out
    return True
test("run_shell executes", test_shell)

# Test 4: list_directory
def test_ls():
    from tools import list_directory
    out = list_directory("C:/Users/deped/Documents/jarvis-demo")
    assert "jarvis.py" in out
    assert "brain_gemini.py" in out
    return True
test("list_directory works", test_ls)

# Test 5: read/write file
def test_file_io():
    from tools import write_file, read_file
    tp = "C:/Users/deped/Documents/jarvis-demo/_verify_test.txt"
    write_file(tp, "test content 12345")
    content = read_file(tp)
    assert "test content 12345" in content
    os.unlink(tp)
    return True
test("read_file/write_file roundtrip", test_file_io)

# Test 6: get_system_info
def test_sysinfo():
    from tools import get_system_info
    info = get_system_info()
    assert "os" in info
    assert "python" in info
    return True
test("get_system_info returns dict", test_sysinfo)

# Test 7: execute_tool dispatcher
def test_dispatcher():
    from tools import execute_tool
    out = execute_tool("run_shell", {"command": "echo dispatched"})
    assert "dispatched" in out
    out2 = execute_tool("nonexistent_tool", {})
    assert "Error" in out2
    return True
test("execute_tool dispatcher", test_dispatcher)

# Test 8: demo mode runs
def test_demo():
    import subprocess
    r = subprocess.run(
        ["python", "jarvis.py", "--demo"],
        capture_output=True, text=True, timeout=15,
        cwd="C:/Users/deped/Documents/jarvis-demo"
    )
    assert r.returncode == 0, f"Exit {r.returncode}: {r.stderr[:200]}"
    assert "JARVIS" in r.stdout
    assert "Jarvis" in r.stdout
    return True
test("demo mode runs clean", test_demo)

# Test 9: voice_engine imports with text fallback
def test_text_interface():
    from voice_engine import TextInterface
    ti = TextInterface()
    assert hasattr(ti, "speak")
    assert hasattr(ti, "listen_for_command")
    return True
test("TextInterface fallback works", test_text_interface)

# Test 10: config has all keys
def test_config():
    from config import GEMINI_API_KEY, GEMINI_MODEL, WHISPER_MODEL, TTS_VOICE
    assert GEMINI_MODEL in ("gemini-1.5-flash", "gemini-2.0-flash", "gemini-2.0-flash-lite")
    assert WHISPER_MODEL == "tiny"
    return True
test("config values set", test_config)

# Test 11: brain_gemini initializes (with mock fallback)
def test_brain():
    import os
    # Never hardcode a real key in source. Use a dummy for the offline init check,
    # or the real one only from the environment.
    os.environ.setdefault("GEMINI_API_KEY", "dummy-key-for-offline-init-check")
    from brain_gemini import JarvisBrain
    b = JarvisBrain()
    assert hasattr(b, 'think')
    assert hasattr(b, 'reset')
    resp = b.think("What is 2+2?")
    assert isinstance(resp, str) and len(resp) > 0
    return True
test("JarvisBrain initializes and thinks", test_brain)

# Report
passed = sum(1 for _, ok, _ in results if ok)
failed = sum(1 for _, ok, _ in results if not ok)

print(f"\n=== JARVIS VERIFICATION: {passed}/{len(results)} passed ===\n")
for name, ok, err in results:
    icon = "✅" if ok else "❌"
    print(f"  {icon} {name}")
    if err:
        print(f"      {err[:120]}")

if failed:
    print(f"\n{failed} FAILED")
    sys.exit(1)
else:
    print("\nAll tests passed.")
    sys.exit(0)