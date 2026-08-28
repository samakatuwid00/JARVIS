"""mcp_browser_server.py — Phase 3: Browser as MCP server (Playwright MCP reference).

Wraps browser_agent's CDP Chrome tools as MCP-style tool definitions so the
capability registry can treat the browser as a registered delegate rather than
hardcoded imports. This is intentionally thin: it re-exports the existing
browser_agent functions with MCP metadata (name, description, input_schema)
mirroring Playwright MCP's tool registry.

Usage:
  from mcp_browser_server import TOOLS, call_tool
  # or as subprocess MCP server: python mcp_browser_server.py

Stdlib + browser_agent only; no new deps.
"""

import json
import sys

try:
    import browser_agent as _ba
except Exception as e:
    _ba = None
    _import_error = str(e)
else:
    _import_error = None

MCP_TOOLS = [
    {
        "name": "open_site",
        "description": "Open a website by name from web_registry.json or raw URL in CDP Chrome new tab",
        "input_schema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Site name or alias"},
                "url": {"type": "string", "description": "Raw URL (optional)"}
            },
            "required": ["name"]
        },
        "handler": "open_site"
    },
    {
        "name": "ask_chatgpt",
        "description": "Type a prompt into ChatGPT (CDP Chrome). Only sends if submit=true",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "submit": {"type": "boolean"}
            },
            "required": ["prompt"]
        },
        "handler": "ask_chatgpt"
    },
    {
        "name": "search_chatgpt_history",
        "description": "Search ChatGPT conversation titles (per-word scoring)",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"}
            },
            "required": ["query"]
        },
        "handler": "search_chatgpt_history"
    },
    {
        "name": "open_chatgpt_conversation",
        "description": "Open best-matching ChatGPT conversation and read messages",
        "input_schema": {
            "type": "object",
            "properties": {
                "title_contains": {"type": "string"}
            },
            "required": ["title_contains"]
        },
        "handler": "open_chatgpt_conversation"
    },
    {
        "name": "browser_status",
        "description": "Report whether CDP Chrome is up and signed in",
        "input_schema": {"type": "object", "properties": {}},
        "handler": "browser_status"
    },
    {
        "name": "ask_web_ai",
        "description": "Write prompt into web AI (chatgpt/gemini/claude/copilot), send only if submit=true",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string"},
                "site": {"type": "string"},
                "submit": {"type": "boolean"}
            },
            "required": ["prompt"]
        },
        "handler": "ask_web_ai"
    },
]

def call_tool(name: str, arguments: dict) -> str:
    """Dispatch to browser_agent. Quarantines external text via guard."""
    if _ba is None:
        return f"[Error] browser_agent unavailable: {_import_error}"
    mapping = {
        "open_site": lambda **kw: _ba.open_site(kw.get("name",""), kw.get("url","")),
        "ask_chatgpt": lambda **kw: _ba.ask_chatgpt(kw.get("prompt",""), kw.get("submit", False)),
        "search_chatgpt_history": lambda **kw: _ba.search_chatgpt_history(kw.get("query",""), int(kw.get("limit",10) or 10)),
        "open_chatgpt_conversation": lambda **kw: _ba.open_chatgpt_conversation(kw.get("title_contains","")),
        "browser_status": lambda **kw: _ba.browser_status(),
        "ask_web_ai": lambda **kw: _ba.ask_web_ai(kw.get("prompt",""), kw.get("site","chatgpt"), kw.get("submit", False)),
    }
    fn = mapping.get(name)
    if not fn:
        return f"[Error] Unknown MCP browser tool: {name}"
    result = fn(**arguments)
    try:
        import guard
        result = guard.sanitize_web_text(result, label=f"mcp:browser:{name}")
    except Exception:
        pass
    return result

def _mcp_serve():
    """Minimal MCP stdio loop: read JSON lines {method, params}, write {result}."""
    for line in sys.stdin:
        line=line.strip()
        if not line:
            continue
        try:
            msg=json.loads(line)
        except Exception:
            sys.stdout.write(json.dumps({"error": "bad json"})+"\n"); sys.stdout.flush(); continue
        method=msg.get("method")
        if method=="list_tools":
            sys.stdout.write(json.dumps({"tools": MCP_TOOLS})+"\n")
        elif method=="call_tool":
            name=msg.get("params",{}).get("name")
            args=msg.get("params",{}).get("arguments",{})
            res=call_tool(name, args)
            sys.stdout.write(json.dumps({"content": [{"type":"text","text":res}]})+"\n")
        else:
            sys.stdout.write(json.dumps({"error": f"unknown method {method}"})+"\n")
        sys.stdout.flush()

if __name__=="__main__":
    if len(sys.argv)>1 and sys.argv[1]=="--list":
        print(json.dumps(MCP_TOOLS, indent=2))
    else:
        _mcp_serve()
