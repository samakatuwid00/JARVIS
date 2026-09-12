"""The smoke and stress scripts' own logic, offline: how a turn is read off
the socket, how the audit trail says what ran, and how a step is judged."""

import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import cygnus_client as cc  # noqa: E402
import cygnus_conversation as conv  # noqa: E402
import cygnus_smoke as smoke  # noqa: E402
import cygnus_stress as stress  # noqa: E402


class FakeSocket:
    def __init__(self, messages):
        self.sent, self.queue = [], [json.dumps(m) for m in messages]

    async def send(self, data):
        self.sent.append(json.loads(data))

    async def recv(self):
        if not self.queue:
            await asyncio.sleep(3600)
        return self.queue.pop(0)


def test_a_turn_is_its_reply_not_a_late_one():
    ws = FakeSocket([{"type": "status", "state": "idle"},              # left over from before
                     {"type": "transcript", "text": "hi"}, {"type": "status", "state": "thinking"},
                     {"type": "response", "text": "Earlier job done.", "deferred": True},
                     {"type": "response", "text": "Hello, sir."},
                     {"type": "telemetry", "stats": {"backend": "instant", "intent": "greeting"}},
                     {"type": "status", "state": "idle"}])
    turn = asyncio.run(cc.ask(ws, "hi", timeout=5))
    assert ws.sent == [{"type": "text", "text": "hi"}]
    assert turn.reply == "Hello, sir." and turn.late == ["Earlier job done."]
    assert turn.telemetry["intent"] == "greeting" and turn.error is None


def test_an_error_ends_the_turn_and_silence_times_out():
    turn = asyncio.run(cc.ask(FakeSocket([{"type": "error", "text": "boom"}]), "x", timeout=5))
    assert turn.error == "boom"
    turn = asyncio.run(cc.ask(FakeSocket([]), "x", timeout=0.2))
    assert turn.error.startswith("no reply within")


def test_the_audit_trail_names_what_ran(tmp_path):
    audit = tmp_path / "audit.jsonl"
    audit.write_text(json.dumps({"caller": "execute_tool:list_sites"}) + "\n", encoding="utf-8")
    mark = os.path.getsize(audit)
    with open(audit, "a", encoding="utf-8") as f:
        f.write(json.dumps({"caller": "execute_tool:open_application"}) + "\n")
        f.write("not json\n")
        f.write(json.dumps({"tool": "rules.action"}) + "\n")
        for decision in ("needs_confirm", "NEEDS_CONFIRM", "declined"):     # a gated delete
            f.write(json.dumps({"caller": "delegate_to_hermes", "decision": decision}) + "\n")
    assert cc.tools_since(mark, str(audit)) == ["open_application", "rules.action"]


def test_a_step_is_judged_on_reply_and_tools():
    turn = cc.Turn(said="open notepad and calculator", reply="Opened notepad.")
    expect = {"reply": r"opened", "count": {"open_application": 2}, "forbid": {"run_autonomous"}}
    assert smoke.check_step(expect, turn, ["open_application"]) == ["open_application ran 1x, want 2"]
    assert smoke.check_step(expect, turn, ["open_application", "open_application"]) == []
    assert smoke.check_step({"tools": {"open_site"}}, turn, []) == \
        ["none of ['open_site'] ran (ran: nothing)"]
    assert smoke.check_step({"forbid": {"open_site"}, "reject": r"error"},
                            cc.Turn(said="x", reply="[Error] no"), ["open_site"]) == \
        ["reply matches /error/", "ran ['open_site'], which it must not"]


def test_every_case_is_well_formed():
    names = [c["name"] for c in smoke.CASES]
    assert len(names) == len(set(names))
    for case in smoke.CASES:
        assert case["tier"] in smoke.TIERS and case["steps"]
        assert all(s["say"].strip() for s in case["steps"])


def _row(say, reply, route=("instant", "time"), ms=100, problems=()):
    return {"say": say, "reply": reply, "backend": route[0], "intent": route[1], "ms": ms,
            "problems": list(problems)}


def test_consistency_reads_repeats_turn_by_turn():
    runs = [[_row("what time is it", "It is 07:43 PM."), _row("anong petsa", "Sabado", ("router", None))],
            [_row("what time is it", "It is 07:44 PM."), _row("anong petsa", "Saturday", ("hermes", "general"),
                                                                 problems=["x"])]]
    first, second = conv.consistency(runs)
    assert first["pass_rate"] == 1 and first["route_stable"] == 1 and first["distinct_replies"] == 1
    assert second["pass_rate"] == 0.5 and second["route_stable"] == 0.5 and second["distinct_replies"] == 2


def test_conversation_findings_name_what_broke():
    report = {"sessions": {"code word kept": [[_row("What code word did I give you?", "No idea",
                                                     problems=["reply does not match /falcon/"])]]},
              "consistency": {},
              "overlap": {"queued_follow_up": {"kept_context": True},
                          "cloud_and_instant_side_by_side": {"fast_waited_for_slow": True, "fast_ms": 5200,
                                                             "slow_mentions_time": False}},
              "concurrent": {"sessions": [{"got_own": False}, {"got_own": True}], "kept_own": 1, "crossed": 1}}
    assert conv.summary(report) == [
        "code word kept: failed on 'What code word did I give you?'",
        "an instant command waited 5200 ms behind another client's cloud turn",
        "1 of 2 simultaneous sessions got another session's code word back",
        "only 1 of 2 simultaneous sessions recalled their own code word"]


def test_every_session_is_well_formed():
    names = [s["name"] for s in conv.SESSIONS]
    assert len(names) == len(set(names))
    assert all(s["tier"] in ("safe", "live") and len(s["turns"]) >= 2 for s in conv.SESSIONS)


def test_percentiles_and_findings():
    assert cc.percentile([], 0.5) is None
    assert cc.percentile([30, 10, 20], 0.5) == 20 and cc.percentile([1, 2, 3, 4], 0.95) == 4
    out = {"http": {"errors": 0},
           "instant": {"errors": 0, "wrong_count": 1, "status_while_loaded": {"max_ms": 4000}},
           "burst": {"in_order_and_right": True, "replies": 8, "sent": 8},
           "edge": [{"input": "malformed JSON", "socket": "closed (ConnectionClosedError)",
                     "server_alive": True}],
           "churn": {"server_alive": True}}
    assert stress.verdicts(out) == ["instant commands: 0 errors, 1 wrong answers",
                                    "/status took up to 4000 ms under load",
                                    "edge 'malformed JSON' closed the socket"]
