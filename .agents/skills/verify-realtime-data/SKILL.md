---
name: verify-realtime-data
description: Ensures agents do not rely on outdated training data. Mandates using scripts or network requests to verify external facts (like available API models, library versions, etc.) before making assumptions.
---

# Real-time Fact Checking & API Verification

## Context
As an AI, your internal training data has a cutoff date and may be outdated (e.g., it may not include newer models released in 2025/2026 like Gemma 4, Nemotron 3 Ultra 550B, or Qwen 3). The user operates in the current real-time environment. Relying on outdated training data leads to frustrating back-and-forth corrections.

## Rules
1. **Never Assume Based on Training Data**: When the user mentions a specific version, API model name, or external fact that contradicts your internal knowledge, **do not correct the user or assume it is a typo**.
2. **Verify via Script/Network First**: Before implementing fallback lists, adding external dependencies, or claiming something "does not exist", you MUST write and execute a short script (e.g., Python `urllib`/`requests`) or a shell command (e.g., `curl`) to fetch the live data and confirm it.
3. **Example - OpenRouter Models**: If modifying OpenRouter models, you must run a script to fetch `https://openrouter.ai/api/v1/models` and dynamically verify if the model ID (e.g., `google/gemma-4-31b-it:free`) actually exists in the current environment before modifying the codebase.
4. **Proactive Verification**: Do your own verification silently in the background using your terminal/execution tools. Do not ask the user "Are you sure this exists?" without having thoroughly checked it programmatically first.
