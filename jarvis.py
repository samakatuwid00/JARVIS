#!/usr/bin/env python3
"""
JARVIS - Just A Rather Very Intelligent System
Voice-activated AI assistant with local STT/TTS and Google Gemini brain (FREE).

Usage:
    python jarvis.py              # Voice mode (requires microphone)
    python jarvis.py --text       # Text mode (keyboard input)
    python jarvis.py --demo       # Demo mode (no API key needed)

Author: Roger Abay Jr
Date: August 17, 2026
"""

import sys
import os
import argparse

# Add project to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


BANNER = """
    ╔══════════════════════════════════════════════╗
    ║                                              ║
    ║      ██╗ █████╗ ██████╗ ██╗   ██╗██╗███████╗║
    ║      ██║██╔══██╗██╔══██╗██║   ██║██║██╔════╝║
    ║      ██║███████║██████╔╝██║   ██║██║███████╗║
    ║ ██   ██║██╔══██║██╔══██╗╚██╗ ██╔╝██║╚════██║║
    ║ ╚█████╔╝██║  ██║██║  ██║ ╚████╔╝ ██║███████║║
    ║  ╚════╝ ╚═╝  ╚═╝╚═╝  ╚═╝  ╚═══╝  ╚═╝╚══════╝║
    ║                                              ║
    ║   Just A Rather Very Intelligent System       ║
    ║   Voice-Activated AI Assistant                ║
    ║   Powered by Google Gemini (FREE)             ║
    ║                                              ║
    ╚══════════════════════════════════════════════╝
"""

COMMANDS = {
    "help": "Show available commands",
    "clear": "Clear conversation memory",
    "status": "Show system status",
    "quit": "Exit JARVIS",
    "exit": "Exit JARVIS",
}


def print_status(brain, voice):
    """Show system status."""
    from tools import get_system_info
    info = get_system_info()
    print("\n📊 System Status:")
    print(f"   OS: {info['os']} {info['os_version']}")
    print(f"   Python: {info['python']}")
    print(f"   User: {info['user']}")
    print(f"   CWD: {info['cwd']}")
    print(f"   Time: {info['time']}")
    print(f"   Model: {brain.model if hasattr(brain, 'model') else 'gemini-1.5-flash'}")
    print(f"   Conversation turns: {len(brain.conversation) // 2}")
    voice_mode = "Voice" if hasattr(voice, 'whisper') else "Text"
    print(f"   Interface: {voice_mode}")
    print()


def run_voice_mode():
    """Run JARVIS in voice mode with wake word detection."""
    from voice_engine import VoiceEngine
    from brain_gemini import JarvisBrain

    print(BANNER)
    print("[JARVIS] Initializing voice engine...")
    voice = VoiceEngine()

    print("[JARVIS] Initializing AI brain (Gemini)...")
    try:
        brain = JarvisBrain()
    except ValueError as e:
        print(f"\n❌ {e}")
        print("\nGet your free API key at: https://makersuite.google.com/app/apikey")
        return

    print("[JARVIS] Ready! Say 'Hey Jarvis' to begin.\n")

    try:
        while True:
            # Wait for wake word
            if voice.listen_for_wake_word():
                voice.speak("Yes?")

                # Listen for command
                command = voice.listen_for_command()
                if command:
                    # Handle special commands
                    cmd_lower = command.lower().strip()
                    if cmd_lower in ("quit", "exit", "goodbye"):
                        voice.speak("Goodbye, sir.")
                        break
                    elif cmd_lower == "clear":
                        brain.reset()
                        voice.speak("Memory cleared.")
                        continue
                    elif cmd_lower == "status":
                        print_status(brain, voice)
                        continue

                    # Process with Gemini
                    try:
                        response = brain.think(command)
                        voice.speak(response)
                    except Exception as e:
                        voice.speak(f"I encountered an error: {str(e)}")

    except KeyboardInterrupt:
        print("\n[JARVIS] Shutdown initiated.")
    finally:
        print("[JARVIS] Goodbye.")


def run_text_mode():
    """Run JARVIS in text mode (keyboard input)."""
    from voice_engine import TextInterface
    from brain_gemini import JarvisBrain

    print(BANNER)
    print("[JARVIS] Text mode activated.")
    print("[JARVIS] Type your commands. Type 'help' for options.\n")

    voice = TextInterface()

    try:
        brain = JarvisBrain()
    except ValueError as e:
        print(f"\n❌ {e}")
        print("\nGet your free API key at: https://makersuite.google.com/app/apikey")
        return

    print("[JARVIS] Ready!\n")

    while True:
        command = voice.listen_for_command()
        if command is None:
            # EOF or Ctrl+C - exit gracefully
            print("\n[JARVIS] Goodbye, sir.")
            break

        cmd_lower = command.lower().strip()

        # Handle special commands
        if cmd_lower in COMMANDS:
            if cmd_lower in ("quit", "exit"):
                print("\n[JARVIS] Goodbye, sir.")
                break
            elif cmd_lower == "clear":
                brain.reset()
                print("[JARVIS] Memory cleared.")
                continue
            elif cmd_lower == "status":
                print_status(brain, voice)
                continue
            elif cmd_lower == "help":
                print("\n📋 Available Commands:")
                for cmd, desc in COMMANDS.items():
                    print(f"   {cmd:10} - {desc}")
                print("\nOr just talk to Jarvis about anything!\n")
                continue

        # Process with Gemini
        try:
            response = brain.think(command)
            voice.speak(response)
        except Exception as e:
            print(f"\n❌ Error: {str(e)}")


def run_demo_mode():
    """Run a pre-scripted demo (no API key needed)."""
    print(BANNER)
    print("[JARVIS] Demo mode - Aug 17 Guest Speaker Presentation\n")

    demos = [
        ("jarvis", "Good morning. I'm JARVIS, your AI assistant powered by Google's free Gemini API. Ready for the demo, sir."),
        ("", ""),
        ("user", "What's the weather in Manila?"),
        ("jarvis", "Let me check that for you..."),
        ("tool", "get_weather(city='Manila')"),
        ("result", "🌤️ Manila: +33°C Partly cloudy"),
        ("jarvis", "The current weather in Manila is 33 degrees Celsius with partly cloudy skies. A typical warm day in the Philippines."),
        ("", ""),
        ("user", "Open Notepad"),
        ("jarvis", "Opening Notepad for you, sir."),
        ("tool", "open_application(app='notepad.exe')"),
        ("jarvis", "Notepad is now open. Would you like me to create a new file?"),
        ("", ""),
        ("user", "What files are in my Documents folder?"),
        ("jarvis", "Let me list that for you."),
        ("tool", "list_directory(path='~/Documents')"),
        ("result", "📁 Projects/\n📄 resume.pdf\n📄 notes.txt\n📁 Downloads/"),
        ("jarvis", "I found 2 folders and 2 files in your Documents. There's a Projects folder, your resume, some notes, and Downloads."),
        ("", ""),
        ("user", "Create a Python hello world file"),
        ("jarvis", "Creating a hello world Python file for you."),
        ("tool", "write_file(path='~/hello.py', content='print(\"Hello from JARVIS!\")')"),
        ("jarvis", "Created hello.py in your home directory. Want me to run it?"),
        ("", ""),
        ("user", "Run the Python file"),
        ("jarvis", "Running hello.py now."),
        ("tool", "run_shell(command='python ~/hello.py')"),
        ("result", "Hello from JARVIS!"),
        ("jarvis", "Output: Hello from JARVIS! The script executed successfully."),
        ("", ""),
        ("user", "What's my system status?"),
        ("jarvis", "Retrieving system information..."),
        ("tool", "get_system_info()"),
        ("result", "OS: Windows 10 | Python: 3.11.15 | User: deped | Time: 2026-08-17 09:00"),
        ("jarvis", "System status: Windows 10, Python 3.11, running as deped. All systems nominal."),
        ("", ""),
        ("jarvis", "That concludes the demo. JARVIS runs locally with free Gemini API, voice STT/TTS, and 8 built-in tools. Thank you."),
    ]

    for speaker, text in demos:
        if not text:
            print()
            continue

        if speaker == "user":
            print(f"🎤 You: {text}")
        elif speaker == "jarvis":
            print(f"🤖 Jarvis: {text}")
        elif speaker == "tool":
            print(f"   ⚡ Tool: {text}")
        elif speaker == "result":
            print(f"   📋 Result: {text}")

    print("\n" + "="*50)
    print("JARVIS - Free Gemini API | Local STT/TTS | 8 Tools")
    print("Guest Speaker Demo - August 17, 2026")
    print("="*50)


def main():
    parser = argparse.ArgumentParser(
        description="JARVIS - Voice-Activated AI Assistant (Powered by Google Gemini)"
    )
    parser.add_argument(
        "--text", action="store_true",
        help="Run in text mode (keyboard input)"
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Run pre-scripted demo"
    )
    args = parser.parse_args()

    if args.demo:
        run_demo_mode()
    elif args.text:
        run_text_mode()
    else:
        # Try voice mode, fall back to text
        try:
            import sounddevice
            run_voice_mode()
        except (ImportError, OSError) as e:
            print(f"[JARVIS] Voice mode unavailable: {e}")
            print("[JARVIS] Falling back to text mode...\n")
            run_text_mode()


if __name__ == "__main__":
    main()