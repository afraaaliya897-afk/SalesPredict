# Multi-LLM Provider Setup Guide

This guide explains how to use Claude (Anthropic), GPT (OpenAI), and Ollama models with the Sales Prediction platform.

## Overview

The platform now supports 4 different LLM models across 3 providers:

| Model | Provider | Type | Notes |
|-------|----------|------|-------|
| **Llama 3.2 3B** | Ollama | Local | Fastest - best for quick queries |
| **Qwen 2.5 7B** | Ollama | Local | Stronger SQL - slower but more accurate |
| **GPT-4o Mini** | OpenAI | Cloud | Fast cloud model - good balance |
| **Claude 3.5 Sonnet** | Anthropic | Cloud | Most capable - best for complex queries |

## Quick Start

### Step 1: Install Dependencies

```bash
pip install -r backend/requirements.txt
```

This installs:
- `openai` - OpenAI API client
- `anthropic` - Anthropic API client  
- `python-dotenv` - Environment variable management

### Step 2: Configure API Keys (Optional)

To use cloud models (GPT or Claude), create a `.env` file in the project root:

```bash
# Copy the example file
cp .env.example .env

# Edit .env with your API keys
```

Add your API keys to `.env`:

```env
# OpenAI API Key (for GPT models)
OPENAI_API_KEY=sk-proj-...your-key-here...

# Anthropic API Key (for Claude models)  
ANTHROPIC_API_KEY=sk-ant-...your-key-here...
```

**Get API Keys:**
- OpenAI: https://platform.openai.com/api-keys
- Anthropic: https://console.anthropic.com/settings/keys

⚠️ **Important:** Never commit `.env` to version control (it's in `.gitignore`)

### Step 3: Install Ollama Models (Optional)

For local models, install via Ollama:

```bash
# Install Llama 3.2 (recommended - fastest)
ollama pull llama3.2:3b

# Install Qwen 2.5 (optional - more accurate)
ollama pull qwen2.5:7b
```

## Testing Your Setup

Run the test script to verify everything is working:

```bash
python test_llm_providers.py
```

This will:
- Check which models are available
- Test API connections
- Verify JSON format support
- Show response times

## Usage

### Starting the Application

```bash
# Start the backend server
python backend/main.py
```

Then open http://localhost:8000 in your browser.

### Selecting a Model

In the web UI:
1. Look for the "Model" dropdown in the chat interface
2. Models are grouped by type:
   - **Local Models (Ollama)** - Free, runs on your machine
   - **Cloud Models (API Key Required)** - Requires API key
3. Unavailable models will be disabled with a message:
   - "download required" - Run `ollama pull <model-name>`
   - "API key required" - Add key to `.env` file

### Model Selection Tips

**Use Llama 3.2 3B when:**
- You need fast responses
- Queries are straightforward
- Running on limited hardware

**Use Qwen 2.5 7B when:**
- You need more accurate SQL generation
- Queries are complex
- You have sufficient RAM/GPU

**Use GPT-4o Mini when:**
- You want cloud-based processing
- Need consistent availability
- Balancing cost and performance

**Use Claude 3.5 Sonnet when:**
- Queries are very complex
- Need highest accuracy
- Cost is less important

## Architecture

### How It Works

```
User Question
     ↓
Frontend (model selection)
     ↓
Backend API (backend/main.py)
     ↓
Query Engine (query_engine.py)
     ↓
LLM Router (llm_router.py) ← Handles all providers
     ↓
┌────────┬──────────┬───────────┐
│ Ollama │  OpenAI  │ Anthropic │
└────────┴──────────┴───────────┘
     ↓
SQL Generation (text_to_sql.py)
     ↓
DuckDB Execution
     ↓
Response to User
```

### Key Files

- **`llm_router.py`** - Unified LLM interface for all providers
- **`query_engine.py`** - Query planning and result formatting
- **`text_to_sql.py`** - SQL generation from natural language
- **`backend/main.py`** - FastAPI server
- **`frontend/app.js`** - Model selection UI

## Troubleshooting

### "No models available"

**Problem:** Dropdown shows no ready models

**Solution:**
1. Install at least one Ollama model: `ollama pull llama3.2:3b`
2. Or add API keys to `.env` file

### "Model X requires API key"

**Problem:** Cloud model is disabled

**Solution:**
1. Create `.env` file in project root
2. Add `OPENAI_API_KEY` or `ANTHROPIC_API_KEY`
3. Restart the backend server

### "OpenAI/Anthropic import error"

**Problem:** API client libraries not installed

**Solution:**
```bash
pip install openai anthropic python-dotenv
```

### API Key Not Working

**Problem:** Added API key but model still unavailable

**Solution:**
1. Verify `.env` is in the project root (same directory as `backend/`)
2. Check key format:
   - OpenAI: starts with `sk-proj-` or `sk-`
   - Anthropic: starts with `sk-ant-`
3. Restart the backend server (keys are loaded at startup)
4. Check for typos or extra spaces in `.env`

### Rate Limits / API Errors

**Problem:** Cloud API calls failing

**Solution:**
- Check your API usage/billing on provider dashboard
- OpenAI free tier has limits - upgrade if needed
- Anthropic requires payment setup
- Fall back to Ollama models if APIs are down

## Cost Considerations

### Ollama (Free)
- ✅ Completely free
- ✅ No API limits
- ✅ Runs offline
- ❌ Requires local compute (RAM/GPU)

### OpenAI GPT-4o Mini
- Typical cost: ~$0.001-0.002 per query
- ~500-1000 queries per $1
- Billing: https://platform.openai.com/usage

### Anthropic Claude 3.5 Sonnet
- Typical cost: ~$0.003-0.008 per query  
- ~125-300 queries per $1
- Billing: https://console.anthropic.com/settings/billing

**Recommendation:** Start with Ollama models (free), then add cloud models as needed for complex queries.

## API Key Security

✅ **DO:**
- Store keys in `.env` file
- Keep `.env` in `.gitignore`
- Use `.env.example` as a template (without real keys)
- Rotate keys if accidentally exposed

❌ **DON'T:**
- Commit API keys to git
- Share keys in chat logs
- Hardcode keys in source files
- Use production keys for testing

## Advanced Configuration

### Changing Default Model

Edit `llm_router.py`:

```python
DEFAULT_MODEL = "gpt-4o-mini"  # Change to your preferred model
```

### Adding New Models

Edit `MODEL_REGISTRY` in `llm_router.py`:

```python
MODEL_REGISTRY = {
    "new-model-id": {
        "provider": "openai",  # or "anthropic" or "ollama"
        "label": "Display Name",
        "note": "Description for users",
        "family": "model-family",
    },
}
```

### Custom Ollama Host

Set environment variable:

```bash
export OLLAMA_HOST=http://custom-host:11434
```

## Support

For issues:
1. Run `python test_llm_providers.py` to diagnose
2. Check console logs for errors
3. Verify API keys are valid
4. Try a different model to isolate the issue

## What's Next?

- ✅ Multi-provider support (Ollama, OpenAI, Anthropic)
- ✅ Graceful fallback when models unavailable
- ✅ Model selection UI
- 🔄 Model performance comparison (future)
- 🔄 Streaming responses (future)
- 🔄 Cost tracking per query (future)
