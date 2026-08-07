#!/usr/bin/env python3
import os

# List all files in the directory
print("Listing files in current directory:")
for item in os.listdir('.'):
    if os.path.isfile(item):
        print(f"  File: {item}")
    elif os.path.isdir(item):
        print(f"  Dir: {item}")

# Try to read jarvis.py
jarvis_path = './jarvis.py'
if os.path.exists(jarvis_path):
    print(f"\nFile exists: {jarvis_path}")
    with open(jarvis_path, 'r') as f:
        content = f.read()
        print(f"File size: {len(content)} characters")
        
        # Check for TextInterface
        if 'class TextInterface:' in content:
            print("Found 'class TextInterface:' in jarvis.py")
        else:
            print("TextInterface not found in jarvis.py")
            
        # Check for listen_for_command
        if 'def listen_for_command' in content:
            print("Found 'def listen_for_command' in jarvis.py")
        else:
            print("listen_for_command not found in jarvis.py")
            
        # Check for input() call
        if 'input(' in content:
            print("Found 'input(' in jarvis.py")
            
            # Find input() call
            import re
            input_matches = re.findall(r'input\(.*?\)', content)
            for match in input_matches:
                print(f"  Input call: {match}")
        else:
            print("No 'input(' found in jarvis.py")
            
        # Check for 'if cmd else None'
        if 'cmd if cmd else None' in content:
            print("Found 'cmd if cmd else None' in jarvis.py")
        else:
            print("No 'cmd if cmd else None' found in jarvis.py")
            
        # Check for 'if command is None:'
        if 'if command is None:' in content:
            print("Found 'if command is None:' in jarvis.py")
        else:
            print("No 'if command is None:' found in jarvis.py")
            
else:
    print(f"\nERROR: File {jarvis_path} does not exist!")

# Also check voice_engine.py
voice_path = './voice_engine.py'
if os.path.exists(voice_path):
    print(f"\nFile exists: {voice_path}")
    with open(voice_path, 'r') as f:
        voice_content = f.read()
        
        if 'class TextInterface:' in voice_content:
            print("Found 'class TextInterface:' in voice_engine.py")
            
            # Find listen_for_command method
            method_start = voice_content.find('def listen_for_command(self, timeout=None):')
            if method_start != -1:
                print("Found 'def listen_for_command' in voice_engine.py")
                
                # Extract the method
                method_end = voice_content.find('\n    def ', method_start + 1)
                if method_end == -1:
                    method_end = len(voice_content)
                    
                method_code = voice_content[method_start:method_end]
                print(f"\nlisten_for_command method:\n{method_code}")
                
                # Check for the specific pattern
                if 'input("\\n🎤 You: ")' in method_code:
                    print("\nFOUND: Uses input('\\n🎤 You: ') with .strip()")
                    
                    # Check return statement
                    if 'return cmd if cmd else None' in method_code:
                        print("FOUND: Returns 'cmd if cmd else None'")
                    else:
                        print("NOT FOUND: Returns 'cmd if cmd else None'")
                else:
                    print("\nNOT FOUND: input('\\n🎤 You: ') not found")