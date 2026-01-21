"""
Test pagination for search results
"""
import asyncio
import sys
import os
sys.path.insert(0, os.path.dirname(__file__))

from api.webchat import is_show_more_command


def test_show_more_detection():
    """Test show_more command detection"""
    print("Testing show_more command detection...")
    
    test_cases = [
        ("daha fazla", True),
        ("daha fazla göster", True),
        ("devam", True),
        ("devamı", True),
        ("show more", True),
        ("sonraki", True),
        ("diğerleri", True),
        ("next", True),
        ("kalan", True),
        ("kalanları göster", True),
        ("ayakkabı varmı", False),
        ("ilan ver", False),
        ("selam", False),
    ]
    
    passed = 0
    failed = 0
    for msg, expected in test_cases:
        result = is_show_more_command(msg)
        status = "✅" if result == expected else "❌"
        if result == expected:
            passed += 1
        else:
            failed += 1
        print(f"{status} '{msg}' -> {result} (expected: {expected})")
    
    print(f"\n{passed} passed, {failed} failed")
    return failed == 0


if __name__ == "__main__":
    success = test_show_more_detection()
    sys.exit(0 if success else 1)
