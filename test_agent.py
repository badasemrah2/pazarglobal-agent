"""
Simple test script to verify agent setup
Run: python test_agent.py
"""
import asyncio
from agents import IntentRouterAgent, ComposerAgent, SmallTalkAgent
from loguru import logger
import sys

# Configure simple logger for testing
logger.remove()
logger.add(sys.stdout, level="INFO")


async def test_intent_router():
    """Test intent classification"""
    print("\n" + "="*50)
    print("Testing Intent Router Agent")
    print("="*50)
    
    router = IntentRouterAgent()
    
    test_messages = [
        "iPhone satmak istiyorum",
        "Laptop aramak istiyorum", 
        "İlanımı yayınla",
        "Merhaba nasılsın"
    ]
    
    for msg in test_messages:
        intent = await router.classify_intent(msg)
        print(f"\nMessage: {msg}")
        print(f"Intent: {intent}")


async def test_small_talk():
    """Test small talk agent"""
    print("\n" + "="*50)
    print("Testing Small Talk Agent")
    print("="*50)
    
    agent = SmallTalkAgent()
    
    test_messages = [
        "Merhaba!",
        "PazarGlobal nedir?",
        "Nasıl ilan oluşturabilirim?"
    ]
    
    for msg in test_messages:
        response = await agent.run_simple(msg)
        print(f"\nUser: {msg}")
        print(f"Agent: {response}")


async def test_composer_workflow():
    """Test composer agent workflow (without actual DB)"""
    print("\n" + "="*50)
    print("Testing Composer Agent Workflow")
    print("="*50)
    
    print("\nNote: This will fail without proper DB setup")
    print("But shows the workflow structure")
    
    composer = ComposerAgent()
    
    # This will fail without Supabase, but shows structure
    try:
        result = await composer.orchestrate_listing_creation(
            user_message="iPhone 13 satmak istiyorum, fiyat 20000 TL",
            user_id="test_user",
            phone_number="+905551234567"
        )
        print(f"\nResult: {result}")
    except Exception as e:
        print(f"\nExpected error (no DB setup): {e}")
        print("This is normal for testing without infrastructure")


async def main():
    """Run all tests"""
    print("\n" + "="*60)
    print("PazarGlobal Agent System - Test Suite")
    print("="*60)
    
    print("\n⚠️  Note: Some tests require proper configuration:")
    print("   - OpenAI API key in .env")
    print("   - Supabase connection (for full workflow tests)")
    print("   - Redis (for state management)")
    
    try:
        # Test 1: Intent Router (requires OpenAI)
        await test_intent_router()
        
        # Test 2: Small Talk (requires OpenAI)
        await test_small_talk()
        
        # Test 3: Composer Workflow (requires everything)
        await test_composer_workflow()
        
        print("\n" + "="*60)
        print("✅ Tests completed!")
        print("="*60)
        
    except Exception as e:
        print(f"\n❌ Test error: {e}")
        print("\nMake sure you have:")
        print("1. Created .env file with OPENAI_API_KEY")
        print("2. Installed dependencies: pip install -r requirements.txt")


if __name__ == "__main__":
    asyncio.run(main())
