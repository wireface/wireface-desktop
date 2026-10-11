"""The Model setting's choices: models.dev for the agents (cached a day), each service's own list for the realtime voice."""
import json
import os
import time

import pytest

from wireface import catalog, config


def model(mid, name, released="", tools=True, out=("text",)):
    return {"id": mid, "name": name, "release_date": released, "tool_call": tools, "modalities": {"output": list(out)}}


CATALOG = {
    "anthropic": {"name": "Anthropic", "models": {
        "claude-opus-5-5": model("claude-opus-5-5", "Claude Opus 5.5", "2026-09-22"),
        "claude-sonnet-5-5": model("claude-sonnet-5-5", "Claude Sonnet 5.5", "2026-09-28")}},
    "openai": {"name": "OpenAI", "models": {
        "gpt-6-sol": model("gpt-6-sol", "GPT-6 Sol", "2026-09-22"),
        "gpt-realtime-2.1": model("gpt-realtime-2.1", "GPT-Realtime-2.1", out=("text", "audio")),
        "gpt-image-2": model("gpt-image-2", "GPT Image 2", tools=False, out=("image",))}},
    "google": {"name": "Google", "models": {
        "gemini-3.1-flash-live-preview": model("gemini-3.1-flash-live-preview", "Gemini 3.1 Flash Live Preview",
                                               tools=False, out=("text", "audio"))}},
    "opencode": {"name": "OpenCode Zen", "models": {
        "big-pickle": model("big-pickle", "Big Pickle", "2026-10-06"),
        "exo-free": model("exo-free", "Exo Free", "2026-10-06"),                   # hidden: see OPENCODE_HIDDEN
        "gpt-5": model("gpt-5", "GPT-5", "2025-08-07")}},                          # not served by OpenCode here
}


def test_each_agent_gets_its_own_providers_models_newest_first():
    claude = catalog.choices("claude", CATALOG)
    assert [m["id"] for m in claude[0]["models"]] == ["opus", "sonnet", "haiku"]                 # the aliases first
    assert [m["id"] for m in claude[1]["models"]] == ["claude-sonnet-5-5", "claude-opus-5-5"]
    assert [m["id"] for m in catalog.choices("codex", CATALOG)[0]["models"]] == ["gpt-6-sol"]      # agentic text models
    opencode = catalog.choices("opencode", CATALOG)
    assert [g["label"] for g in opencode] == ["OpenCode Zen", "Anthropic", "OpenAI"]               # Google has none here
    assert [m["id"] for m in opencode[0]["models"]] == ["opencode/big-pickle", "opencode/gpt-5"]   # provider/model, hidden dropped
    assert catalog.choices("acme", CATALOG) == []
    assert [g["label"] for g in catalog.choices("claude", {})] == ["Always the newest"]           # never downloaded


def test_opencode_offers_only_the_models_it_can_serve():
    served = {"opencode/big-pickle", "anthropic/claude-opus-5-5", "openai/gpt-6-sol"}
    opencode = catalog.choices("opencode", CATALOG, served)
    assert [g["label"] for g in opencode] == ["OpenCode Zen", "Anthropic", "OpenAI"]
    assert [m["id"] for m in opencode[0]["models"]] == ["opencode/big-pickle"]                 # gpt-5 not served
    assert [m["id"] for m in opencode[1]["models"]] == ["anthropic/claude-opus-5-5"]
    assert [m["id"] for m in opencode[2]["models"]] == ["openai/gpt-6-sol"]
    assert [g["label"] for g in catalog.choices("opencode", CATALOG, set())] == []             # nothing served: no groups
    assert len(catalog.choices("opencode", CATALOG)) == 3                                      # None: the catalog's list


@pytest.fixture
def cache(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA", tmp_path)
    monkeypatch.setattr(catalog, "_memo", None)
    return tmp_path / "cache" / "models.dev.json"


class Response:
    def __init__(self, body):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_the_catalog_is_downloaded_once_a_day_and_kept_for_when_offline(cache, monkeypatch):
    calls = []

    def urlopen(req, timeout):
        calls.append(req.full_url)
        return Response(json.dumps(CATALOG).encode())

    monkeypatch.setattr(catalog.urllib.request, "urlopen", urlopen)
    data, fetched, error = catalog.catalog()
    assert "anthropic" in data and fetched and error is None and cache.exists() and len(calls) == 1
    catalog.catalog()
    assert len(calls) == 1                                       # fresh: no second download
    old = time.time() - catalog.MAX_AGE_S - 60
    os.utime(cache, (old, old))

    def offline(req, timeout):
        raise OSError("no network")

    monkeypatch.setattr(catalog.urllib.request, "urlopen", offline)
    data, fetched, error = catalog.catalog()
    assert "anthropic" in data and "no network" in error          # stale, but better than nothing
    cache.unlink()
    assert catalog.catalog()[0] == {}


def test_the_realtime_voice_lists_what_the_key_can_use(cache, monkeypatch):
    seen = []

    def get_json(url, headers):
        seen.append((url, headers))
        return {"models": [
            {"name": "models/gemini-3.8-live", "displayName": "Gemini 3.8 Live",
             "supportedGenerationMethods": ["bidiGenerateContent"]},
            {"name": "models/gemini-3.8-flash", "supportedGenerationMethods": ["generateContent"]},
            {"name": "models/gemini-3.5-live-translate-preview", "supportedGenerationMethods": ["bidiGenerateContent"]}]}

    monkeypatch.setattr(catalog, "_get_json", get_json)
    groups, error = catalog.realtime_choices("gemini", "Gemini Live", "k123", "gemini-3.8-live", CATALOG)
    assert error is None and [m["id"] for m in groups[0]["models"]] == ["gemini-3.8-live"]
    assert "k123" not in seen[0][0] and seen[0][1] == {"x-goog-api-key": "k123"}        # the key stays out of the URL
    catalog.realtime_choices("gemini", "Gemini Live", "k123", "gemini-3.8-live", CATALOG)
    assert len(seen) == 1                                        # kept on disk for a day
    assert "k123" not in catalog.live_file("gemini").read_text()
    catalog.realtime_choices("gemini", "Gemini Live", "k123", "gemini-3.8-live", CATALOG, refresh=True)
    assert len(seen) == 2
    # no key: the catalog's live models, and the default is always on offer
    groups, error = catalog.realtime_choices("gemini", "Gemini Live", None, "gemini-3.8-live", CATALOG)
    assert "no API key" in error
    assert [m["id"] for m in groups[0]["models"]] == ["gemini-3.8-live", "gemini-3.1-flash-live-preview"]

    def refused(url, headers):
        raise OSError("HTTP Error 403: Forbidden")

    monkeypatch.setattr(catalog, "_get_json", refused)
    groups, error = catalog.realtime_choices("openai", "OpenAI Realtime", "sk-x", "gpt-realtime-2.1", CATALOG)
    assert "403" in error and [m["id"] for m in groups[0]["models"]] == ["gpt-realtime-2.1"]
