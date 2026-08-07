#!/usr/bin/env python3
# More detailed analysis of the stdin loop issue

import sys

# Simulate the exact TextInterface.listen_for_command behavior
def original_listen_for_command():
    """Original implementation from voice_engine.py"""
    try:
        cmd = input("\n🎤 You: ").strip()
        return cmd if cmd else None
    except (EOFError, KeyboardInterrupt):
        return None

# Simulate run_text_mode loop logic
def simulate_loop():
    print("Simulating run_text_mode loop...")
    
    # Test cases
    test_inputs = [
        ("help", "Should show help and continue"),
        ("", "Should break (empty input)"),
        ("  ", "Should break (whitespace only)"),
        ("quit", "Should break"),
        ("normal command", "Should process normally"),
    ]
    
    for test_input, expected in test_inputs:
        print(f"\n--- Test case: {repr(test_input)} ---")
        print(f"Expected: {expected}")
        
        # Mock input for this test
        import builtins
        original_input = builtins.input
        
        def mock_input(prompt):
            return test_input
        
        builtins.input = mock_input
        
        try:
            # Call listen_for_command directly
            result = original_listen_for_command()
            print(f"listen_for_command() returned: {repr(result)}")
            
            # Now simulate the run_text_mode logic
            if result is None:
                print("-> Loop would break (good)")
            else:
                cmd_lower = result.lower().strip()
                if cmd_lower in COMMANDS:
                    print(f"-> Special command handled: {cmd_lower}")
                else:
                    print(f"-> Would process with Gemini: {result}")
                    
        except Exception as e:
            print(f"Error: {e}")
        finally:
            builtins.input = original_input

# COMMANDS dict from jarvis.py for reference
COMMANDS = {
    "help": "Show this help message",
    "quit": "Exit JARVIS",
    "exit": "Exit JARVIS", 
    "clear": "Clear conversation history",
    "status": "Show system status",
}

if __name__ == "__main__":
    simulate_loop()