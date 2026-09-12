"""The four fixes from the 2026-09-12 conversation tests:
1. one time limit per turn across the model chain, and an honest reply
2. a question is answered, not handed to an agent
3. questions about past work are grounded in the jobs and files they name
4. each client keeps its own conversation"""

import datetime
import json
import time

import pytest

import brain_gemini as b
import jobs
import tools


# ------------------------------------------------ 3: grounded own-work --

def test_the_words_a_question_shares_with_its_job():
    assert jobs.content_words("What coding agent did you use to create the hello world website?") == \
        {"hello", "world", "website"}


@pytest.fixture
def job_log(monkeypatch):
    rows = {"a1": {"id": "a1", "task": "Can you create a Hello World website with open code?",
                   "tier": "autonomous", "state": "unverified", "started": 100.0,
                   "progress": ["[STEP 2] ✓ Dispatched OpenCode CLI"]},
            "b2": {"id": "b2", "task": "read jarvis_hud_v3.html and find the chat CSS", "tier": "hermes",
                   "state": "done", "started": 900.0, "progress": []},
            "c3": {"id": "c3", "task": "can you visit websites?", "tier": "hermes", "state": "done",
                   "started": 800.0, "progress": []}}
    monkeypatch.setattr(jobs, "_jobs", rows)
    monkeypatch.setattr(jobs, "_loaded", True)
    return rows


def test_a_morning_job_is_found_by_what_it_built(job_log):
    found = jobs.find_work("What coding agent did you use to create the hello world website?")
    assert [j["id"] for j in found] == ["a1"]
    assert jobs.find_work("what did you do?") == []                  # nothing named, nothing matched


def test_recent_work_leaves_out_what_was_already_shown(job_log):
    assert "a1" not in jobs.recent_work(exclude={"a1"}) and "b2" in jobs.recent_work(exclude={"a1"})


def test_file_writes_are_found_by_name_however_old(tmp_path, monkeypatch):
    old = (datetime.datetime.now() - datetime.timedelta(hours=9)).isoformat()
    audit = tmp_path / "audit.jsonl"
    audit.write_text("\n".join(json.dumps(r) for r in [
        {"ts": old, "caller": "write_file", "decision": "executed",
         "task": "C:\\Users\\x\\Documents\\hello-world-website\\index.html"},
        {"ts": old, "caller": "write_file", "decision": "executed", "task": "C:\\notes.txt"}]),
        encoding="utf-8")
    monkeypatch.setattr(tools, "_AUDIT_PATH", audit)
    assert tools.recent_file_writes() == []                                   # older than 2 h
    found = tools.recent_file_writes(match=["hello", "world", "website"])
    assert len(found) == 1 and found[0].endswith("hello-world-website\\index.html")


def test_the_facts_lead_with_the_job_the_question_names(job_log, monkeypatch):
    monkeypatch.setattr(tools, "recent_file_writes", lambda *a, **k: [])
    facts = b._own_work_facts("What coding agent did you use to create the hello world website?")
    assert facts.index("Dispatched OpenCode CLI") < facts.index("Latest background jobs")


# --------------------------------------------- 2: questions stay questions --

@pytest.mark.parametrize("text", ["which of those do I visit most?", "What is the start of Cygnus?",
                                  "how do I clear my cache"])
def test_wh_questions_are_answered(text):
    assert b._is_question_not_request(text) is True


@pytest.mark.parametrize("pick, question", [
    (("chat", 0.9), True), (("task", 0.95), False), (("task", 0.5), True), ((None, 0.0), False)])
def test_yes_no_questions_ask_the_semantic_router(monkeypatch, pick, question):
    import semantic_route
    monkeypatch.setattr(semantic_route, "pick", lambda text: pick)
    assert b._is_question_not_request("can you visit websites?") is question


@pytest.mark.parametrize("text, reply", [
    ("test", "I'm here, sir. What can I do for you?"), ("jarvis", "I'm here, sir. What can I do for you?"),
    ("testing.", "I'm here, sir. What can I do for you?"),
    ("ok", "Alright, sir."), ("never mind.", "Alright, sir.")])
def test_a_bare_check_in_is_answered_without_a_model(text, reply):
    assert b._is_bare_check_in(text) is True
    assert b._check_in_reply(text) == reply


@pytest.mark.parametrize("text, question", [
    ("In one word, what colour is the sky on a clear day?", True),
    ("Quick one, how many sites do you know?", True),
    ("Open notepad, what do you think?", True),
    ("open notepad, then close it", False)])
def test_a_question_after_a_lead_in_is_still_a_question(text, question):
    assert b._is_question_not_request(text) is question


@pytest.mark.parametrize("method, kwargs", [
    ("_think_router", {}),
    ("_think_openai_compat", {"base_url": "http://x", "api_key": "k", "model": "m"})])
def test_model_clients_never_retry_a_timed_out_call(monkeypatch, method, kwargs):
    import openai
    made = []

    class Client:
        def __init__(self, **kw):
            made.append(kw)
            raise RuntimeError("stop here")
    monkeypatch.setattr(openai, "OpenAI", Client)
    brain = object.__new__(b.JarvisBrain)
    brain.conversation = []
    with pytest.raises(RuntimeError):
        getattr(brain, method)("hi", **kwargs)
    assert made and made[0]["max_retries"] == 0


class _Health:
    """A stand-in router_health: live ordering, failure reports, notice."""

    def __init__(self, healthy):
        self.healthy, self.marked = healthy, []

    def healthy_models(self):
        return self.healthy

    def mark_unhealthy(self, model, reason):
        self.marked.append((model, reason))

    def switch_notice(self, failed, model):
        return f"{failed} is down, so {model} is answering."


def test_router_models_follow_the_live_health_list(monkeypatch):
    import sys
    monkeypatch.setattr(b, "ROUTER_MODEL", "a")
    monkeypatch.setattr(b, "ROUTER_FALLBACK_MODELS", ["b", "c"])
    monkeypatch.setitem(sys.modules, "router_health", None)
    assert b._router_models() == ["a", "b", "c"]                  # no health module yet
    monkeypatch.setitem(sys.modules, "router_health", _Health(["c", "b"]))
    assert b._router_models() == ["c", "b"]
    monkeypatch.setitem(sys.modules, "router_health", _Health([]))
    assert b._router_models() == ["a", "b", "c"]                  # nothing known: configured


def test_a_down_model_hands_over_and_says_which_one_answered(monkeypatch):
    import sys
    from types import SimpleNamespace
    import openai
    health = _Health(["a", "b"])
    monkeypatch.setitem(sys.modules, "router_health", health)

    def create(model, **kw):
        if model == "a":
            raise RuntimeError("Error code: 503 - Unavailable")
        msg = SimpleNamespace(content="Blue, sir.", tool_calls=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)], usage=None)

    class Client:
        def __init__(self, **kw):
            self.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
    monkeypatch.setattr(openai, "OpenAI", Client)
    heard = []
    monkeypatch.setattr(b._progress_local, "cb", heard.append, raising=False)
    monkeypatch.setattr(b._progress_local, "deadline", None, raising=False)
    brain = object.__new__(b.JarvisBrain)
    brain.conversation = [{"role": "user", "content": "what colour is the sky?"}]
    brain.last_stats = {}
    assert brain._think_router("what colour is the sky?") == "Blue, sir."
    assert brain.last_stats == {"model": "b", "switched_from": "a"}
    assert heard == ["a is down, so b is answering."]
    assert health.marked and health.marked[0][0] == "a"


def test_the_switch_signal_goes_out_only_once_router_health_exists(monkeypatch):
    import sys
    heard = []
    monkeypatch.setattr(b._progress_local, "cb", heard.append, raising=False)
    monkeypatch.setitem(sys.modules, "router_health", None)
    b._announce_model_switch("a", "b")
    assert heard == []                                            # nobody to read it yet
    bare = type("Health", (), {"healthy_models": staticmethod(lambda: [])})()
    monkeypatch.setitem(sys.modules, "router_health", bare)
    b._announce_model_switch("a", "b")
    assert heard == ["model_switch:a->b"]                         # router_health words it


def test_a_real_request_is_not_a_check_in():
    assert b._is_bare_check_in("test the login page") is False
    assert b._is_bare_check_in("open notepad") is False


def test_commands_are_not_questions():
    assert b._is_question_not_request("open notepad") is False
    assert b._is_question_not_request("create a website with opencode") is False


# ------------------------------------------------------ 1: time limit --

def test_each_call_gets_what_the_turn_has_left(monkeypatch):
    monkeypatch.setattr(b._progress_local, "deadline", None, raising=False)
    assert b._call_timeout(45) == 45
    b._progress_local.deadline = time.monotonic() + 10
    assert b._call_timeout(45) <= 10
    b._progress_local.deadline = time.monotonic() + 1
    with pytest.raises(b._OutOfTime):
        b._call_timeout(45)
    b._progress_local.deadline = None


def _chain_brain(router, ollama, busy=0):
    brain = object.__new__(b.JarvisBrain)
    brain._prefer_local = brain._use_cerebras = brain._local_only = False
    brain._use_router = brain._use_ollama = True
    brain._ollama_active = busy
    brain.last_stats, brain.last_backend = {}, None
    brain.conversation = [{"role": "user", "content": "hi"}]
    brain._think_router, brain._think_ollama = router, ollama
    return brain


def _fails(msg):
    def raise_it(user_input):
        raise RuntimeError(msg)
    return raise_it


def test_a_turn_no_model_answers_says_so():
    brain = _chain_brain(_fails("503 unavailable"), _fails("timed out"))
    assert brain._answer_with_models("hi") == b._NO_MODEL_REPLY
    assert brain.conversation[-1]["content"] == b._NO_MODEL_REPLY and brain.last_backend == "timeout"


def test_a_busy_local_model_is_not_queued_behind():
    ran = []
    brain = _chain_brain(_fails("503"), lambda u: ran.append(u) or "late answer", busy=1)
    assert brain._answer_with_models("hi") == b._NO_MODEL_REPLY and ran == []


def test_an_answering_model_still_answers():
    brain = _chain_brain(lambda u: "Hello, sir.", _fails("unused"))
    assert brain._answer_with_models("hi") == "Hello, sir." and brain.last_backend == "router"


# --------------------------------------------- 4: one conversation per client --

def test_each_client_keeps_its_own_history(monkeypatch):
    import intent_router
    monkeypatch.setattr(intent_router, "turn_started", lambda text: (_ for _ in ()).throw(RuntimeError))
    brain = object.__new__(b.JarvisBrain)
    brain._lead_record = None

    def turn(user_input, **kw):
        brain.conversation.append({"role": "user", "content": user_input})
        return "ok"
    brain._think_turn = turn
    brain.think("the code word is FALCON", client="phone")
    brain.think("the code word is ORCHID", client="session:test")
    brain.think("hello")
    by_client = brain.__dict__["_conversations"]
    assert [m["content"] for m in by_client["phone"]] == ["the code word is FALCON"]
    assert [m["content"] for m in by_client["session:test"]] == ["the code word is ORCHID"]
    assert [m["content"] for m in by_client["default"]] == ["hello"]
    brain.reset = b.JarvisBrain.reset.__get__(brain)
    brain._use_mock, brain.last_stats = False, {}
