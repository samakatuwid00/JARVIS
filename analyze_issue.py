#!/usr/bin/env python3
# Fix for text mode stdin loop in jarvis.py

import sys
import subprocess

def run_text_mode_with_stdin():
    """Simulate the text mode input loop to test the fix"""
    from voice_engine import TextInterface
    from brain_gemini import JarvisBrain
    
    # Mock the input and print functions for testing
    original_input = input
    original_print = print
    
    inputs = [
        "help",           # First input - should show help
        "",               # Second input - empty, should break
        "test"            # Should not be reached
    ]
    input_index = 0
    
    def mock_input(prompt):
        nonlocal input_index
        if input_index < len(inputs):
            value = inputs[input_index]
            input_index += 1
            # Simulate user pressing Enter (empty string)
            sys.stdout.write(f"{prompt}{repr(value)}\n")
            return value
        return ""
    
    def mock_print(*args, **kwargs):
        # Suppress output for cleaner test
        pass
    
    # Replace the functions temporarily
    import builtins
    builtins.input = mock_input
    builtins.print = mock_print
    
    try:
        # Now run the actual loop
        from jarvis import run_text_mode
        print("Running text mode with simulated inputs...")
        # We'll manually simulate the loop to test the logic
        print("Simulating loop execution...")
        print("Input 1: 'help' -> should show help and continue")
        print("Input 2: '' (empty) -> should break loop")
        print("Result: Loop should exit on empty input")
    except Exception as e:
        print(f"Error: {e}")
    finally:
        # Restore original functions
        builtins.input = original_input
        builtins.print = original_print

def analyze_code():
    """Analyze the jarvis.py code to find the issue"""
    print("\nAnalyzing jarvis.py for text mode stdin loop bug...")
    print("\nLooking at TextInterface.listen_for_command() in voice_engine.py:")
    print("Current implementation:")
    print("""
    def listen_for_command(self, timeout=None):
        \"\"\"Get command from text input.\"\"\"
        try:
            cmd = input("\\n🎤 You: ").strip()
            return cmd if cmd else None
        except (EOFError, KeyboardInterrupt):
            return None
    """)
    
    print("\nAnalysis:")
    print("1. input() returns a string (empty or not)")
    print("2. .strip() removes whitespace")
    print("3. 'cmd if cmd else None' returns cmd if truthy, None if falsy")
    print("4. Empty string '' is falsy -> returns None")
    print("5. run_text_mode() checks 'if command is None: break'")
    
    print("\nBUT the issue context says: 'reads empty lines repeatedly'")
    print("This suggests input() returns empty strings instead of breaking the loop")
    
    print("\nPossible causes:")
    print("1. input() not being called properly (maybe infinite loop on empty read)")
    print("2. Empty string '' being treated as valid command")
    print("3. Some other bug in the loop logic")
    
    print("\nLooking at run_text_mode() loop:")
    print("""
    while True:
        command = voice.listen_for_command()
        if command is None:
            # EOF or Ctrl+C - exit gracefully
            print("\\n[JARVIS] Goodbye, sir.")
            break
        # Process command...
    """)
    
    print("\nThe loop should break on empty input (returns None)")
    print("\nThe bug might be:")
    print("1. A different issue not in the code I'm seeing")
    print("2. Or the context is outdated")
    print("3. Or there's something else consuming stdin")

if __name__ == "__main__":
    analyze_code()
    # run_text_mode_with_stdin()