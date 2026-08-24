"""Watch jarvis-demo .py files and auto-restart jarvis_web.py on changes.
Usage: python watch_and_restart.py
Polls every 2s. Kills old process, starts new one. Ctrl+C to stop."""
import os, sys, time, subprocess, signal, glob

ROOT = os.path.dirname(os.path.abspath(__file__))
WATCH = [os.path.join(ROOT, f) for f in ("brain_gemini.py", "tools.py", "music_agent.py",
                                           "voice_engine.py", "wake_engine.py", "config.py",
                                           "session_store.py", "browser_agent.py",
                                           "project_agent.py", "report_agent.py",
                                           "warm_harness.py", "jarvis_visual.html")]
WEB = os.path.join(ROOT, "jarvis_web.py")
POLL = 2  # seconds

mtimes = {f: os.path.getmtime(f) for f in WATCH if os.path.exists(f)}
proc = None

def start():
    global proc
    print(f"[watcher] Starting jarvis_web.py ...", flush=True)
    proc = subprocess.Popen([sys.executable, WEB], cwd=ROOT)
    print(f"[watcher] PID {proc.pid}", flush=True)

def stop():
    global proc
    if proc and proc.poll() is None:
        print(f"[watcher] Stopping PID {proc.pid} ...", flush=True)
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        print(f"[watcher] Stopped.", flush=True)
    proc = None

def check():
    changed = False
    for f in WATCH:
        if not os.path.exists(f):
            continue
        t = os.path.getmtime(f)
        if t != mtimes.get(f):
            print(f"[watcher] Changed: {os.path.basename(f)}", flush=True)
            mtimes[f] = t
            changed = True
    return changed

signal.signal(signal.SIGINT, lambda *_: (stop(), sys.exit(0)))
signal.signal(signal.SIGTERM, lambda *_: (stop(), sys.exit(0)))

start()
print(f"[watcher] Watching {len(WATCH)} files, poll={POLL}s. Ctrl+C to stop.", flush=True)

while True:
    time.sleep(POLL)
    if proc and proc.poll() is not None:
        print(f"[watcher] Process exited (code={proc.returncode}), restarting ...", flush=True)
        start()
    elif check():
        stop()
        start()
