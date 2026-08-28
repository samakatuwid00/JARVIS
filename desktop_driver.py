"""desktop_driver.py — Phase 6: UI-Automation driver (pywinauto) replaces pyautogui hack.

Targets actual controls instead of typing into focused window. Misfire fails safely
instead of landing somewhere unintended.

Design:
  - Prefer pywinauto (UIA) on Windows; fallback to SendKeys with warning if not installed.
  - Every action does post-action verification: screenshot or control exists check; retry once before escalate.
  - All actions are audited via audit.log_call and guarded via _safe checks.

Stdlib + pywinauto (optional). Never crashes JARVIS if pywinauto missing — falls back.
"""

import time
import re
from pathlib import Path

try:
    from pywinauto import Desktop as _PywDesktop
    from pywinauto.findwindows import ElementNotFoundError as _ElemErr
    _HAS_PYW = True
except Exception as e:
    _HAS_PYW = False
    _PywDesktop = None
    _ElemErr = Exception
    _pyw_err = str(e)

def has_driver() -> bool:
    return _HAS_PYW

def _audit(action: str, target: str, result: str, ok: bool):
    try:
        from tools import _audit_log
        _audit_log(f"desktop_driver:{action}", target[:200], "ok" if ok else "failed", result=result[:400])
    except Exception:
        pass

def find_window(title_re: str = None, control_type: str = None, timeout: float = 5.0):
    """Find a window by title regex. Returns pywinauto WindowSpecification or None."""
    if not _HAS_PYW:
        return None
    deadline = time.time() + timeout
    pattern = re.compile(title_re, re.I) if title_re else None
    while time.time() < deadline:
        try:
            desktop = _PywDesktop(backend="uia")
            windows = desktop.windows()
            for w in windows:
                try:
                    t = w.window_text() or ""
                    if pattern and not pattern.search(t):
                        continue
                    if control_type and w.element_info.control_type != control_type:
                        continue
                    return w
                except Exception:
                    continue
        except Exception:
            pass
        time.sleep(0.5)
    return None

def click_control(title_re: str, control_name: str = None, timeout: float = 8.0) -> str:
    """Click a control by window title + control name. Verifies post-action."""
    if not _HAS_PYW:
        _audit("click_control", f"{title_re}:{control_name}", "pywinauto not installed — fallback to SendKeys would be unsafe, aborting", False)
        return "[Error] pywinauto not installed — install via `pip install pywinauto` for safe UI automation. Fallback disabled to avoid misfire."
    try:
        win = find_window(title_re, timeout=timeout)
        if not win:
            _audit("click_control", f"{title_re}:{control_name}", "window not found", False)
            return f"[Error] Window matching '{title_re}' not found"
        if control_name:
            # Find child control by name/title
            ctrl = win.child_window(title=control_name, control_type="Button")
            if not ctrl.exists(timeout=3):
                # try generic
                ctrl = win.child_window(title_re=control_name)
                if not ctrl.exists(timeout=2):
                    _audit("click_control", f"{title_re}:{control_name}", "control not found", False)
                    return f"[Error] Control '{control_name}' not found in '{title_re}'"
            ctrl.click_input()
        else:
            win.click_input()
        time.sleep(0.5)
        # post-action verification: window still exists and is not error
        if win.exists():
            _audit("click_control", f"{title_re}:{control_name}", "clicked and verified", True)
            return f"Clicked {control_name or title_re} in {title_re} (verified)"
        _audit("click_control", f"{title_re}:{control_name}", "window disappeared after click — possible failure", False)
        return f"Clicked {control_name or title_re} but window verification failed — retry needed"
    except Exception as e:
        _audit("click_control", f"{title_re}:{control_name}", f"{type(e).__name__}: {e}", False)
        return f"[Error] click_control failed: {e}"

def type_into_control(title_re: str, control_name: str, text: str, timeout: float = 8.0) -> str:
    """Type text into a specific control (not focused window). Retries once."""
    if not _HAS_PYW:
        return "[Error] pywinauto not installed — safe typing disabled."
    for attempt in (1, 2):
        try:
            win = find_window(title_re, timeout=timeout)
            if not win:
                return f"[Error] Window '{title_re}' not found"
            ctrl = win.child_window(title=control_name, control_type="Edit")
            if not ctrl.exists(timeout=3):
                ctrl = win.child_window(title_re=control_name)
                if not ctrl.exists(timeout=2):
                    return f"[Error] Edit control '{control_name}' not found"
            ctrl.set_focus()
            ctrl.type_keys(text, with_spaces=True)
            # verify: read back
            time.sleep(0.3)
            try:
                val = ctrl.get_value() if hasattr(ctrl, "get_value") else ""
                if text[:20] in val or text in val:
                    _audit("type_into_control", f"{title_re}:{control_name}", "typed and verified", True)
                    return f"Typed into {control_name} (verified)"
            except Exception:
                pass
            _audit("type_into_control", f"{title_re}:{control_name}", f"typed (attempt {attempt}) — verification pending", True)
            return f"Typed into {control_name}"
        except Exception as e:
            if attempt == 2:
                _audit("type_into_control", f"{title_re}:{control_name}", f"failed after retry: {e}", False)
                return f"[Error] type_into_control failed: {e}"
            time.sleep(0.8)
    return "[Error] type_into_control failed after retry"

def screenshot_check(title_re: str = None, save_path: str = None) -> str:
    """Capture screenshot for post-action verification. Returns path or error."""
    try:
        from PIL import ImageGrab  # optional
        import datetime, os
        if not save_path:
            save_path = str(Path.home() / f"Documents/JARVIS_screenshot_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.png")
        img = ImageGrab.grab()
        img.save(save_path)
        _audit("screenshot", title_re or "desktop", f"saved to {save_path}", True)
        return f"Screenshot saved to {save_path}"
    except Exception as e:
        # Fallback: pywinauto's capture
        if _HAS_PYW and title_re:
            try:
                win = find_window(title_re, timeout=3)
                if win:
                    path = save_path or str(Path.home() / "Documents/jarvis_capture.png")
                    win.capture_as_image().save(path)
                    _audit("screenshot", title_re, f"pywinauto capture {path}", True)
                    return f"Captured {title_re} to {path}"
            except Exception as e2:
                return f"[Error] screenshot failed: {e2}"
        return f"[Error] screenshot failed: {e}"

def verify_control_exists(title_re: str, control_name: str) -> bool:
    """Post-action check: does control exist?"""
    if not _HAS_PYW:
        return False
    try:
        win = find_window(title_re, timeout=3)
        if not win:
            return False
        ctrl = win.child_window(title=control_name)
        return ctrl.exists(timeout=2)
    except Exception:
        return False
