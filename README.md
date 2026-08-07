# JARVIS - Just A Rather Very Intelligent System
Voice-activated AI assistant with local STT/TTS and Claude brain.

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Set your API key
cp .env.example .env
# Edit .env and add your Anthropic API key

# 3. Run JARVIS
python jarvis.py --text    # Text mode (recommended for demo)
python jarvis.py           # Voice mode (needs microphone)
python jarvis.py --demo    # Pre-scripted demo
```

## Features

- 🎤 **Voice Activation** - Say "Hey Jarvis" to activate
- 🗣️ **Local STT** - Whisper runs 100% locally
- 🔊 **Local TTS** - Edge-TTS (free, no API needed)
- 🧠 **Claude Brain** - Powered by Anthropic's Claude
- 🛠️ **System Control** - Files, apps, shell commands
- 🌐 **Web Search** - Search Google via voice

## Architecture

```
┌─────────────────────────────────────────────────┐
│                    JARVIS                        │
├─────────────────────────────────────────────────┤
│  Voice Engine          │  AI Brain              │
│  ├─ Wake Word (VAD)    │  ├─ Claude API         │
│  ├─ STT (Whisper)      │  ├─ Tool Use           │
│  └─ TTS (Edge-TTS)     │  └─ Memory             │
├─────────────────────────────────────────────────┤
│  System Tools                                    │
│  ├─ Shell Commands     ├─ File Operations       │
│  ├─ App Launcher       ├─ Web Search            │
│  └─ Weather            └─ System Info           │
└─────────────────────────────────────────────────┘
```

## Commands

| Command | Description |
|---------|-------------|
| `help` | Show available commands |
| `clear` | Clear conversation memory |
| `status` | Show system status |
| `quit` | Exit JARVIS |

## Configuration

Edit `config.py` to customize:
- Whisper model size (tiny/base/small/medium/large)
- TTS voice and speed
- Wake word sensitivity
- Max conversation history

## Requirements

- Python 3.10+
- Microphone (for voice mode)
- Anthropic API key (get at console.anthropic.com)

## Demo Script (for Aug 17 presentation)

1. Start with `python jarvis.py --text`
2. Show the architecture diagram
3. Demo: "What's the weather in Manila?"
4. Demo: "List files in my Documents"
5. Demo: "Open Notepad"
6. Demo: "Create a Python hello world program"
7. Show conversation memory with "What did I just ask?"

## Author

Roger Abay Jr - Guest Speaker, August 17, 2026
