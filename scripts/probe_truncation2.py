"""Round 2: realistic history WITH tool results replayed. Ollama keeps system/tools in KV; the
poison is tool-result blocks eating context + model degradation. Also test timeout ceiling."""
import json, sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from openai import OpenAI
import brain_gemini as b

client = OpenAI(base_url="http://localhost:11434/v1", api_key="ollama")
tools = b._openai_tools()
sys_prompt = b.JARVIS_SYSTEM
MODEL = "jarvis-qwen3"

def ask(messages, label):
    t0 = time.time()
    try:
        r = client.chat.completions.create(model=MODEL, messages=messages, tools=tools,
                                           tool_choice="auto", max_tokens=300, timeout=170)
        tc = r.choices[0].message.tool_calls
        txt = (r.choices[0].message.content or "")[:200].replace("\n", " ")
        calls = [c.function.name for c in tc] if tc else []
        print(f"[{label}] tool_calls={calls} reply={txt!r} ({time.time()-t0:.1f}s)")
        return bool(calls)
    except Exception as e:
        print(f"[{label}] ERROR {type(e).__name__}: {str(e)[:120]} ({time.time()-t0:.1f}s)")
        return False

# Realistic history: several turns where JARVIS used tools, big results replayed as tool role.
long = [{"role": "system", "content": sys_prompt}]
for i in range(6):
    long.append({"role": "user", "content": "search my chatgpt history for the irimsv project report"})
    long.append({"role": "assistant", "content": "", "tool_calls": [{
        "id": f"call_{i}", "type": "function",
        "function": {"name": "search_chatgpt_history", "arguments": "{\"query\": \"irimsv project report\"}"}}]})
    long.append({"role": "tool", "tool_call_id": f"call_{i}",
                 "content": f"Found {3+i} conversations. Top hit: 'IRIMS-V Project Report' (Aug 2026): "
                 "quarterly region performance, budget utilization, learning continuity metrics, "
                 "module completion rates by division, teacher training progress, deped system "
                 "updates, infrastructure, procurement status, narrative report draft with "
                 "recommendations for next quarter. Second hit: 'IRIMS-V status update' with "
                 "dashboard changes and data quality notes. Third hit: 'IRIMS-V launch notes'."})
    long.append({"role": "assistant", "content": "Found it. IRIMS-V Project Report has quarterly performance, budget, continuity metrics."})
est = sum(len(m.get("content") or "") // 4 for m in long)
print(f"realistic history est ~{est} tokens")
long.append({"role": "user", "content": "play some calm instrumental music"})

ask(long, "REALISTIC-6TOOLS")
