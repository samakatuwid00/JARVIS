"""Accomplishment reports for JARVIS - past ChatGPT threads into a Word file.

Read-only against ChatGPT throughout: it uses browser_agent's existing search
and read tools, which only look at conversations that are already there. It
never types a prompt into the site and never sends anything.

The synthesis is done by the local brain (brain_gemini.JarvisBrain), not by
ChatGPT, so nothing leaves the machine beyond what the brain backend already
sees. The result is written as a real .docx through python-docx.
"""

import datetime
import re
from pathlib import Path

DEFAULT_DIR = Path(r"C:\Users\deped\Documents")
# How many past conversations to search for, and how many to actually read back.
SEARCH_LIMIT = 5
READ_LIMIT = 3
# Windows forbids these in filenames, and a spoken topic will contain some.
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def _safe_filename(topic: str) -> str:
    cleaned = _BAD_FILENAME_CHARS.sub("", topic).strip().strip(".")
    return (cleaned or "Report")[:80]


def _titles_from_search(result: str):
    """Pull the conversation titles out of search_chatgpt_history's sentence.

    That tool answers for speech, not for parsing - "Found 3 conversation(s)
    matching 'x': 1. First; 2. Second" - so the titles are recovered rather than
    returned structured. A layout change here degrades to zero titles, which the
    caller reports honestly instead of inventing sources.
    """
    if not result or result.startswith("[Error]") or result.startswith("No past"):
        return []
    _, _, tail = result.partition("': ")
    titles = []
    for chunk in tail.split("; "):
        title = re.sub(r"^\s*\d+\.\s*", "", chunk).strip()
        if title:
            titles.append(title)
    return titles


def _add_body(doc, text: str):
    """Lay the brain's prose out as headings, bullets and paragraphs."""
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            level = min(len(line) - len(line.lstrip("#")), 4)
            doc.add_heading(line.lstrip("#").strip(), level=max(2, level))
        elif line[:2] in ("- ", "* ", "• "):
            doc.add_paragraph(line[2:].strip(), style="List Bullet")
        elif re.match(r"^\d+[.)]\s", line):
            doc.add_paragraph(re.sub(r"^\d+[.)]\s*", "", line), style="List Number")
        elif len(line) < 70 and line.endswith(":"):
            doc.add_heading(line.rstrip(":"), level=2)
        else:
            doc.add_paragraph(line)


def compose_report(topic: str, output_path: str = None) -> str:
    """Write an accomplishment report on `topic` as a .docx and return its path.

    Requires the automation browser to be signed in to ChatGPT; when it is not,
    this returns the one-time sign-in instruction and does nothing else.
    """
    try:
        import browser_agent

        if not browser_agent.is_signed_in():
            return ("I need the JARVIS browser signed in to ChatGPT before I can "
                    "gather the source material. Run python browser_agent.py once, "
                    "sign in to ChatGPT in the window it opens, and the session is "
                    "remembered from then on.")

        found = browser_agent.search_chatgpt_history(topic, SEARCH_LIMIT)
        # A failed search is not an empty search: reporting "nothing found" for a
        # broken browser hides the real cause, so hand the error straight back.
        if found and found.startswith("[Error]"):
            return found

        titles = _titles_from_search(found)
        if not titles:
            return (f"I found no past ChatGPT conversations about '{topic}', so there "
                    "is nothing to build a report from yet.")

        sources = []
        used = []
        for title in titles[:READ_LIMIT]:
            # Match on a fragment: the sidebar truncates long titles.
            body = browser_agent.open_chatgpt_conversation(title[:60])
            if not body or body.startswith("[Error]"):
                continue
            sources.append(f"--- Conversation: {title} ---\n{body}")
            used.append(title)
        if not sources:
            return (f"I found {len(titles)} conversations about '{topic}' but could "
                    "not read any of them back, so I have written nothing.")

        material = "\n\n".join(sources)[:12000]
        import brain_gemini

        brain = brain_gemini.JarvisBrain()
        prose = brain.think(
            f"Write an accomplishment report about {topic} based on this source "
            "material. Structure it with a short summary, the work accomplished as "
            "bullet points, the outcomes, and next steps. Write it as a document, "
            "not as speech.\n\n" + material
        )
        if not prose or prose.startswith("[Error]"):
            return f"[Error] The brain did not produce a report: {prose}"

        from docx import Document

        today = datetime.date.today().isoformat()
        if output_path:
            out = Path(output_path).expanduser()
        else:
            out = DEFAULT_DIR / f"Accomplishment Report {_safe_filename(topic)} {today}.docx"
        out.parent.mkdir(parents=True, exist_ok=True)

        doc = Document()
        doc.add_heading(f"Accomplishment Report: {topic}", level=0)
        doc.add_paragraph(f"Prepared {today}")
        doc.add_paragraph("Sources: " + "; ".join(used))
        _add_body(doc, prose)
        doc.save(str(out))

        return (f"Saved the accomplishment report on {topic} to {out}, drawn from "
                f"{len(used)} past conversation(s).")
    except Exception as e:
        return f"[Error] Could not compose the report on '{topic}': {type(e).__name__}: {e}"
