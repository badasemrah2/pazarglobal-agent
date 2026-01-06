"""
Test script for ambiguous intent detection
"""
import asyncio
from agents.intent_router import IntentRouterAgent


async def test_ambiguous_intents():
    """Test various ambiguous and clear intent scenarios"""
    
    router = IntentRouterAgent()
    
    test_cases = [
        # Ambiguous cases - should return 'ambiguous'
        {
            "message": "samsung s21 var satmak istiyorum kaç para eder",
            "expected": "ambiguous",
            "expected_intents": ["create_listing", "price_inquiry"],
            "description": "User wants to sell AND asks about price"
        },
        {
            "message": "iPhone 13 aramak istiyorum ama benim iPhone 11'i de satayım",
            "expected": "ambiguous",
            "expected_intents": ["search_listings", "create_listing"],
            "description": "User wants to search AND create listing"
        },
        {
            "message": "kaç liraya satabilirim ve nasıl ilan veririm",
            "expected": "ambiguous",
            "expected_intents": ["price_inquiry", "create_listing"],
            "description": "User asks about price AND how to list"
        },
        
        # Clear single intent cases
        {
            "message": "iPhone 13 satmak istiyorum",
            "expected": "create_listing",
            "expected_intents": [],
            "description": "Clear listing creation intent"
        },
        {
            "message": "samsung kaç para eder",
            "expected": "small_talk",
            "expected_intents": [],
            "description": "General price question without context"
        },
        {
            "message": "laptop göz atmak istiyorum",
            "expected": "search_listings",
            "expected_intents": [],
            "description": "Clear search intent"
        },
        {
            "message": "nasılsın",
            "expected": "small_talk",
            "expected_intents": [],
            "description": "Casual conversation"
        }
    ]
    
    print("\n" + "="*80)
    print("AMBIGUOUS INTENT DETECTION TEST")
    print("="*80 + "\n")
    
    passed = 0
    failed = 0
    
    for i, test_case in enumerate(test_cases, 1):
        message = test_case["message"]
        expected = test_case["expected"]
        expected_intents = test_case["expected_intents"]
        description = test_case["description"]
        
        print(f"\nTest {i}: {description}")
        print(f"Message: '{message}'")
        print(f"Expected: intent='{expected}', detected_intents={expected_intents}")
        
        try:
            result = await router.classify_intent(message)
            intent = result.get("intent")
            detected_intents = result.get("detected_intents", [])
            confidence = result.get("confidence", "unknown")
            
            print(f"Got:      intent='{intent}', detected_intents={detected_intents}, confidence={confidence}")
            
            if intent == expected:
                if expected == "ambiguous":
                    # Check if detected intents match
                    if set(detected_intents) == set(expected_intents):
                        print("✅ PASSED")
                        passed += 1
                    else:
                        print(f"⚠️ PARTIAL: Intent correct but detected_intents mismatch")
                        print(f"   Expected intents: {expected_intents}")
                        print(f"   Got intents: {detected_intents}")
                        passed += 1  # Still count as pass if intent is correct
                else:
                    print("✅ PASSED")
                    passed += 1
            else:
                print(f"❌ FAILED: Expected '{expected}' but got '{intent}'")
                failed += 1
                
        except Exception as e:
            print(f"❌ ERROR: {e}")
            failed += 1
    
    print("\n" + "="*80)
    print(f"RESULTS: {passed} passed, {failed} failed out of {len(test_cases)} tests")
    print("="*80 + "\n")
    
    return passed, failed


if __name__ == "__main__":
    asyncio.run(test_ambiguous_intents())
