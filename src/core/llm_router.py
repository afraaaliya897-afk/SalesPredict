"""
Unified LLM router for Ollama (local), OpenAI, and Anthropic APIs.
Provides a single interface for calling different LLM providers.
"""

from __future__ import annotations

import os
import re
from datetime import datetime
from typing import Any

from dotenv import load_dotenv

load_dotenv()

_openai_client = None
_anthropic_client = None


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


# Local Ollama models first (dropdown order), then cloud APIs.
MODEL_REGISTRY = {
    "llama3.2:3b": {
        "provider": "ollama",
        "label": "Llama 3.2 3B",
        "note": "Fastest local — quick queries",
        "family": "llama",
    },
    "deepseek-r1:1.5b": {
        "provider": "ollama",
        "label": "DeepSeek R1 1.5B",
        "note": "Ultra-fast — simple queries (auto-routed)",
        "family": "deepseek",
    },
    "deepseek-r1:7b": {
        "provider": "ollama",
        "label": "DeepSeek R1 7B",
        "note": "Balanced — medium queries (auto-routed)",
        "family": "deepseek",
    },
    "qwen2.5:7b": {
        "provider": "ollama",
        "label": "Qwen 2.5 7B",
        "note": "Stronger SQL — local",
        "family": "qwen",
    },
    "deepseek-r1:8b": {
        "provider": "ollama",
        "label": "DeepSeek R1 8B",
        "note": "Local reasoning — complex queries (auto-routed)",
        "family": "deepseek",
    },
    "deepseek-r1:14b": {
        "provider": "ollama",
        "label": "DeepSeek R1 14B",
        "note": "Best local SQL/reasoning — needs more RAM",
        "family": "deepseek",
    },
    "gpt-4o-mini": {
        "provider": "openai",
        "label": "GPT-4o Mini",
        "note": "Fast cloud — good balance",
        "family": "gpt",
    },
    "claude-sonnet-5": {
        "provider": "anthropic",
        "label": "Claude Sonnet 5",
        "note": "Cloud — complex queries",
        "family": "claude",
    },
}

# Prefer stronger local models when the requested one is missing.
LOCAL_FALLBACK_ORDER = (
    "deepseek-r1:14b",
    "deepseek-r1:8b",
    "deepseek-r1:7b",
    "deepseek-r1:1.5b",
    "qwen2.5:7b",
    "llama3.2:3b",
)

# Browser/localStorage and older docs still send these IDs.
MODEL_ALIASES = {
    "claude-3-5-sonnet-20241022": "claude-sonnet-5",
    "claude-3-5-sonnet-latest": "claude-sonnet-5",
    "claude-3-7-sonnet-20250219": "claude-sonnet-5",
    "claude-sonnet-4-5": "claude-sonnet-5",
    "claude-sonnet-4-6": "claude-sonnet-5",
    "deepseek-r1": "deepseek-r1:14b",
    "deepseek-r1:latest": "deepseek-r1:14b",
    "deepseek-r1:7b": "deepseek-r1:8b",
    "deepseek": "deepseek-r1:14b",
}

# Safe default on small machines; resolve_model upgrades to DeepSeek/Qwen when pulled.
DEFAULT_MODEL = "llama3.2:3b"


def canonical_model(model: str | None) -> str | None:
    if not model:
        return model
    return MODEL_ALIASES.get(model.strip(), model.strip())


def get_provider(model: str) -> str:
    """Get the provider name for a given model."""
    model = canonical_model(model) or model
    return MODEL_REGISTRY.get(model, {}).get("provider", "ollama")


def _ollama_model_names() -> set[str]:
    """Names Ollama reports (full tags + bare base names)."""
    import ollama

    listing = ollama.list()
    models = listing.get("models") if isinstance(listing, dict) else getattr(listing, "models", None)
    names: set[str] = set()
    for item in models or []:
        name = item.get("model") if isinstance(item, dict) else getattr(item, "model", None)
        if not name:
            name = item.get("name") if isinstance(item, dict) else getattr(item, "name", None)
        if not name:
            continue
        names.add(name)
        names.add(name.split(":")[0])
    return names


def is_model_available(model: str) -> bool:
    """Check if a model is available (API key present or Ollama tag installed)."""
    model = canonical_model(model) or model
    provider = get_provider(model)

    if provider == "ollama":
        try:
            # Full tags are stored (e.g. deepseek-r1:14b); bare names too.
            return model in _ollama_model_names()
        except Exception:
            return False
    if provider == "openai":
        return has_openai_key()
    if provider == "anthropic":
        return has_anthropic_key()
    return False


def _extract_thinking(text: str) -> tuple[str, str | None]:
    """Extract DeepSeek-R1 reasoning blocks and return (cleaned_text, thinking_content)."""
    if not text:
        return text, None
    
    # Extract thinking content
    thinking = None
    tag = "think"
    match = re.search(rf"<{tag}>(.*?)</{tag}>", text, flags=re.DOTALL | re.IGNORECASE)
    if match:
        thinking = match.group(1).strip()
    
    # Remove thinking tags from main content
    cleaned = re.sub(
        rf"<{tag}>.*?</{tag}>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    cleaned = re.sub(
        rf"<{tag}>.*",
        "",
        cleaned,
        flags=re.DOTALL | re.IGNORECASE,
    ).strip()
    
    return cleaned or text.strip(), thinking


def _strip_thinking(text: str, verbose: bool = False) -> str:
    """Remove DeepSeek-R1 / similar reasoning blocks; keep the final answer.
    
    DEPRECATED: Use _extract_thinking() for new code to preserve thinking.
    """
    if not text:
        return text
    
    # Print raw response if verbose
    if verbose and "<think>" in text.lower():
        print("\n" + "="*80)
        print("RAW LLM RESPONSE (with thinking):")
        print("="*80)
        print(text[:2000])  # First 2000 chars
        if len(text) > 2000:
            print(f"\n... ({len(text) - 2000} more characters) ...")
        print("="*80 + "\n")
    
    cleaned, _ = _extract_thinking(text)
    return cleaned


def call_llm(
    model: str,
    messages: list[dict[str, str]],
    temperature: float = 0.0,
    max_tokens: int | None = None,
    format_json: bool = False,
) -> dict[str, Any]:
    """
    Unified interface for calling different LLM providers.

    Returns:
        Dict with content, llm_ms, provider, model.
    """
    model = canonical_model(model) or model
    provider = get_provider(model)
    started = datetime.utcnow()

    # Reasoning models spend many tokens on hidden chain-of-thought before SQL/JSON.
    # For DeepSeek: limit tokens to force conciseness and faster responses
    if max_tokens is not None and "deepseek" in model.lower():
        max_tokens = min(int(max_tokens), 1200)  # Reduced from 2048 for speed
    elif max_tokens is None and "deepseek" in model.lower():
        max_tokens = 1200  # Force shorter, faster responses

    try:
        if provider == "ollama":
            result = _call_ollama(model, messages, temperature, max_tokens, format_json)
        elif provider == "openai":
            result = _call_openai(model, messages, temperature, max_tokens, format_json)
        elif provider == "anthropic":
            result = _call_anthropic(model, messages, temperature, max_tokens, format_json)
        else:
            raise ValueError(f"Unknown provider: {provider}")

        elapsed_ms = round((datetime.utcnow() - started).total_seconds() * 1000, 1)
        return {
            "content": result["content"],
            "thinking": result.get("thinking"),
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
    """Call Ollama API (Llama / Qwen / DeepSeek, etc.)."""
    import ollama

    options: dict[str, Any] = {"temperature": temperature}
    if max_tokens:
        options["num_predict"] = max_tokens

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "options": options,
    }

    if format_json:
        try:
            response = ollama.chat(**kwargs, format="json")
        except TypeError:
            response = ollama.chat(**kwargs)
    else:
        response = ollama.chat(**kwargs)

    content = response["message"]["content"]
    if isinstance(content, list):
        # Some clients return content parts; join text pieces.
        parts = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                parts.append(part.get("text") or "")
            elif isinstance(part, str):
                parts.append(part)
        content = "".join(parts)
    
    # Extract reasoning blocks from DeepSeek / similar.
    # Returns (cleaned_content, thinking_content)
    verbose_logging = os.getenv("LLM_VERBOSE", "").lower() in ("1", "true", "yes")
    content_str = str(content).strip()
    
    if verbose_logging and "<think>" in content_str.lower():
        print("\n" + "="*80)
        print("RAW LLM RESPONSE (with thinking):")
        print("="*80)
        print(content_str[:2000])
        if len(content_str) > 2000:
            print(f"\n... ({len(content_str) - 2000} more characters) ...")
        print("="*80 + "\n")
    
    cleaned, thinking = _extract_thinking(content_str)
    return {"content": cleaned, "thinking": thinking}


def _call_openai(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    format_json: bool,
) -> dict[str, Any]:
    """Call OpenAI API."""
    client = _get_openai_client()
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if max_tokens:
        kwargs["max_tokens"] = max_tokens
    if format_json:
        kwargs["response_format"] = {"type": "json_object"}
    response = client.chat.completions.create(**kwargs)
    return {"content": response.choices[0].message.content.strip(), "thinking": None}


def _call_anthropic(
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int | None,
    format_json: bool,
) -> dict[str, Any]:
    """Call Anthropic API."""
    client = _get_anthropic_client()

    system_content = None
    api_messages = []
    for msg in messages:
        if msg["role"] == "system":
            if system_content is None:
                system_content = msg["content"]
            else:
                system_content += "\n\n" + msg["content"]
        else:
            api_messages.append(msg)

    if format_json and system_content:
        system_content += "\n\nRespond with valid JSON only."

    kwargs: dict[str, Any] = {
        "model": model,
        "messages": api_messages,
        "max_tokens": max_tokens or 4096,
    }
    if temperature not in (None, 0.0):
        kwargs["temperature"] = temperature
    if system_content:
        kwargs["system"] = system_content

    response = client.messages.create(**kwargs)
    for block in response.content:
        if getattr(block, "type", None) == "text":
            return {"content": block.text.strip(), "thinking": None}
    raise ValueError("Anthropic response had no text block (thinking-only reply)")


def list_available_models() -> list[dict]:
    """All registered models with readiness for the UI dropdown."""
    models = []
    for model_id, info in MODEL_REGISTRY.items():
        provider = info["provider"]
        ready = is_model_available(model_id)
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
    Resolve a model id to one that is actually available.

    Preference when falling back: DeepSeek 14B → 8B → Qwen → Llama → any other ready model.
    """
    requested = canonical_model(requested)
    if requested and requested in MODEL_REGISTRY and is_model_available(requested):
        return requested

    for model_id in LOCAL_FALLBACK_ORDER:
        if model_id in MODEL_REGISTRY and is_model_available(model_id):
            return model_id

    for model_id in MODEL_REGISTRY:
        if is_model_available(model_id):
            return model_id

    return DEFAULT_MODEL
