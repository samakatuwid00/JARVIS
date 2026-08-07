#!/usr/bin/env python3
# Focus on the actual stdin loop issue

# Read just the key sections of jarvis.py
jarvis_path = '/c/Users/deped/Documents/jarvis-demo/jarvis.py'

print("=== READING JARVIS.PY ===\n")

# Read the entire file to analyze it
with open(jarvis_path, 'r') as f:
    content = f.read()

# Find the TextInterface class in voice_engine.py (not jarvis.py)
voice_path = '/c/Users/deped/Documents/jarvis-demo/voice_engine.py'
with open(voice_path, 'r') as f:
    voice_content = f.read()

print("1. Checking TextInterface in voice_engine.py...")
text_interface_start = voice_content.find('class TextInterface:')
if text_interface_start != -1:
    print("   Found TextInterface class")
    
    # Find the listen_for_command method
    method_start = voice_content.find('def listen_for_command(self, timeout=None):', text_interface_start)
    if method_start != -1:
        print("   Found listen_for_command method")
        
        # Extract the method
        method_end = voice_content.find('\n    def ', method_start + 1)
        if method_end == -1:
            method_end = len(voice_content)
        
        method_code = voice_content[method_start:method_end]
        print("\n   Method code:")
        print("   " + method_code.replace('\n', '\n   '))
        
        print("\n   Analysis:")
        # Check for potential issues
        if 'input("' in method_code and '.strip()' in method_code:
            print("   - Uses input() with strip() - this should handle empty lines")
        if 'cmd if cmd else None' in method_code:
            print("   - Checks 'if cmd else None' - returns None for empty strings")
            print("   - This should fix the 'empty lines' issue")
        
        # Simulate the behavior
        print("\n   Simulating input behavior:")
        test_cases = [
            ("hello", "Should return 'hello'"),
            ("", "Should return None (empty string)"),
            ("  ", "Should return None (whitespace only)"),
            ("\t", "Should return None (tab only)"),
        ]
        
        for input_val, expected in test_cases:
            # Simulate the logic
            cmd = input_val.strip()
            result = cmd if cmd else None
            print(f"   Input: {repr(input_val)} -> cmd.strip(): {repr(cmd)} -> returns: {repr(result)} - {expected}")

print("\n2. Checking run_text_mode loop in jarvis.py...")

# Find run_text_mode in jarvis.py
func_start = content.find('def run_text_mode():')
if func_start != -1:
    print("   Found run_text_mode function")
    
    # Find the main loop
    loop_start = content.find('while True:', func_start)
    if loop_start != -1:
        print("   Found 'while True:' loop")
        
        # Extract loop section
        loop_end = content.find('\n\n', loop_start)
        if loop_end == -1:
            loop_end = loop_start + 1000  # Get first 1000 chars after
        
        loop_section = content[loop_start:loop_end]
        print("\n   Loop section (first 1000 chars):")
        print("   " + loop_section[:1000].replace('\n', '\n   '))
        
        print("\n   Analysis of loop logic:")
        if 'command = voice.listen_for_command()' in loop_section:
            print("   - Calls voice.listen_for_command()")
        if 'if command is None:' in loop_section:
            print("   - Checks 'if command is None:' before processing")
            print("   - This should break the loop on empty input")
            
        # Check what happens after the None check
        if 'continue' in loop_section:
            print("   - Has 'continue' statements for special commands")

print("\n=== CONCLUSION ===\n")
print("Based on the code analysis:")
print("1. TextInterface.listen_for_command() uses: input().strip() -> cmd if cmd else None")
print("2. run_text_mode() checks: if command is None: break")
print("3. This should handle empty lines correctly!")
print("\nBUT the bug report says 'reads empty lines repeatedly'")
print("\nPossible explanations:")
print("1. The code has changed since the bug report")
print("2. There's a different bug elsewhere")
print("3. The simulation isn't accurate")
print("\nLet me check if there's a different implementation...")