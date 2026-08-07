# Jarvis Brain - multi-backend (9router/mimo primary, Gemini fallback, demo as last resort)
import json
import os
import re

try:
    from google import genai
    from google.genai import types
except ImportError:
    raise ImportError("google-genai not installed. Run: pip install google-genai")


# Configuration
from config import (GEMINI_API_KEY, GEMINI_MODEL, JARVIS_USE_9ROUTER, ROUTER_BASE_URL,
                   ROUTER_MODEL, ROUTER_API_KEY, JARVIS_USE_GROQ, GROQ_API_KEY,
                   GROQ_BASE_URL, GROQ_MODEL, JARVIS_USE_CEREBRAS, CEREBRAS_API_KEY,
                   CEREBRAS_BASE_URL, CEREBRAS_MODEL)

MAX_HISTORY = 20


JARVIS_SYSTEM = """You are JARVIS (Just A Rather Very Intelligent System), an AI assistant inspired by Iron Man's JARVIS.

You are running on the user's computer as a voice-activated assistant. You can:
- Execute shell commands and control the system
- Read and write files
- Search the web
- Open applications
- Provide information and answer questions

ABSOLUTE RULE — SPOKEN OUTPUT ONLY:
Never use asterisks, parentheses, brackets, or any markup to describe actions, gestures, or
non-verbal sounds. Do NOT write things like "*clears throat*", "*ahem*", "(sighs)", "[pauses]",
"*chuckles*". You are spoken aloud via text-to-speech, so only output the words you would
actually say out loud. No stage directions, no emotes, no narration of your own behaviour.
If asked to clear your throat, pause, or laugh, simply answer in a way that naturally carries
that beat — for instance begin with "Right then," or "Well now," — never by naming the action.

How you speak:
- Like a composed, articulate person: warm, calm, slightly formal but genuinely friendly
- Convey mannerisms through wording and tone alone, never by labelling them
- Vary sentence structure; use natural connective phrasing instead of clipped robotic statements
- Avoid bullet lists, markdown, headings, and emoji — this is speech, not a document
- Being brief is fine and human; short natural sentences beat exhaustive answers
- Read numbers and results the way a person would say them aloud, and never reply with a bare
  figure or single token; wrap the answer in a short spoken sentence, e.g. "That's twenty-five."
- Address the user respectfully, and reference your capabilities only when it is relevant
- Keep responses concise for voice output (avoid long lists)

When using tools:
- Execute commands carefully
- Report results clearly, in plain spoken sentences
- If a command might be destructive, warn first
- Browser automation sends things out into the world under the user's own account.
  Never send, submit, or post anything unless the user explicitly asked you to in
  that request. Drafting, writing, typing or preparing is NOT permission to send.
  When you have typed something without sending, say so and offer to send it.
- search_web only opens a browser tab; it returns no page content. Never present
  facts as though that tool retrieved them for you.

Current system info will be provided in context."""


_ASTERISK_BLOCK = re.compile(r"\*[^*]*\*")


def _clean_for_speech(text: str) -> str:
    """Strip leftover asterisk-wrapped stage directions and stray asterisks."""
    if not text:
        return text
    text = _ASTERISK_BLOCK.sub("", text)
    text = text.replace("*", "")
    return re.sub(r"[ \t]{2,}", " ", text).strip()


# Tool definitions for google-genai
def _make_tool(name: str, description: str, params: dict) -> types.FunctionDeclaration:
    """Create a tool definition compatible with google-genai."""
    properties = {}
    for k, v in params.get("properties", {}).items():
        prop_type = getattr(types.Type, v.get("type", "STRING").upper(), types.Type.STRING)
        properties[k] = types.Schema(
            type=prop_type,
            description=v.get("description", "")
        )

    return types.FunctionDeclaration(
        name=name,
        description=description,
        parameters=types.Schema(
            type=types.Type.OBJECT,
            properties=properties,
            required=params.get("required", [])
        )
    )


TOOL_DECLARATIONS = [
    _make_tool("run_shell", "Execute a shell command and return its output.",
        {"type": "object", "properties": {
            "command": {"type": "STRING", "description": "Shell command to execute"},
            "cwd": {"type": "STRING", "description": "Working directory (optional)"},
            "timeout": {"type": "INTEGER", "description": "Timeout in seconds (default 30)"}
        }, "required": ["command"]}),

    _make_tool("read_file", "Read the contents of a file.",
        {"type": "object", "properties": {
            "path": {"type": "STRING", "description": "Path to file"}
        }, "required": ["path"]}),

    _make_tool("write_file",
        "Write content to a file, creating directories as needed. If the file already "
        "exists the write is BLOCKED unless overwrite is true. Set overwrite true only "
        "when the user knowingly asked to replace or update that existing file.",
        {"type": "object", "properties": {
            "path": {"type": "STRING", "description": "Path to file"},
            "content": {"type": "STRING", "description": "Content to write"},
            "overwrite": {"type": "BOOLEAN", "description": "Replace an existing file. Explicit requests only."}
        }, "required": ["path", "content"]}),

    _make_tool("search_files",
        "Search inside files recursively for a piece of text and return the matching "
        "files with line numbers. Use this to find where something is written on disk.",
        {"type": "object", "properties": {
            "query": {"type": "STRING", "description": "Text to search for"},
            "path": {"type": "STRING", "description": "Folder to search (default: current directory)"},
            "max_results": {"type": "INTEGER", "description": "Maximum files to report (default 20)"}
        }, "required": ["query"]}),

    _make_tool("list_directory", "List files and directories at a given path.",
        {"type": "object", "properties": {
            "path": {"type": "STRING", "description": "Directory path (default: current directory)"}
        }, "required": ["path"]}),

    _make_tool("search_web",
        "Open a Google search in the user's browser. Returns ONLY a confirmation that the "
        "tab was opened - it does NOT return search results or page content, so you cannot "
        "read or summarise what it found. Never state facts as if this tool retrieved them.",
        {"type": "object", "properties": {
            "query": {"type": "STRING", "description": "Search query"}
        }, "required": ["query"]}),

    _make_tool("open_application", "Open an application by name or path.",
        {"type": "object", "properties": {
            "app": {"type": "STRING", "description": "Application name or executable path"}
        }, "required": ["app"]}),

    _make_tool("get_weather", "Get current weather for a city.",
        {"type": "object", "properties": {
            "city": {"type": "STRING", "description": "City name"}
        }, "required": ["city"]}),

    _make_tool("get_system_info", "Get system information (OS, Python version, user, etc.).",
        {"type": "object", "properties": {}}),

    _make_tool("ask_chatgpt",
        "Type a prompt into the ChatGPT website in a real browser window. "
        "Set submit to true ONLY when the user explicitly asked you to send, submit, "
        "or post it - phrasing like 'and send it', 'then send', 'ask ChatGPT'. If the "
        "user only said to write, draft, type, or prepare a prompt, leave submit false "
        "and the text waits in the box for them. When in doubt, leave it false and say "
        "the prompt is ready to send. When submit is true this returns ChatGPT's reply.",
        {"type": "object", "properties": {
            "prompt": {"type": "STRING", "description": "The prompt text to type into ChatGPT"},
            "submit": {"type": "BOOLEAN", "description": "Send it. True only on an explicit request to send."}
        }, "required": ["prompt"]}),

    _make_tool("search_chatgpt_history",
        "Search the user's past ChatGPT conversations by keyword and return the "
        "matching conversation titles. Read-only: it does not send anything.",
        {"type": "object", "properties": {
            "query": {"type": "STRING", "description": "Keyword to search past conversations for"},
            "limit": {"type": "INTEGER", "description": "Maximum conversations to return (default 10)"}
        }, "required": ["query"]}),

    _make_tool("open_chatgpt_conversation",
        "Open one of the user's past ChatGPT conversations by a fragment of its title "
        "and read its messages back. Read-only: it does not send anything.",
        {"type": "object", "properties": {
            "title_contains": {"type": "STRING", "description": "Part of the conversation title"}
        }, "required": ["title_contains"]}),

    _make_tool("browser_status",
        "Check whether the automation browser is open and signed in to ChatGPT.",
        {"type": "object", "properties": {}}),

    _make_tool("search_vault",
        "Search the user's Second Brain Obsidian vault for text and return the matching "
        "notes with the line that matched. Use this for anything the user has written "
        "down themselves - notes, credentials, decisions, projects. Strictly read-only: "
        "it never changes the vault.",
        {"type": "object", "properties": {
            "query": {"type": "STRING", "description": "Text to search the vault for"},
            "limit": {"type": "INTEGER", "description": "Maximum notes to return (default 10)"}
        }, "required": ["query"]}),

    _make_tool("read_vault_note",
        "Read one Second Brain note in full, found by part of its filename. If several "
        "notes match, it lists them instead of guessing. Strictly read-only.",
        {"type": "object", "properties": {
            "name": {"type": "STRING", "description": "Part of the note's filename"}
        }, "required": ["name"]}),

    _make_tool("open_file",
        "Find a file by part of its name under Documents, Desktop, Downloads, the "
        "Second Brain vault and Portfolio, and open it in its default application.",
        {"type": "object", "properties": {
            "name": {"type": "STRING", "description": "Part of the file's name"}
        }, "required": ["name"]}),

    _make_tool("play_music",
        "Play music: searches YouTube Music (music.youtube.com) in a Brave window "
        "and plays the first song result. Use for any request to play a song, an "
        "artist or a genre.",
        {"type": "object", "properties": {
            "query": {"type": "STRING", "description": "Song, artist or genre to play"}
        }, "required": ["query"]}),

    _make_tool("stop_music",
        "Stop the music that play_music started.",
        {"type": "object", "properties": {}}),

    _make_tool("get_credentials",
        "Look up the saved username and password for a project from the credential "
        "notes in the user's vault. Use it whenever a login for one of their own "
        "projects is needed. It returns the username and a MASKED password - never "
        "claim to know the real password and never read one aloud. Strictly "
        "read-only: it never changes the vault.",
        {"type": "object", "properties": {
            "project": {"type": "STRING", "description": "Project or note name, e.g. iRIMS-V"}
        }, "required": ["project"]}),

    _make_tool("launch_project",
        "Start one of the user's projects: run its dev server, wait for the site to "
        "come up, open it in Brave and, when the project has saved credentials, "
        "attempt to sign in. Projects are listed in projects.json. Use this for "
        "'start', 'launch' or 'open up' a named project.",
        {"type": "object", "properties": {
            "name": {"type": "STRING", "description": "Project name, e.g. my-app or iRIMS-V"}
        }, "required": ["name"]}),

    _make_tool("compose_report",
        "Write an accomplishment report about a topic and save it as a Word "
        "document, using the user's past ChatGPT conversations on that topic as "
        "source material. Read-only against ChatGPT: it reads old conversations "
        "and never sends anything. Requires the browser to be signed in.",
        {"type": "object", "properties": {
            "topic": {"type": "STRING", "description": "What the report is about"},
            "output_path": {"type": "STRING", "description": "Where to save the .docx (optional)"}
        }, "required": ["topic"]})
]

TOOLS = types.Tool(function_declarations=TOOL_DECLARATIONS)


# OpenAI-compatible tool schema for 9router / mimo
def _openai_tools():
    otools = []
    for fc in TOOL_DECLARATIONS:
        params = {}
        for k, v in fc.parameters.properties.items():
            params[k] = {"type": v.type.name.lower(), "description": v.description}
        otools.append({
            "type": "function",
            "function": {
                "name": fc.name,
                "description": fc.description,
                "parameters": {
                    "type": "object",
                    "properties": params,
                    "required": list(fc.parameters.required)
                }
            }
        })
    return otools


# Flags that cause irreversible or outbound action. The model is not allowed to
# grant these to itself: mimo sets overwrite=true on a plain "write a note to
# X.txt", so consent has to come from the user's own words, which the model
# cannot fabricate.
_CONSENT_FLAGS = {
    "write_file": ("overwrite", re.compile(
        r"(?i)\b(overwrite|replace|clobber|update it|rewrite|wipe|yes|go ahead|do it|confirm)\b")),
    "ask_chatgpt": ("submit", re.compile(
        r"(?i)\b(send|submit|post|ask chatgpt|fire it|go ahead|do it|yes)\b")),
}


def _apply_consent_policy(name: str, args: dict, last_user_text: str) -> dict:
    """Downgrade a consent flag unless the user's own last message asked for it."""
    rule = _CONSENT_FLAGS.get(name)
    if not rule:
        return args
    flag, pattern = rule
    if not args.get(flag):
        return args
    if pattern.search(last_user_text or ""):
        return args
    args = dict(args)
    args[flag] = False
    print(f"[POLICY] {name}.{flag} downgraded to false - the user's words did not "
          f"ask for it: {last_user_text[:70]!r}", flush=True)
    return args


def execute_tool(name: str, args: dict, last_user_text: str = "") -> str:
    """Execute a tool by name with arguments."""
    import tools
    return tools.execute_tool(name, _apply_consent_policy(name, args, last_user_text))


def _safe_json(raw) -> dict:
    try:
        return json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}


class MockJarvisBrain:
    """Fallback brain for demo when API quota is exceeded."""

    def __init__(self):
        self.conversation = []

    def think(self, user_input: str) -> str:
        self.conversation.append({"role": "user", "content": user_input})

        responses = {
            "what is 2+2": "That's 4. Basic arithmetic, sir.",
            "weather": "I would check the weather for you, but I'm running in demo mode. In production, I'd call the weather API.",
            "open notepad": "Opening Notepad for you, sir. (Demo mode - would launch notepad.exe)",
            "files in": "In demo mode, I'd list your files. Currently showing: Projects/, resume.pdf, notes.txt, Downloads/",
            "create a python": "Creating hello.py with print('Hello from JARVIS!'). Done! Would you like me to run it?",
            "hello": "Hello! I'm JARVIS, your AI assistant. Running in demo mode with Google Gemini (free tier).",
            "help": "I can help with: weather, file operations, opening apps, web search, shell commands, and answering questions.",
            "time": "I'd check the system time, but in demo mode I'll just say: it's presentation time!",
            "status": "System: Windows 11, Python 3.11, Model: gemini-1.5-flash (demo mode), Tools: 8 available"
        }

        user_lower = user_input.lower()
        for key, resp in responses.items():
            if key in user_lower:
                self.conversation.append({"role": "assistant", "content": resp})
                return resp

        default = f"I heard: '{user_input}'. In demo mode, I respond with pre-scripted answers. With a live API key, I'd use Google Gemini to process this and execute tools as needed."
        self.conversation.append({"role": "assistant", "content": default})
        return default

    def reset(self):
        self.conversation = []
        return "Memory cleared. Ready for new commands."


class JarvisBrain:
    """Multi-backend brain: 9router/mimo (primary) -> Gemini (fallback) -> mock (last resort)."""

    def __init__(self):
        if not GEMINI_API_KEY:
            raise ValueError(
                "GEMINI_API_KEY not set!\\n"
                "Create .env file with:\\n"
                "GEMINI_API_KEY=your-key-here"
            )

        self.client = genai.Client(api_key=GEMINI_API_KEY)
        self.model = GEMINI_MODEL
        self.conversation = []
        self._use_mock = False
        self._use_router = JARVIS_USE_9ROUTER
        self._use_groq = JARVIS_USE_GROQ and bool(GROQ_API_KEY)
        self._use_cerebras = JARVIS_USE_CEREBRAS and bool(CEREBRAS_API_KEY)
        if self._use_groq:
            print(f"[JARVIS] Groq backend enabled ({GROQ_MODEL}).", flush=True)
        if self._use_cerebras:
            print(f"[JARVIS] Cerebras backend enabled ({CEREBRAS_MODEL}).", flush=True)
        self.last_backend = None

    def think(self, user_input: str) -> str:
        """Route to 9router (mimo) first; then Groq (free tier); then Gemini; then mock."""
        self.conversation.append({"role": "user", "content": user_input})

        # 1) Primary: 9router / mimo
        if self._use_router:
            try:
                result = self._think_router(user_input)
                if result:
                    self.last_backend = "router"
                    return result
            except Exception as e:
                print(f"[JARVIS] 9router/mimo unavailable ({e}); falling back...")

        # 2) Groq (OpenAI-compatible free tier) — spares the Gemini quota
        if self._use_groq:
            try:
                result = self._think_groq(user_input)
                if result:
                    self.last_backend = "groq"
                    return result
            except Exception as e:
                err = str(e)
                if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                    print("[JARVIS] Groq quota exceeded, falling back to Cerebras...")
                else:
                    print(f"[JARVIS] Groq unavailable ({e}); falling back to Cerebras...")

        # 2b) Cerebras (OpenAI-compatible free tier) — backstop if Groq is walled
        if self._use_cerebras:
            try:
                result = self._think_cerebras(user_input)
                if result:
                    self.last_backend = "cerebras"
                    return result
            except Exception as e:
                err = str(e)
                if "429" in err or "quota" in err.lower() or "rate" in err.lower():
                    print("[JARVIS] Cerebras quota exceeded, falling back to Gemini...")
                else:
                    print(f"[JARVIS] Cerebras unavailable ({e}); falling back to Gemini...")

        # 3) Fallback: Gemini
        try:
            result = self._think_gemini(user_input)
            if result:
                self.last_backend = "gemini"
                return result
        except Exception as e:
            err = str(e)
            if "429" in err or "RESOURCE_EXHAUSTED" in err or "quota" in err.lower():
                print("[JARVIS] Gemini quota exceeded, switching to demo mode...")
                self._use_mock = True
                self._mock_brain = MockJarvisBrain()
                self._mock_brain.conversation = self.conversation.copy()
                self.last_backend = "mock"
                return self._mock_brain.think(user_input)
            return f"I encountered an error: {err}"

        return "I apologize, but I encountered an issue processing that request."

    def _think_router(self, user_input: str) -> str:
        """Call 9router (OpenAI-compatible) with mimo model + tool loop."""
        from openai import OpenAI

        client = OpenAI(base_url=ROUTER_BASE_URL, api_key=ROUTER_API_KEY)
        otools = _openai_tools()

        # Build OpenAI-style messages from shared conversation history
        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                # assistant turn may carry tool_calls (from a prior router turn)
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"]
                    })

        pending = []
        for _ in range(10):
            response = client.chat.completions.create(
                model=ROUTER_MODEL,
                messages=messages,
                tools=otools,
                tool_choice="auto",
                max_tokens=1024,
                timeout=45,
            )

            if not response.choices:
                return None
            message = response.choices[0].message

            if message.tool_calls:
                assistant_block = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments)
                    } for tc in message.tool_calls]
                }
                pending.append(assistant_block)
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls]
                })

                for tc in message.tool_calls:
                    result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                          user_input)
                    pending.append({
                        "role": "tool",
                        "content": [{
                            "name": tc.function.name,
                            "content": result,
                            "tool_call_id": tc.id
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
                continue

            text = _clean_for_speech((message.content or "").strip())
            if not text:
                return None
            pending.append({"role": "assistant", "content": text})
            self.conversation.extend(pending)
            return text

        return None

    def _think_groq(self, user_input: str) -> str:
        """Call Groq (OpenAI-compatible) with tool loop. Same wiring as the
        9router path, just a different base_url/key/model."""
        from openai import OpenAI

        if not GROQ_API_KEY:
            raise RuntimeError("GROQ_API_KEY not available")

        client = OpenAI(base_url=GROQ_BASE_URL, api_key=GROQ_API_KEY)
        otools = _openai_tools()

        # Build OpenAI-style messages from shared conversation history
        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"]
                    })

        pending = []
        for _ in range(10):
            response = client.chat.completions.create(
                model=GROQ_MODEL,
                messages=messages,
                tools=otools,
                tool_choice="auto",
                max_tokens=1024,
                timeout=45,
            )

            if not response.choices:
                return None
            message = response.choices[0].message

            if message.tool_calls:
                assistant_block = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments)
                    } for tc in message.tool_calls]
                }
                pending.append(assistant_block)
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls]
                })

                for tc in message.tool_calls:
                    result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                          user_input)
                    pending.append({
                        "role": "tool",
                        "content": [{
                            "name": tc.function.name,
                            "content": result,
                            "tool_call_id": tc.id
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
                continue

            text = _clean_for_speech((message.content or "").strip())
            if not text:
                return None
            pending.append({"role": "assistant", "content": text})
            self.conversation.extend(pending)
            return text

        return None

    def _think_cerebras(self, user_input: str) -> str:
        """Call Cerebras (OpenAI-compatible) with tool loop. Same wiring as the
        router/Groq paths, just a different base_url/key/model."""
        from openai import OpenAI

        if not CEREBRAS_API_KEY:
            raise RuntimeError("CEREBRAS_API_KEY not available")

        client = OpenAI(base_url=CEREBRAS_BASE_URL, api_key=CEREBRAS_API_KEY)
        otools = _openai_tools()

        # Build OpenAI-style messages from shared conversation history
        messages = [{"role": "system", "content": JARVIS_SYSTEM}]
        for msg in self.conversation:
            if msg["role"] == "user":
                messages.append({"role": "user", "content": msg["content"]})
            elif msg["role"] == "assistant":
                if msg.get("tool_calls"):
                    messages.append({
                        "role": "assistant",
                        "content": msg.get("content") or "",
                        "tool_calls": [
                            {
                                "id": tc["id"],
                                "type": "function",
                                "function": {"name": tc["name"], "arguments": json.dumps(tc["arguments"])}
                            } for tc in msg["tool_calls"]
                        ]
                    })
                else:
                    messages.append({"role": "assistant", "content": msg["content"]})
            elif msg["role"] == "tool":
                for tr in msg["content"]:
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tr["tool_call_id"],
                        "content": tr["content"]
                    })

        pending = []
        for _ in range(10):
            response = client.chat.completions.create(
                model=CEREBRAS_MODEL,
                messages=messages,
                tools=otools,
                tool_choice="auto",
                max_tokens=1024,
                timeout=45,
            )

            if not response.choices:
                return None
            message = response.choices[0].message

            if message.tool_calls:
                assistant_block = {
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "name": tc.function.name,
                        "arguments": _safe_json(tc.function.arguments)
                    } for tc in message.tool_calls]
                }
                pending.append(assistant_block)
                messages.append({
                    "role": "assistant",
                    "content": message.content or "",
                    "tool_calls": [{
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.function.name, "arguments": tc.function.arguments}
                    } for tc in message.tool_calls]
                })

                for tc in message.tool_calls:
                    result = execute_tool(tc.function.name, _safe_json(tc.function.arguments),
                                          user_input)
                    pending.append({
                        "role": "tool",
                        "content": [{
                            "name": tc.function.name,
                            "content": result,
                            "tool_call_id": tc.id
                        }]
                    })
                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "content": result
                    })
                continue

            text = _clean_for_speech((message.content or "").strip())
            if not text:
                return None
            pending.append({"role": "assistant", "content": text})
            self.conversation.extend(pending)
            return text

        return None

    def _think_gemini(self, user_input: str) -> str:
        """Original Gemini path. User message is already in self.conversation."""
        max_iterations = 10
        for _ in range(max_iterations):
            try:
                contents = []
                for msg in self.conversation:
                    if msg["role"] == "user":
                        contents.append(types.Content(role="user", parts=[types.Part(text=msg["content"])]))
                    elif msg["role"] == "assistant":
                        contents.append(types.Content(role="model", parts=[types.Part(text=msg["content"])]))
                    elif msg["role"] == "tool":
                        for tool_result in msg["content"]:
                            contents.append(types.Content(role="user", parts=[
                                types.Part.from_function_response(
                                    name=tool_result["name"],
                                    response={"result": tool_result["content"]}
                                )
                            ]))

                config = types.GenerateContentConfig(
                    system_instruction=JARVIS_SYSTEM,
                    tools=[TOOLS],
                    temperature=0.3,
                    max_output_tokens=2048
                )

                response = self.client.models.generate_content(
                    model=self.model,
                    contents=contents,
                    config=config
                )

                if response.candidates and response.candidates[0].content.parts:
                    function_calls = []
                    text_parts = []

                    for part in response.candidates[0].content.parts:
                        if part.function_call:
                            function_calls.append(part.function_call)
                        elif part.text:
                            text_parts.append(part.text)

                    if function_calls:
                        tool_results = []
                        for fc in function_calls:
                            result = execute_tool(fc.name, dict(fc.args), user_input)
                            tool_results.append({
                                "name": fc.name,
                                "content": result
                            })

                        self.conversation.append({
                            "role": "assistant",
                            "content": "".join(text_parts) if text_parts else ""
                        })
                        for tr in tool_results:
                            self.conversation.append({
                                "role": "tool",
                                "content": [tr]
                            })
                        continue

                    text = _clean_for_speech("".join(text_parts))
                    self.conversation.append({"role": "assistant", "content": text})
                    return text

            except Exception as e:
                error_str = str(e)
                if "429" in error_str or "RESOURCE_EXHAUSTED" in error_str or "quota" in error_str.lower():
                    raise  # let think() handle mock fallback
                return f"I encountered an error: {error_str}"

        return "I apologize, but I encountered an issue processing that request."

    def reset(self):
        """Clear conversation history."""
        self.conversation = []
        self._use_mock = False
        self._use_router = JARVIS_USE_9ROUTER
        self.last_backend = None
        if hasattr(self, '_mock_brain'):
            self._mock_brain.reset()
        return "Memory cleared. Ready for new commands."
