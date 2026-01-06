"""
Test Multi-Intent Chaos Pack
Tests complex real-world scenarios with 2-way and 3-way intent conflicts.
"""

import asyncio
from agents.intent_router import IntentRouterAgent

async def test_chaos_scenarios():
    """Test the 4 scenarios from Chaos Pack."""
    
    router = IntentRouterAgent()
    
    test_cases = [
        {
            "name": "🔁 Scenario 1: Create → Price (iPhone 13)",
            "message": "Bir iPhone 13 satacağım ama kaç para eder önce bi bakabilir miyiz",
            "expected_intent": "ambiguous",
            "expected_detected": ["create_listing", "price_inquiry"],
            "note": "❌ Draft açma, ✅ Clarify zorunlu"
        },
        {
            "name": "🔁 Scenario 2: Price → Search (Samsung S21)",
            "message": "Samsung S21 kaç para ediyor piyasada var mı bakabilir miyiz",
            "expected_intent": "ambiguous",
            "expected_detected": ["price_inquiry", "search_listings"],
            "note": "Kullanıcı kendi ürünü mü, piyasa mı? → Clarify"
        },
        {
            "name": "🔁 Scenario 3: Search → Create (Context-dependent)",
            "message": "Bu fiyata satılanlar varsa ben de ilan gireyim",
            "expected_intent": "ambiguous",
            "expected_detected": ["search_listings", "create_listing"],
            "note": "✅ Önce search, sonra create (onayla)"
        },
        {
            "name": "🔀 Scenario 4: Full Combo (PS5 3'lü)",
            "message": "Evde bir PS5 var satmayı düşünüyorum, kaç para eder, varsa ilanlara da bak",
            "expected_intent": "ambiguous",
            "expected_detected": ["create_listing", "price_inquiry", "search_listings"],
            "note": "❌ Asla otomatik akış, ✅ Menüyle yönlendir"
        },
    ]
    
    print("\n🧩 MULTI-INTENT CHAOS PACK TEST")
    print("=" * 70)
    
    passed = 0
    failed = 0
    
    for test in test_cases:
        print(f"\n{test['name']}")
        print(f"💬 User: {test['message']}")
        print(f"📝 Note: {test['note']}")
        
        result = await router.classify_intent(test["message"])
        
        # Check intent
        intent_match = result["intent"] == test["expected_intent"]
        
        # Check detected_intents
        detected = result.get("detected_intents", [])
        detected_match = set(detected) == set(test["expected_detected"])
        
        # Overall pass/fail
        if intent_match and detected_match:
            print(f"✅ PASS")
            print(f"   Intent: {result['intent']}")
            print(f"   Detected: {detected}")
            passed += 1
        else:
            print(f"❌ FAIL")
            print(f"   Expected: {test['expected_intent']} with {test['expected_detected']}")
            print(f"   Got: {result['intent']} with {detected}")
            failed += 1
    
    print("\n" + "=" * 70)
    print(f"📊 RESULTS: {passed}/{len(test_cases)} passed, {failed} failed")
    
    # Additional tests: Single intent clarity
    print("\n\n🔍 SINGLE INTENT VALIDATION (should NOT be ambiguous)")
    print("=" * 70)
    
    single_tests = [
        ("iPhone 13 satmak istiyorum", "create_listing"),
        ("samsung var mı", "search_listings"),
        ("MacBook kaç para eder", "small_talk"),  # Should route to small_talk or price_inquiry
    ]
    
    for message, expected in single_tests:
        result = await router.classify_intent(message)
        match = result["intent"] == expected or (expected == "small_talk" and result["intent"] == "price_inquiry")
        status = "✅" if match else "❌"
        print(f"{status} \"{message}\" → {result['intent']} (expected: {expected})")

if __name__ == "__main__":
    asyncio.run(test_chaos_scenarios())
