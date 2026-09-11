"""Shared test setup: no test may open a real browser.

rules_sim checks rules by loading real pages in a hidden Chrome. Tests get an
offline page checker that answers "blocked" at once; tests that exercise the
simulation pass their own fake probe.
"""

import pytest


class OfflineProbe:
    def discover_search(self, site_url):
        return {"blocked": True, "title": "offline"}

    def probe_search(self, search_url, sample):
        return {"status": "blocked", "evidence": "offline test", "title": "",
                "url": search_url.replace("{query}", sample)}

    def probe_open(self, url):
        return {"status": "blocked", "evidence": "offline test", "title": "", "url": url}


@pytest.fixture(autouse=True)
def offline_probe(monkeypatch):
    import rules_sim
    import rules_steps
    import rules_voice
    monkeypatch.setattr(rules_sim, "PROBE", OfflineProbe())
    monkeypatch.setattr(rules_steps, "_RUN", {})
    monkeypatch.setattr(rules_steps, "_LAST", {})
    # module-level state must not leak between tests that reuse "brave"
    monkeypatch.setattr(rules_sim, "_PROGRESS", {})
    monkeypatch.setattr(rules_voice, "_PENDING", {})
    import tools
    tools._PENDING_RULE_SLOT.clear()
