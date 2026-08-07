#!/usr/bin/env python3
import sys

# Read the actual jarvis.py file to examine it
with open('/c/Users/deped/Documents/jarvis-demo/jarvis.py', 'r') as f:
    jarvis_content = f.read()

print("=== JARVIS.PY ANALYSIS ===\n")

# Look for TextInterface class definition
text_interface_start = jarvis_content.find('class TextInterface:')
if text_interface_start != -1:
    print("Found TextInterface class at position:", text_interface_start)
    
    # Extract a good chunk of the class
    class_end = jarvis_content.find('\n\nclass ', text_interface_start + 1)
    if class_end == -1:
        class_end = len(jarvis_content)
    
    text_interface_code = jarvis_content[text_interface_start:class_end]
    print("\n=== TextInterface class ===")
    print(text_interface_code)
else:
    print("TextInterface class not found in jarvis.py")
    print("\nSearching in voice_engine.py...")
    
    with open('/c/Users/deped/Documents/jarvis-demo/voice_engine.py', 'r') as f:
        voice_content = f.read()
        
    text_interface_start = voice_content.find('class TextInterface:')
    if text_interface_start != -1:
        print("\n=== TextInterface class (from voice_engine.py) ===")
        class_end = voice_content.find('\n\nclass ', text_interface_start + 1)
        if class_end == -1:
            class_end = len(voice_content)
        text_interface_code = voice_content[text_interface_start:class_end]
        print(text_interface_code)

print("\n=== ANALYSIS OF stdin LOOP ===\n")

# Check for the listen_for_command method
if 'def listen_for_command' in text_interface_code:
    print("Found listen_for_command method")
    
    # Extract just this method
    method_start = text_interface_code.find('def listen_for_command')
    if method_start != -1:
        # Find next method or end of class
        next_def = text_interface_code.find('\n    def ', method_start + 1)
        if next_def == -1:
            method_code = text_interface_code[method_start:]
        else:
            method_code = text_interface_code[method_start:next_def]
        
        print("\n=== listen_for_command method ===")
        print(method_code)
        
        print("\n=== POTENTIAL ISSUES ===\n")
        
        # Check for common stdin loop bugs
        if 'input("\n🎤 You: ")' in method_code:
            print("1. Using input() with a prompt - this should work")
            
        if '.strip()' in method_code:
            print("2. Using .strip() to remove whitespace")
            
        if 'cmd if cmd else None' in method_code:
            print("3. Checking 'if cmd else None' - this returns None for empty strings")
            print("   This should fix the 'empty lines' issue")
            
        print("\n=== CONCLUSION ===")
        print("The current code looks CORRECT for handling empty lines:")
        print("  - input() gets the raw input")
        print("  - .strip() removes whitespace")
        print("  - 'if cmd else None' returns None for empty/whitespace-only strings")
        print("  - run_text_mode() checks 'if command is None: break'")
        print("\nBUT the bug report says 'reads empty lines repeatedly'")
        print("This suggests either:")
        print("1. The code I'm looking at is different from what's running")
        print("2. There's another bug somewhere else")
        print("3. The issue has been partially fixed")
        
print("\n=== CHECKING run_text_mode LOOP ===\n")

# Find run_text_mode in jarvis.py
if 'def run_text_mode()' in jarvis_content:
    print("Found run_text_mode function")
    
    # Extract the function
    func_start = jarvis_content.find('def run_text_mode()')
    func_end = jarvis_content.find('\n\ndef ', func_start + 1)
    if func_end == -1:
        func_end = len(jarvis_content)
    
    func_code = jarvis_content[func_start:func_end]
    print("\n=== run_text_mode function (partial) ===")
    
    # Show the main loop
    loop_start = func_code.find('while True:')
    if loop_start != -1:
        loop_code = func_code[loop_start:loop_start + 500]  # First 500 chars of loop
        print(loop_code)