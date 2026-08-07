#!/usr/bin/env python3
import os

# Change to the correct directory
os.chdir('/c/Users/deped/Documents/jarvis-demo')

print("Current directory:", os.getcwd())
print("\nLooking for jarvis.py...")

if os.path.exists('jarvis.py'):
    print("✓ Found jarvis.py")
    with open('jarvis.py', 'r') as f:
        content = f.read()
        print(f"\nFile size: {len(content)} characters")
        
        # Check if TextInterface is in jarvis.py
        if 'class TextInterface:' in content:
            print("\n✓ Found 'class TextInterface:' in jarvis.py")
        else:
            print("\n✗ 'class TextInterface:' NOT in jarvis.py")
            
        # Check if voice_engine.py has TextInterface
        if os.path.exists('voice_engine.py'):
            with open('voice_engine.py', 'r') as f2:
                voice_content = f2.read()
                if 'class TextInterface:' in voice_content:
                    print("✓ 'class TextInterface:' found in voice_engine.py")
                    
                    # Extract the listen_for_command method
                    method_start = voice_content.find('def listen_for_command(self, timeout=None):')
                    if method_start != -1:
                        print("✓ Found 'def listen_for_command'")
                        
                        # Get the method
                        method_end = voice_content.find('\n    def ', method_start + 1)
                        if method_end == -1:
                            method_end = len(voice_content)
                        
                        method_code = voice_content[method_start:method_end]
                        print("\n--- listen_for_command method ---")
                        print(method_code)
                        
                        # Check for the specific bug
                        if 'input("\\n🎤 You: ")' in method_code:
                            print("\n--- BUG ANALYSIS ---")
                            print("Found: input('\\n🎤 You: ') with .strip()")
                            print("\nThe issue is likely:")
                            print("1. input() reads from stdin")
                            print("2. .strip() removes whitespace")
                            print("3. 'cmd if cmd else None' returns None for empty strings")
                            print("4. BUT the bug says 'reads empty lines repeatedly'")
                            print("\nPossible cause:")
                            print("The .strip() might not be working as expected")
                            print("or there's an issue with how the input loop is structured")
                        
                        # Simulate the exact behavior
                        print("\n--- SIMULATION ---")
                        print("Testing various inputs:")
                        
                        test_inputs = [
                            "hello",      # Normal input
                            "",           # Empty line
                            "   ",        # Whitespace only
                            "\t\t",       # Tabs only
                        ]
                        
                        for test_input in test_inputs:
                            cmd = test_input.strip()
                            result = cmd if cmd else None
                            print(f"  Input: {repr(test_input)} -> cmd.strip(): {repr(cmd)} -> returns: {repr(result)}")
                else:
                    print("\n✗ 'class TextInterface:' NOT found in voice_engine.py")
        else:
            print("\n✗ voice_engine.py not found")