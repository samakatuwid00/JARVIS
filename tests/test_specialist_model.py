"""An OpenCode agent whose own 9router model is down runs on a healthy one
that OpenCode's config lists (every hand-off failed with "Missing API key"
on cx/gpt-5.4-mini, 2026-09-12)."""

import json

import pytest

import router_health
import tools

CFG = {"model": "ninerouter/cx/gpt-5.4-mini",
       "agent": {"browser-session": {"model": "ninerouter/ag/gemini-3-flash"},
                 "local-helper": {"model": "ollama/jarvis-qwen3"}},
       "provider": {"ninerouter": {"models": {"cx/gpt-5.4-mini": {}, "ag/gemini-3-flash": {},
                                              "oc/big-pickle": {}, "oc/mimo-v2.5-free": {}}}}}


@pytest.fixture
def config(monkeypatch, tmp_path):
    path = tmp_path / "opencode.json"
    path.write_text(json.dumps(CFG), encoding="utf-8")
    real_open = open
    monkeypatch.setattr("builtins.open", lambda p, *a, **k: real_open(
        path if str(p).endswith("opencode.json") else p, *a, **k))


def test_a_dead_model_is_swapped_for_a_healthy_listed_one(config, monkeypatch):
    monkeypatch.setattr(router_health, "healthy_models", lambda: ["gc/gemini-2.5-flash", "oc/big-pickle"])
    assert tools._specialist_model_args(None) == ["-m", "ninerouter/oc/big-pickle"]
    assert tools._specialist_model_args("browser-session") == ["-m", "ninerouter/oc/big-pickle"]


def test_a_working_model_is_left_alone(config, monkeypatch):
    monkeypatch.setattr(router_health, "healthy_models", lambda: ["cx/gpt-5.4-mini", "oc/big-pickle"])
    assert tools._specialist_model_args(None) == []


def test_nothing_changes_without_a_known_healthy_model(config, monkeypatch):
    monkeypatch.setattr(router_health, "healthy_models", lambda: [])
    assert tools._specialist_model_args(None) == []
    monkeypatch.setattr(router_health, "healthy_models", lambda: ["gc/gemini-2.5-flash"])   # not listed
    assert tools._specialist_model_args(None) == []


def test_a_local_model_agent_is_left_alone(config, monkeypatch):
    monkeypatch.setattr(router_health, "healthy_models", lambda: ["oc/big-pickle"])
    assert tools._specialist_model_args("local-helper") == []
