#!/usr/bin/env python3
import os

# Check if files exist and display their contents
os.chdir('/c/Users/deped/Documents/jarvis-demo')

print("Checking directory structure...")
print("\nFiles in jarvis-demo:")
for filename in ['jarvis.py', 'voice_engine.py', 'brain_gemini.py', 'tools.py']:
    if os.path.exists(filename):
        print(f"✓ {filename}")
    else:
        print(f"✗ {filename}")

# Now read and analyze the actual files
print("\n" + "="*50)
print("ANALYZING TEXT MODE stdin LOOP")
print("="*50)

# First check voice_engine.py for TextInterface
if os.path.exists('voice_engine.py'):
    print("\n1. Reading voice_engine.py...")
    with open('voice_engine.py', 'r') as f:
        voice_content = f.read()
        
    if 'class TextInterface:' in voice_content:
        print("   ✓ Found 'class TextInterface:'")
        
        # Find listen_for_command method
        method_start = voice_content.find('def listen_for_command(self, timeout=None):')
        if method_start != -1:
            print("   ✓ Found 'def listen_for_command'")
            
            # Extract method
            method_end = voice_content.find('\n    def ', method_start + 1)
            if method_end == -1:
                method_end = len(voice_content)
                
            method_code = voice_content[method_start:method_end]
            print("\n   Method code:")
            print("   " + method_code.replace('\n', '\n   '))
            
            # Check for the bug
            print("\n   Bug analysis:")
            if 'input("\\n🎤 You: ")' in method_code:
                print("   ✓ Uses input('\\n🎤 You: ')")
                if '.strip()' in method_code:
                    print("   ✓ Uses .strip() to remove whitespace")
                if 'return cmd if cmd else None' in method_code:
                    print("   ✓ Returns 'cmd if cmd else None' - this should handle empty lines!")
                    
                    # The bug might be elsewhere
                    print("\n   ✗ BUT the bug report says 'reads empty lines repeatedly'")
                    print("   This suggests the .strip()/else None logic isn't working")
                    print("   Possible causes:")
                    print("   1. The .strip() isn't working as expected")
                    print("   2. There's a different stdin loop bug")
                    print("   3. The issue might be in run_text_mode()")
                else:
                    print("   ✗ Does NOT use 'cmd if cmd else None' - this is the bug!")
                    print("   This would cause empty lines to be processed")
            else:
                print("   ✗ Does NOT use input('\\n🎤 You: ') - this is the bug!")
        else:
            print("   ✗ 'def listen_for_command' not found")
    else:
        print("   ✗ 'class TextInterface:' not found in voice_engine.py")
else:
    print("\n✗ voice_engine.py not found")

# Check run_text_mode in jarvis.py
if os.path.exists('jarvis.py'):
    print("\n2. Checking run_text_mode loop in jarvis.py...")
    with open('jarvis.py', 'r') as f:
        jarvis_content = f.read()
        
    # Find run_text_mode
    func_start = jarvis_content.find('def run_text_mode():')
    if func_start != -1:
        print("   ✓ Found 'def run_text_mode()'")
        
        # Find the main loop
        loop_start = jarvis_content.find('while True:', func_start)
        if loop_start != -1:
            print("   ✓ Found 'while True:' loop")
            
            # Extract loop
            loop_end = jarvis_content.find('\n\n', loop_start)
            if loop_end == -1:
                loop_end = loop_start + 1000
                
            loop_section = jarvis_content[loop_start:loop_end]
            print("\n   Loop section:")
            print("   " + loop_section[:500].replace('\n', '\n   '))
            
            # Check for the None check
            if 'if command is None:' in loop_section:
                print("\n   ✓ Has 'if command is None:' check")
                print("   ✓ This should break the loop on empty input")
            else:
                print("\n   ✗ MISSING 'if command is None:' check!")
                print("   ✗ This would cause infinite loop on empty input")
        else:
            print("   ✗ 'while True:' loop not found")
    else:
        print("   ✗ 'def run_text_mode()' not found")
else:
    print("\n✗ jarvis.py not found")