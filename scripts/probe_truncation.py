"""Verify: long conversation -> Ollama head-truncates -> tool schemas lost -> JARVIS asks user to do it.
Short history vs padded history, same request. Does NOT execute tools, does NOT launch Brave."""
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
                                           tool_choice="auto", max_tokens=300, timeout=180)
        tc = r.choices[0].message.tool_calls
        txt = (r.choices[0].message.content or "")[:160].replace("\n", " ")
        calls = [c.function.name for c in tc] if tc else []
        print(f"[{label}] tool_calls={calls} reply={txt!r} ({time.time()-t0:.1f}s)")
        return bool(calls)
    except Exception as e:
        print(f"[{label}] ERROR {e}")
        return False

# --- SHORT history: fresh conversation ---
short = [{"role": "system", "content": sys_prompt},
         {"role": "user", "content": "play some calm instrumental music"}]

# --- LONG history: pad with filler turns to blow past num_ctx 8192 ---
long = [{"role": "system", "content": sys_prompt}]
filler = ("just chatting about the weather today, it is fairly warm and sunny outside, "
          "nothing special happening, typical day, okay fine, moving on, " * 4)  # ~130 tokens
for i in range(70):  # ~9k+ tokens of history
    long.append({"role": "user", "content": filler})
    long.append({"role": "assistant", "content": "sure, it is a nice day indeed, continuing on, yes."})
long.append({"role": "user", "content": "play some calm instrumental music"})

est = sum(len(m["content"]) // 4 for m in long)
print(f"long history est ~{est} tokens (num_ctx 8192, overhead ~3713)")
ok_short = ask(short, "SHORT")
ok_long = ask(long, "LONG")
print("RESULT:", "CONFIRMED - tool call lost with long history" if (ok_short and not ok_long) else
      ("both or neither - see above" if ok_short == ok_long else "partial"))
