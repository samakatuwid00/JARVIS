"""Task 2: the semantic router leads only where it earned it - a trusted
route, a confident pick, a model already loaded - and the brain maps each
route onto a path it already has. Embeddings are faked."""

import pytest

import brain_gemini
import semantic_route as sr


class _Bank:
    def __init__(self, route, score):
        self.pick = (route, score)

    def route(self, text=None, vector=None, min_score=0.0):
        route, score = self.pick
        return (route if score >= min_score else None), score


@pytest.fixture
def leading(monkeypatch):
    monkeypatch.setattr(sr, "MODE", "lead")

    def with_pick(route, score):
        monkeypatch.setattr(sr, "_bank", {"route": _Bank(route, score)})
    return with_pick


def test_a_confident_trusted_pick_leads(leading):
    leading("job_status", 0.82)
    assert sr.lead("what are you working on right now?") == "job_status"


@pytest.mark.parametrize("route, score", [("job_status", 0.65), ("close_app", 0.95)])
def test_weak_or_untrusted_picks_do_not_lead(leading, route, score):
    leading(route, score)
    assert sr.lead("anything") is None


def test_nothing_leads_before_the_model_is_loaded_or_when_not_leading(monkeypatch, leading):
    monkeypatch.setattr(sr, "_bank", None)
    assert sr.lead("what are you working on?") is None
    leading("job_status", 0.9)
    monkeypatch.setattr(sr, "MODE", "shadow")
    assert sr.lead("what are you working on?") is None


@pytest.mark.parametrize("route, brain_route", [
    ("job_status", "job_status"), ("app_action", "app_reference"),
    ("media_control", "app_reference"), ("task", "general"), ("clarify", "clarify"),
    ("recall_memory", None)])
def test_routes_map_onto_brain_paths(monkeypatch, route, brain_route):
    monkeypatch.setattr(sr, "lead", lambda text: route)
    assert brain_gemini._semantic_lead("some command") == brain_route


def test_open_site_leads_only_with_a_target(monkeypatch):
    monkeypatch.setattr(sr, "lead", lambda text: "open_site")
    assert brain_gemini._semantic_lead("Can you open github for me?") == "open_site"
    assert brain_gemini._semantic_lead("that website we talked about") is None


@pytest.mark.parametrize("text, target", [
    ("Can you open github for me?", "github for me"), ("visit youtube.com", "youtube.com"),
    ("open the github site", "github site"), ("what time is it", None)])
def test_open_target_reads_request_leads(text, target):
    assert brain_gemini._open_target(text) == target
