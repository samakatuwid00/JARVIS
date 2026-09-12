"""Task 4: a chain of quick actions is split into commands that each route on
their own; goals, questions and plain sentences stay whole."""

import pytest

import brain_gemini
from command_chain import split_commands


@pytest.mark.parametrize("text, commands", [
    ("close notepad and Spotify", ["close notepad", "close Spotify"]),
    ("close spotify, notepad, and calculator", ["close spotify", "close notepad", "close calculator"]),
    ("Close the Spotify and the Notepad. Jarvis.", ["Close the Spotify", "Close the Notepad"]),
    ("jarvis force close task manager and brave", ["force close task manager", "force close brave"]),
    ("Open Spotify and play Be All Right by Dean Lewis, Jarvis.",
     ["Open Spotify", "play Be All Right by Dean Lewis"]),
    ("Can you open Spotify and play some music?", ["open Spotify", "play some music"]),
    ("open spotify lessen the volume to 40 then play harry style two ghosts",
     ["open spotify lessen the volume to 40", "play harry style two ghosts"]),
    ("open brave and search manus ai", ["open brave", "search manus ai in brave"]),
    ("open chatgpt and search history irimsv project report",
     ["open chatgpt", "search history irimsv project report in chatgpt"]),
    ("open brave and search cats on youtube", ["open brave", "search cats on youtube"]),
    ("Open Microsoft Word JARVIS and close the Notepad JARVIS.",
     ["Open Microsoft Word", "close the Notepad"]),
    ("open spotify play two ghosts", ["open spotify", "play two ghosts"]),
    ("turn it down and play something else", ["turn it down", "play something else"])])
def test_chains_split_into_commands(text, commands):
    assert split_commands(text) == commands


@pytest.mark.parametrize("text", [
    "play rock and roll", "search for tom and jerry",
    "Create a folder called sandbox in my Documents and write today's plan into a file",
    "Open Spotify, then delete C:\\old.txt and play Nujabes",
    "search the web for the top 3 lo-fi channels and summarize them",
    "what is the weather and play music",
    "Get my workspace ready: open VS Code, open my GitHub in Brave",
    "It is not open on my end. So close the Facebook site and open it again.",
    "open spotify", "yes, proceed", "",
    "Open, open, open, open, open.", "Open, open code, Jarvis.",
    "Play some music on Spotify, play some music on Spotify.",
    "open chatgpt and search in history irimsv project report and tell me the latest reply"])
def test_goals_questions_and_sentences_stay_whole(text):
    assert split_commands(text) == [text]


def test_a_chain_runs_each_command_and_stops_at_a_confirm():
    brain = object.__new__(brain_gemini.JarvisBrain)
    ran = []

    def turn(clause, **kw):
        ran.append(clause)
        return "[NEEDS_CONFIRM] sure?" if clause.startswith("delete") else f"Done: {clause}."
    brain._think_turn = turn
    assert brain._run_chain(["open spotify", "play two ghosts"]) == \
        "Done: open spotify.\nDone: play two ghosts."
    ran.clear()
    out = brain._run_chain(["close notepad", "delete x", "play y"])
    assert ran == ["close notepad", "delete x"]
    assert out.endswith("I'll wait on that before the rest: play y.")
