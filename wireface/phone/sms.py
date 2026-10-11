"""Answering text messages: the line agent (``lineagent.py``) as a text model with function calling.

The reply goes back to the number that texted, and only there; the model can't choose a number. Gemini by default
(``phone.models.text``), OpenAI if only an OpenAI key is set.
"""
from __future__ import annotations

import json
import logging

import aiohttp

from .lineagent import Line, Toolbox, instructions, tools

log = logging.getLogger(__name__)

GEMINI = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
OPENAI = "https://api.openai.com/v1/chat/completions"
ROUNDS = 5                 # tool calls before it must answer
REPLY_LIMIT = 600          # characters per reply text


class SmsError(Exception):
    pass


def _gemini_schema(schema: dict) -> dict:
    """JSON schema -> the Gemini API's schema (upper-case types)."""
    out = {}
    for k, v in schema.items():
        if k == "type":
            out[k] = str(v).upper()
        elif k == "properties":
            out[k] = {name: _gemini_schema(p) for name, p in v.items()}
        elif isinstance(v, dict):
            out[k] = _gemini_schema(v)
        else:
            out[k] = v
    if out.get("type") == "OBJECT" and not out.get("properties"):
        out.pop("properties", None)
        out.pop("required", None)
    return out


def history(transcript: list[dict], limit: int = 12) -> list[tuple[str, str]]:
    """(role, text) turns from a thread's transcript: "them" is the user, "agent" the model."""
    turns = [("user" if t["who"] == "them" else "model", t["text"]) for t in transcript if t.get("who") in ("them", "agent")]
    return turns[-limit:]


async def reply(line: Line, settings: dict, toolbox: Toolbox, turns: list[tuple[str, str]], provider: str, key: str,
                model: str) -> str:
    """The line agent's reply to the last text in ``turns`` (after using any tools), or "" for no reply."""
    system = instructions(line, settings)
    fns = tools(line, settings)
    if provider == "gemini":
        return await _gemini(system, fns, turns, toolbox, key, model)
    return await _openai(system, fns, turns, toolbox, key, model)


async def _post(url: str, body: dict, headers: dict) -> dict:
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=60)) as s:
            async with s.post(url, json=body, headers=headers) as r:
                data = await r.json(content_type=None)
                if r.status >= 400:
                    raise SmsError(((data or {}).get("error") or {}).get("message") or f"HTTP {r.status}")
                return data
    except (aiohttp.ClientError, TimeoutError) as e:
        raise SmsError(f"can't reach the text model: {e or type(e).__name__}") from None


async def _gemini(system: str, fns: list[dict], turns, toolbox: Toolbox, key: str, model: str) -> str:
    contents = [{"role": role, "parts": [{"text": text}]} for role, text in turns]
    body = {"systemInstruction": {"parts": [{"text": system}]},
            "tools": [{"functionDeclarations": [{**f, "parameters": _gemini_schema(f["parameters"])} for f in fns]}],
            "toolConfig": {"functionCallingConfig": {"mode": "AUTO"}},
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 1024}}
    for _ in range(ROUNDS):
        data = await _post(GEMINI.format(model=model), {**body, "contents": contents},
                           {"x-goog-api-key": key, "Content-Type": "application/json"})
        cand = (data.get("candidates") or [{}])[0]
        content = cand.get("content") or {"role": "model", "parts": []}
        parts = content.get("parts") or []
        calls = [p["functionCall"] for p in parts if "functionCall" in p]
        if not calls:
            return " ".join(p.get("text", "") for p in parts if p.get("text") and not p.get("thought")).strip()[:REPLY_LIMIT]
        contents.append(content)                     # as returned: Gemini 3 needs its thought signatures back
        contents.append({"role": "user", "parts": [
            {"functionResponse": {"name": c["name"], "response": {"result": toolbox.run(c["name"], c.get("args"))}}}
            for c in calls]})
    return ""


async def _openai(system: str, fns: list[dict], turns, toolbox: Toolbox, key: str, model: str) -> str:
    messages = [{"role": "system", "content": system}] + [
        {"role": "user" if role == "user" else "assistant", "content": text} for role, text in turns]
    body = {"model": model, "tools": [{"type": "function", "function": f} for f in fns]}
    for _ in range(ROUNDS):
        data = await _post(OPENAI, {**body, "messages": messages}, {"Authorization": f"Bearer {key}"})
        msg = (data.get("choices") or [{}])[0].get("message") or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            return (msg.get("content") or "").strip()[:REPLY_LIMIT]
        messages.append(msg)
        for c in calls:
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except ValueError:
                args = {}
            messages.append({"role": "tool", "tool_call_id": c["id"], "content": toolbox.run(c["function"]["name"], args)})
    return ""
