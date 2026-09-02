"""
Unified LLM router for Ollama (local), OpenAI, and Anthropic APIs.
Provides a single interface for calling different LLM providers.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# API clients (lazy-loaded)
_openai_client = None
_anthropic_client = None

# Check which APIs are available based on API keys
def has_openai_key() -> bool:
    """Check if OpenAI API key is configured."""
    return bool(os.getenv("OPENAI_API_KEY", "").strip())

def has_anthropic_key() -> bool:
    """Check if Anthropic API key is configured."""
    return bool(os.getenv("ANTHROPIC_API_KEY", "").strip())

def _get_openai_client():
    """Lazy-load OpenAI client."""
    global _openai_client
    if _openai_client is None:
        try:
            from openai import OpenAI
            _openai_client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
        except ImportError:
            raise ImportError("openai package not installed. Run: pip install openai")
    return _openai_client

def _get_anthropic_client():
    """Lazy-load Anthropic client."""
    global _anthropic_client
    if _anthropic_client is None:
        try:
            from anthropic import Anthropic
            _anthropic_client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
        except ImportError:
            raise ImportError("anthropic package not installed. Run: pip install anthropic")
    return _anthropic_client


# Model registry with provider info
MODEL_REGISTRY = {
    "llama3.2:3b": {
        "provider": "ollama",
        "label": "Llama 3.2 3B",
        "note": "Fastest - best for quick queries",
        "family": "llama",
    },
    "qwen2.5:7b": {
        "provider": "ollama",
        "label": "Qwen 2.5 7B",
        "note": "Stronger SQL - slower but more accurate",
        "family": "qwen",
    },
    "gpt-4o-mini": {
        "provider": "openai",
        "label": "GPT-4o Mini",
        "note": "Fast cloud model - good balance",
        "family": "gpt",
    },
    "claude-sonnet-5": {
        "provider": "anthropic",
        "label": "Claude Sonnet 5",
        "note": "Most capable - best for complex queries",
        "family": "claude",
    },
}

# Browser/localStorage and older docs still send these IDs. Anthropic 404s them.
MODEL_ALIASES = {
    "claude-3-5-sonnet-20241022": "claude-sonnet-5",
    "claude-3-5-sonnet-latest": "claude-sonnet-5",
    "claude-3-7-sonnet-20250219": "claude-sonnet-5",
    "claude-sonnet-4-5": "claude-sonnet-5",
    "claude-sonnet-4-6": "claude-sonnet-5",
}
DEFAULT_MODEL = "llama3.2:3b"


def canonical_model(model: str | None) -> str | None:
    if not model:
        return model
    return MODEL_ALIASES.get(model, model)


def get_provider(model: str) -> str:
    """Get the provider name for a given model."""
    model = canonical_model(model) or model
    return MODEL_REGISTRY.get(model, {}).get("provider", "ollama")


def is_model_available(model: str) -> bool:
    """Check if a model is available (API key present or Ollama installed)."""
    model = canonical_model(model) or model
    provider = get_provider(model)
    
    if provider == "ollama":
        try:
            import ollama
            listing = ollama.list()
            models = listing.get("models") if isinstance(listing, dict) else getattr(listing, "models", None)
            names = set()
            for item in models or []:
                name = item.get("model") if isinstance(item, dict) else getattr(item, "model", None)
                if name:
                    names.add(name)
                    names.add(name.split(":")[0])
            return model in names
        except Exception:
            return False
    elif provider == "openai":
        return has_openai_key()
    elif provider == "anthropic":
        return has_anthropic_key()
    
    return False


def call_llm(
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int | None = None,
    format_json: bool = False,
) -> dict[str, Any]:
    """
    Unified interface for calling different LLM providers.
    
    Args:
        model: Model identifier (e.g., "llama3.2:3b", "gpt-4o-mini", "claude-3-5-sonnet-20241022")
        messages: List of message dicts with "role" and "content" keys
        temperature: Sampling temperature (0.0 = deterministic)
        max_tokens: Maximum tokens to generate (None = provider default)
        format_json: Whether to request JSON format output
    
    Returns:
        Dict with:
            - content: The generated text response
            - llm_ms: Elapsed time in milliseconds
            - provider: Which provider was used
            - model: Model identifier used
    """
    model = canonical_model(model) or model
    provider = get_provider(model)
    started = datetime.utcnow()
    
    try:
        if provider == "ollama":
            response_content = _call_ollama(model, messages, temperature, max_tokens, format_json)
        elif provider == "openai":
            response_content = _call_openai(model, messages, temperature, max_tokens, format_json)
        elif provider == "anthropic":
            response_content = _call_anthropic(model, messages, temperature, max_tokens, format_json)
        else:
            raise ValueError(f"Unknown provider: {provider}")
        
        elapsed_ms = round((datetime.utcnow() - started).total_seconds() * 1000, 1)
        
        return {
            "content": response_content,
            "llm_ms": elapsed_ms,
            "provider": provider,
            "model": model,
        }
    
    except Exception as e:
        elapsed_ms = round((datetime.utcnow() - started).total_seconds() * 1000, 1)
        raise Exception(f"LLM call failed ({provider}/{model}): {str(e)}") from e


def _call_ollama(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    format_json: bool,
) -> str:
    """Call Ollama API."""
    import ollama
    
    options = {"temperature": temperature}
    if max_tokens:
        options["num_predict"] = max_tokens
    
    kwargs = {
        "model": model,
        "messages": messages,
        "options": options,
    }
    
    # Add JSON format if requested
    if format_json:
        try:
            response = ollama.chat(**kwargs, format="json")
        except TypeError:
            # Older ollama version doesn't support format parameter
            response = ollama.chat(**kwargs)
    else:
        response = ollama.chat(**kwargs)
    
    return response["message"]["content"].strip()


def _call_openai(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    format_json: bool,
) -> str:
    """Call OpenAI API."""
    client = _get_openai_client()
    
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    
    # Request JSON format if needed
    if format_json:
        kwargs["response_format"] = {"type": "json_object"}
    
    response = client.chat.completions.create(**kwargs)
    return response.choices[0].message.content.strip()


def _call_anthropic(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    format_json: bool,
) -> str:
    """Call Anthropic API."""
    client = _get_anthropic_client()
    
    # Anthropic requires system message to be separate
    system_content = None
    api_messages = []
    
    for msg in messages:
        if msg["role"] == "system":
            # Combine multiple system messages
            if system_content is None:
                system_content = msg["content"]
            else:
                system_content += "\n\n" + msg["content"]
        else:
            api_messages.append(msg)
    
    # If JSON format requested, add instruction to system prompt
    if format_json and system_content:
        system_content += "\n\nRespond with valid JSON only."
    
    kwargs = {
        "model": model,
        "messages": api_messages,
        "max_tokens": max_tokens or 4096,  # Anthropic requires max_tokens
    }
    # Newer models (extended thinking on by default) reject a custom temperature —
    # the API pins it to 1 while thinking is active. Only pass it when the
    # caller actually wants non-default sampling; this app always asks for 0.0.
    if temperature not in (None, 0.0):
        kwargs["temperature"] = temperature

    if system_content:
        kwargs["system"] = system_content

    response = client.messages.create(**kwargs)
    # With extended thinking on, content[0] is a ThinkingBlock, not the reply —
    # find the actual text block instead of assuming a fixed index.
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return block.text.strip()
    raise ValueError("Anthropic response had no text block (thinking-only reply)")


def list_available_models() -> list[dict]:
    """
    Get list of all models with their availability status.
    
    Returns:
        List of dicts with model info and availability status.
    """
    models = []
    
    for model_id, info in MODEL_REGISTRY.items():
        provider = info["provider"]
        ready = is_model_available(model_id)
        
        # Determine availability status
        if provider == "ollama":
            status = "ready" if ready else "download_required"
        else:
            status = "ready" if ready else "api_key_required"
        
        models.append({
            "id": model_id,
            "label": info["label"],
            "note": info["note"],
            "family": info["family"],
            "provider": provider,
            "ready": ready,
            "status": status,
            "default": model_id == DEFAULT_MODEL,
        })
    
    return models


def resolve_model(requested: str | None) -> str:
    """
    Resolve a model request to an available model.
    
    Args:
        requested: Requested model ID or None
    
    Returns:
        Model ID to use (falls back to default if requested is unavailable)
    """
    requested = canonical_model(requested)
    if requested and requested in MODEL_REGISTRY:
        # Check if requested model is available
        if is_model_available(requested):
            return requested
    
    # Fall back to default if available
    if is_model_available(DEFAULT_MODEL):
        return DEFAULT_MODEL
    
    # Try to find any available model
    for model_id in MODEL_REGISTRY:
        if is_model_available(model_id):
            return model_id
    
    # No models available, return default anyway (will fail later with clear error)
    return DEFAULT_MODEL
