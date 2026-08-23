"""Unit tests for JARVIS Intake Layer (Phase 1).
Run:  python intake_test.py
Pure stdlib assertions -- no mic, no model. Verifies the Layer-1 contract:
  * bad grammar -> correct verb/entity
  * typo'd voice -> fuzzy recovery or safe 'did you mean?' fallback
  * no false high-confidence entity from garbage
  * confidence triage + word_conf demotion
  * autocomplete always returns tappable chips
"""
import intake
from intake import load_corpus, resolve_intent, normalize, autocomplete

C = load_corpus()


def check(name, cond):
    print(("PASS" if cond else "FAIL"), name)
    if not cond:
        raise SystemExit(f"FAILED: {name}")


# 1. Grammar-resilient routing ------------------------------------------------
it = resolve_intent("can you please open the notepad for me jarvis", C)
check("open notepad verb", it.verb == "open")
check("open notepad entity", it.entity == "notepad")
check("open notepad high conf", it.confidence == "high")

it = resolve_intent("copy the budget table from excel", C)
check("copy excel verb", it.verb == "copy")
check("copy excel entity", it.entity == "excel")
check("copy excel high conf", it.confidence == "high")

# 2. Fuzzy voice recovery / safe fallback -------------------------------------
it = resolve_intent("jarvus play taylor son", C)
check("taylor son no false entity", it.entity is None)
check("taylor son offers taylor swift", "taylor swift" in it.candidates)
check("taylor son low conf", it.confidence == "low")
check("taylor son needs confirm", it.needs_confirmation())

# 3. No garbage high-confidence entity ----------------------------------------
it = resolve_intent("what is the weather in manila", C)
check("weather no false entity", it.entity is None)
check("weather candidates include weather", "weather" in it.candidates)

# 4. Strong fuzzy project match -----------------------------------------------
it = resolve_intent("compose report for iRIMS-B", C)
check("iRIMS-B -> iRIMS", it.entity == "iRIMS-V" or it.entity == "iRIMS")
check("iRIMS high conf", it.confidence == "high")

it = resolve_intent("make me a website like my portfolio", C)
check("portfolio entity", it.entity == "Portfolio")
check("portfolio high conf", it.confidence == "high")

# 5. Word-confidence demotion (low mic confidence forces confirm) -------------
it = resolve_intent("play lofi", C, word_conf=[0.2, 0.3])
check("low word_conf demotes to low", it.confidence == "low")

it = resolve_intent("play lofi", C, word_conf=[0.95, 0.97])
check("high word_conf stays high", it.confidence == "high")

# 6. Autocomplete always returns chips ---------------------------------------
chips = autocomplete("opn notpad", C, verb="open")
check("autocomplete seeds verb", "open" in chips)
check("autocomplete finds notepad", "notepad" in chips)

# 7. normalize strips fillers + punctuation -----------------------------------
check("normalize drops fillers",
      normalize("Hey Jarvis, can you please open the Notepad?!") == "open notepad")

# 8. empty / silent input ------------------------------------------------------
it = resolve_intent("", C)
check("empty -> low + echo", it.confidence == "low" and it.echo)

print("\nALL INTAKE LAYER TESTS PASSED")
