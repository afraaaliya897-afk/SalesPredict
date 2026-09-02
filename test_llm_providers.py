"""
Test script to verify LLM provider integration.
Tests Ollama, OpenAI, and Anthropic API connections.
"""

import os
from llm_router import (
    call_llm,
    has_anthropic_key,
    has_openai_key,
    is_model_available,
    list_available_models,
)


def test_model_availability():
    """Test which models are available."""
    print("\n=== Testing Model Availability ===")
    
    print(f"\nOpenAI API Key: {'✓ Set' if has_openai_key() else '✗ Not set'}")
    print(f"Anthropic API Key: {'✓ Set' if has_anthropic_key() else '✗ Not set'}")
    
    print("\nAvailable Models:")
    models = list_available_models()
    for model in models:
        status = "✓ Ready" if model["ready"] else "✗ Not available"
        print(f"  {status} - {model['label']} ({model['id']}) [{model['provider']}]")
        if not model["ready"]:
            if model["status"] == "api_key_required":
                provider = "OpenAI" if model["provider"] == "openai" else "Anthropic"
                print(f"         Needs: {provider} API key in .env file")
            elif model["status"] == "download_required":
                print(f"         Needs: ollama pull {model['id']}")


def test_simple_call(model_id: str):
    """Test a simple LLM call with a specific model."""
    print(f"\n=== Testing {model_id} ===")
    
    if not is_model_available(model_id):
        print(f"✗ Model {model_id} is not available")
        return False
    
    try:
        messages = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Say 'Hello, this is a test!' and nothing else."}
        ]
        
        print(f"Calling {model_id}...")
        result = call_llm(model=model_id, messages=messages, temperature=0.0)
        
        print(f"✓ Success!")
        print(f"  Response: {result['content'][:100]}")
        print(f"  Time: {result['llm_ms']}ms")
        print(f"  Provider: {result['provider']}")
        return True
        
    except Exception as e:
        print(f"✗ Failed: {str(e)}")
        return False


def test_json_format(model_id: str):
    """Test JSON format output."""
    print(f"\n=== Testing JSON Format with {model_id} ===")
    
    if not is_model_available(model_id):
        print(f"✗ Model {model_id} is not available")
        return False
    
    try:
        messages = [
            {"role": "system", "content": "You respond with valid JSON only."},
            {"role": "user", "content": "Return a JSON object with two fields: 'greeting' (value: 'hello') and 'status' (value: 'success')"}
        ]
        
        print(f"Calling {model_id} with JSON format...")
        result = call_llm(
            model=model_id,
            messages=messages,
            temperature=0.0,
            format_json=True
        )
        
        print(f"✓ Success!")
        print(f"  Response: {result['content'][:200]}")
        
        # Try to parse as JSON
        import json
        json.loads(result['content'])
        print(f"  ✓ Valid JSON")
        return True
        
    except json.JSONDecodeError:
        print(f"  ✗ Invalid JSON (this is expected for some models)")
        return False
    except Exception as e:
        print(f"✗ Failed: {str(e)}")
        return False


def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("  LLM Provider Integration Tests")
    print("=" * 60)
    
    # Test availability
    test_model_availability()
    
    # Test each available model with a simple call
    models = list_available_models()
    available_models = [m for m in models if m["ready"]]
    
    if not available_models:
        print("\n⚠ No models are available. Please:")
        print("  1. Install Ollama models: ollama pull llama3.2:3b")
        print("  2. Or add API keys to .env file")
        return
    
    print(f"\n\nTesting {len(available_models)} available model(s)...")
    
    for model in available_models:
        test_simple_call(model["id"])
    
    # Test JSON format with first available model
    if available_models:
        test_json_format(available_models[0]["id"])
    
    print("\n" + "=" * 60)
    print("  Tests Complete")
    print("=" * 60 + "\n")


if __name__ == "__main__":
    main()
