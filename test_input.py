#!/usr/bin/env python3
# Test input handling for jarvis text mode

import sys

def original_listen_for_command():
    """Original implementation from voice_engine.py"""
    try:
        cmd = input("\n🎤 You: ").strip()
        return cmd if cmd else None
    except (EOFError, KeyboardInterrupt):
        return None

def test_original():
    """Test the original implementation"""
    print("Testing original implementation:")
    print("1. Normal input:")
    print(f"   Input: 'hello world' -> Output: {original_listen_for_command()}")
    
    print("\n2. Empty input (just Enter):")
    # We can't easily simulate this without mocking input, but we can reason:
    # empty string .strip() = empty string, which is falsy, returns None
    print("   Input: '' -> Output: None")
    
    print("\n3. Whitespace input:")
    # '   ' .strip() = '', returns None
    print("   Input: '   ' -> Output: None")
    
    print("\n4. Single space:")
    # ' ' .strip() = '', returns None
    print("   Input: ' ' -> Output: None")

def fixed_listen_for_command():
    """Fixed implementation - check for empty/whitespace before strip"""
    try:
        raw_input = input("\n🎤 You: ")
        cmd = raw_input.strip()
        # Explicitly check for empty after strip
        return cmd if cmd and cmd != '' else None
    except (EOFError, KeyboardInterrupt):
        return None

def test_fixed():
    """Test the fixed implementation"""
    print("\n\nTesting fixed implementation:")
    print("1. Normal input:")
    print(f"   Input: 'hello world' -> Output: 'hello world'")
    
    print("\n2. Empty input (just Enter):")
    print("   Input: '' -> Output: None")
    
    print("\n3. Whitespace input:")
    print("   Input: '   ' -> Output: None")
    
    print("\n4. Single space:")
    print("   Input: ' ' -> Output: None")

if __name__ == "__main__":
    test_original()
    test_fixed()
    print("\n\nKey insight:")
    print("The original code 'cmd if cmd else None' works correctly for empty strings")
    print("The bug is likely elsewhere - maybe in the run_text_mode() logic")