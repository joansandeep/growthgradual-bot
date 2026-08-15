"""
Generic JSON-extraction LLM helper — Groq (primary) → Gemini (fallback).

Distinct from routes/report.py's call_groq/call_gemini, which are wired to
the long-form report SYSTEM_PROMPT and report-length heuristics. This one is
for short, structured JSON extractions (e.g. "pull data points out of these
search results") and takes its own system/user prompts per call.
"""
from __future__ import annotations

import json
import logging
import re
import time

import httpx

from utils.keys import (
    get_gemini_keys, get_groq_keys,
    is_rate_limited, mark_rate_limited, round_robin,
)

log = logging.getLogger("llm_extract")

_GEMINI_MODELS = ["gemini-3.6-flash", "gemini-3.5-flash", "gemini-3-flash-preview", "gemini-3.5-flash-lite"]


def _is_rest_api_key(key: str) -> bool:
    return key.startswith("AIzaSy") or key.startswith("AQ.")


def _extract_json(text: str) -> dict | list | None:
    """Best-effort JSON parse: strips code fences, finds the outermost {..} or [..]."""
    if not text:
        return None
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t).strip()
    try:
        return json.loads(t)
    except Exception:
        pass
    # fall back to the widest bracketed span
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = t.find(open_c)
        end = t.rfind(close_c)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(t[start:end + 1])
            except Exception:
                continue
    return None


async def _call_groq(system_prompt: str, user_prompt: str) -> str:
    keys = get_groq_keys()
    if not keys:
        return ""
    for key in round_robin(keys):
        if is_rate_limited(key):
            continue
        try:
            t0 = time.perf_counter()
            async with httpx.AsyncClient(timeout=60) as client:
                res = await client.post(
                    "https://api.groq.com/openai/v1/chat/completions",
                    headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
                    json={
                        "model": "llama-3.3-70b-versatile",
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "max_tokens": 8192,
                        "temperature": 0.1,
                        "response_format": {"type": "json_object"},
                    },
                )
            if res.status_code == 429:
                mark_rate_limited(key, 60_000)
                continue
            if res.status_code in (401, 403):
                mark_rate_limited(key, 24 * 60 * 60_000)
                continue
            if not res.is_success:
                log.warning("Groq extract HTTP %d", res.status_code)
                continue
            data = res.json()
            text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            if text:
                log.info("Groq extract: %.0fms, %d chars", (time.perf_counter() - t0) * 1000, len(text))
                return text
        except Exception as exc:
            log.warning("Groq extract exception: %s", exc)
            continue
    return ""


async def _call_gemini(system_prompt: str, user_prompt: str) -> str:
    keys = [k for k in get_gemini_keys() if _is_rest_api_key(k)]
    if not keys:
        return ""
    for model in _GEMINI_MODELS:
        for key in round_robin(keys):
            if is_rate_limited(key):
                continue
            try:
                async with httpx.AsyncClient(timeout=60) as client:
                    res = await client.post(
                        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}",
                        headers={"Content-Type": "application/json"},
                        json={
                            "contents": [{"role": "user", "parts": [{"text": user_prompt}]}],
                            "systemInstruction": {"parts": [{"text": system_prompt}]},
                            "generationConfig": {
                                "temperature": 0.1,
                                "maxOutputTokens": 8192,
                                "responseMimeType": "application/json",
                            },
                        },
                    )
                if res.status_code == 429:
                    mark_rate_limited(key, 60_000)
                    continue
                if res.status_code in (404, 503):
                    break  # try next model
                if not res.is_success:
                    continue
                data = res.json()
                parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [{}])
                text = "".join(p.get("text", "") for p in parts)
                if text:
                    return text
            except Exception as exc:
                log.warning("Gemini extract exception (%s): %s", model, exc)
                continue
    return ""


async def extract_json(system_prompt: str, user_prompt: str) -> dict | list | None:
    """Runs the extraction prompt through Groq, falling back to Gemini, and
    parses the result as JSON. Returns None if every provider failed or the
    output wasn't valid/parseable JSON."""
    text = await _call_groq(system_prompt, user_prompt)
    if not text:
        text = await _call_gemini(system_prompt, user_prompt)
    if not text:
        log.warning("extract_json: no provider returned output")
        return None
    parsed = _extract_json(text)
    if parsed is None:
        log.warning("extract_json: failed to parse JSON from output (%d chars)", len(text))
    return parsed
