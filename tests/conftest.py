import pytest


class QuietRealtime:
    """Stands in for the realtime voice in tests that build an App: no network, no sound card."""

    def __init__(self, app, **_):
        self.app, self.state, self.error, self.speaker, self.speaking, self.label = app, "off", None, None, False, "Fake"
        self.calls = []

    handles_speech = property(lambda self: self.state == "connected")

    def view(self):
        return {"state": self.state, "provider": "fake", "label": self.label, "error": self.error, "speaking": False}

    async def start(self):
        self.calls.append("start")

    async def stop(self):
        self.calls.append("stop")

    async def restart(self):
        self.calls.append("restart")

    def __getattr__(self, name):                 # activity, permission, typed, halt, cut_off, set_listening, ...
        return lambda *a, **k: self.calls.append(name)


@pytest.fixture(autouse=True)
def no_realtime_voice(monkeypatch):
    import wireface.app

    monkeypatch.setattr(wireface.app, "RealtimeVoice", QuietRealtime)


@pytest.fixture(autouse=True)
def no_judge_network(monkeypatch):
    """The judge never reaches OpenRouter from a test (an OPENROUTER_API_KEY may be set on this PC): tests that judge
    stub ``Judge._post`` or ``Judge.decide``."""
    from wireface import judge

    async def offline(self, body, key):
        raise judge.JudgeUnavailable("no network in tests")

    monkeypatch.setattr(judge.Judge, "_post", offline)
