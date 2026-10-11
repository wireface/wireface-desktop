"""The realtime voice: a low-latency speech-to-speech model (Gemini Live or OpenAI Realtime) that talks with the
user while the agent (Claude Code, OpenCode, Codex) works in its background session. See ``voice.py``."""
from .gemini import GeminiLink
from .openai import OpenAILink

PROVIDERS = {"gemini": GeminiLink, "openai": OpenAILink}
