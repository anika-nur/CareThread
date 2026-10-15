"""
llm_client.py — single place to configure which LLM backend CareThread uses.

Backends:
    colby  — Colby DAI LiteLLM proxy (OpenAI-compatible), default
    gemini — Google Gemini API, kept as failover and quality baseline

Switch with LLM_BACKEND in .env. Nothing else in the codebase changes.
"""

import json
import os
import re

from dotenv import load_dotenv

load_dotenv()

BACKEND = os.getenv("LLM_BACKEND", "colby").lower()
COLBY_MODEL = os.getenv("COLBY_MODEL", "qwen-3.8-27b")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
# 2000 cut off briefs for patients with many gaps and alerts mid-JSON.
MAX_TOKENS = 4000


def _call_colby(system_prompt, user_prompt):
    from openai import OpenAI

    client = OpenAI(
        base_url=os.environ["LITELLM_BASE_URL"],
        api_key=os.environ["LITELLM_API_KEY"],
    )

    kwargs = dict(
        model=COLBY_MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=MAX_TOKENS,
        temperature=0,
        # Qwen 3.x thinks by default; the reasoning eats the whole token budget
        # and content comes back None (finish_reason=length). Turn it off.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )

    # Ask for JSON mode. Not every model behind a LiteLLM proxy supports it,
    # so fall back to a plain call rather than crashing the batch run.
    try:
        response = client.chat.completions.create(
            response_format={"type": "json_object"}, **kwargs
        )
    except Exception:
        response = client.chat.completions.create(**kwargs)

    usage = getattr(response, "usage", None)
    finish_reason = response.choices[0].finish_reason
    meta = {
        "backend": "colby",
        "model": COLBY_MODEL,
        "input_tokens": getattr(usage, "prompt_tokens", None),
        "output_tokens": getattr(usage, "completion_tokens", None),
        # Labels a cut-off response, which otherwise looks the same as
        # malformed JSON once it fails to parse.
        "finish_reason": finish_reason,
        "truncated": finish_reason == "length",
    }
    return response.choices[0].message.content, meta


def _call_gemini(system_prompt, user_prompt):
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            response_mime_type="application/json",
            max_output_tokens=MAX_TOKENS,
        ),
    )
    usage = getattr(response, "usage_metadata", None)
    candidates = getattr(response, "candidates", None) or []
    reason = getattr(candidates[0], "finish_reason", None) if candidates else None
    finish_reason = getattr(reason, "name", reason)   # enum → "MAX_TOKENS"
    meta = {
        "backend": "gemini",
        "model": GEMINI_MODEL,
        "input_tokens": getattr(usage, "prompt_token_count", None),
        "output_tokens": getattr(usage, "candidates_token_count", None),
        "finish_reason": finish_reason,
        "truncated": finish_reason == "MAX_TOKENS",
    }
    return response.text, meta


def call_llm(system_prompt, user_prompt):
    """Returns (raw_text, meta_dict) from whichever backend is configured."""
    if BACKEND == "colby":
        return _call_colby(system_prompt, user_prompt)
    if BACKEND == "gemini":
        return _call_gemini(system_prompt, user_prompt)
    raise ValueError(f"Unknown LLM_BACKEND: {BACKEND}")


def extract_json(raw_text):
    """
    Pull a JSON object out of a model response.

    Gemini's response_mime_type guaranteed clean JSON. Qwen behind the Colby
    proxy does not, so handle markdown fences, preamble text, and trailing
    commentary.
    """
    if raw_text is None:
        raise ValueError("empty response")

    text = raw_text.strip()

    # Strip markdown code fences if the model added them
    fence = re.match(r"^```[a-zA-Z]*\s*\n(.*?)\n?```$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost braces
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return json.loads(text[start:end + 1])

    raise json.JSONDecodeError("no JSON object found", text, 0)