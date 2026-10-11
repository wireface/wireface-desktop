"""The room process, with fake faces over a real WebSocket."""
import asyncio
import json

import aiohttp
import pytest

from wireface.room.hub import IDLE_EXIT_S, Hub


class Clock:
    def __init__(self):
        self.now = 5000.0

    def __call__(self):
        return self.now


class Face:
    """A stand-in for a face: connects to the room and records everything it is told."""

    def __init__(self, hub_port, token, name, kind=""):
        self.url, self.name, self.kind = f"ws://127.0.0.1:{hub_port}/room?t={token}", name, kind
        self.log: list[dict] = []
        self._queue: asyncio.Queue = asyncio.Queue()

    async def connect(self, listening=False):
        self.session = aiohttp.ClientSession()
        self.ws = await self.session.ws_connect(self.url)
        await self.ws.send_str(json.dumps({"type": "hello", "name": self.name, "kind": self.kind, "aliases": [], "listening": listening}))
        self.reader = asyncio.create_task(self._read())
        return await self.expect("welcome")

    async def _read(self):
        async for raw in self.ws:
            if raw.type == aiohttp.WSMsgType.TEXT:
                msg = json.loads(raw.data)
                self.log.append(msg)
                self._queue.put_nowait(msg)

    async def expect(self, kind, timeout=3):
        while True:
            msg = await asyncio.wait_for(self._queue.get(), timeout)
            if msg["type"] == kind:
                return msg

    def types(self):
        return [m["type"] for m in self.log if m["type"] not in ("room", "notice", "welcome")]

    async def send(self, **msg):
        await self.ws.send_str(json.dumps(msg))
        await asyncio.sleep(0.05)

    async def close(self):
        await self.ws.close()
        self.reader.cancel()
        await self.session.close()


def run(scenario, **hub_kwargs):
    async def main():
        clock = Clock()
        hub = Hub("test", clock=clock, **hub_kwargs)
        runner, port = await hub.serve()
        faces = []

        async def join(name, kind="", **kw):
            f = Face(port, hub.token, name, kind)
            await f.connect(**kw)
            faces.append(f)
            return f

        try:
            await scenario(hub, join, clock)
        finally:
            for f in faces:
                await f.close()
            await runner.cleanup()

    asyncio.run(main())


async def settle():
    await asyncio.sleep(0.15)


def test_a_question_goes_to_everyone_and_the_answers_take_turns():
    async def scenario(hub, join, clock):
        claude, codex, opencode = await join("Claude", "claude"), await join("Codex", "codex"), await join("OpenCode", "opencode")
        assert hub.hear("is the migration safe?") == "user"
        asks = [await f.expect("ask") for f in (claude, codex, opencode)]
        assert all(a["floor"] is False and "[User]: is the migration safe?" in a["prompt"] for a in asks)
        assert "answers first" in asks[0]["prompt"] and "You are Claude" in asks[0]["prompt"]

        rnd = asks[0]["round"]
        await codex.send(type="claim", round=rnd)                             # codex has something first
        await codex.expect("floor")
        await claude.send(type="claim", round=rnd)                            # claude is too late
        await asyncio.sleep(0.1)
        assert "floor" not in claude.types()

        await claude.send(type="reply", round=rnd, text="my draft, never spoken")
        await codex.send(type="reply", round=rnd, text="Yes, it is safe.")
        await codex.send(type="speech_done")
        c = await claude.expect("contribute")                                 # claude finished first, so it goes first
        assert "[Codex]: Yes, it is safe." in c["prompt"] and "exactly PASS" in c["prompt"]
        assert "my draft" not in c["prompt"]                                  # held answers stay private
        await claude.send(type="reply", round=rnd, text="Check the cache too.")
        await claude.send(type="speech_done")
        o = await opencode.expect("contribute")
        assert "[Codex]: Yes, it is safe." in o["prompt"] and "[Claude]: Check the cache too." in o["prompt"]
        await opencode.send(type="reply", round=rnd, text="PASS", passed=True)
        f = await codex.expect("final")                                        # the first responder has the last word
        assert "[Claude]: Check the cache too." in f["prompt"] and "[OpenCode]" not in f["prompt"]
        assert "Codex]: Yes" not in f["prompt"]                                # it already knows its own words
        await codex.send(type="reply", round=rnd, text="Safe, with the cache caveat.")
        await codex.send(type="speech_done")
        await settle()
        assert hub.sched.round is None
        assert [t for _, t in hub.sched.transcript.lines][-1] == "Safe, with the cache caveat."
        # nobody was ever told to speak while someone else held the floor: exactly one floor/contribute/final at a time
        assert codex.types().count("final") == 1 and opencode.types().count("contribute") == 1

    run(scenario)


def test_a_name_at_the_start_addresses_one_agent_and_the_others_get_it_as_context():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        hub.hear("Codex, review the plan")
        ask = await codex.expect("ask")
        assert ask["floor"] is True and "directly" in ask["prompt"]
        await asyncio.sleep(0.1)
        assert "ask" not in claude.types()                                    # claude was not asked
        await codex.send(type="reply", round=ask["round"], text="Plan looks fine.")
        await codex.send(type="speech_done")
        c = await claude.expect("contribute")
        assert "[User]: Codex, review the plan" in c["prompt"] and "[Codex]: Plan looks fine." in c["prompt"]

    run(scenario)


def test_typing_into_a_face_directs_the_question_to_that_faces_agent():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        await codex.send(type="say", text="what is 2+2?")
        ask = await codex.expect("ask")
        assert ask["floor"] is True and "[User]: what is 2+2?" in ask["prompt"]
        await asyncio.sleep(0.1)
        assert "ask" not in claude.types()

    run(scenario)


def test_saying_a_name_while_an_agent_speaks_stops_everyone_and_that_agent_leads_next():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        hub.hear("tell me about caching")
        ask = await claude.expect("ask")
        await claude.send(type="claim", round=ask["round"])
        await claude.send(type="speaking", state="start", text="Caching stores results so later requests are faster.")
        assert hub._anyone_speaking()
        assert hub.hear("caching stores results so later requests") == "echo"           # the speakers, not the user
        await asyncio.sleep(0.1)
        assert "stop" not in claude.types() and hub.sched.round is not None
        assert hub.hear("Codex") == "interrupt"                                           # the user says a name
        await claude.expect("stop")
        await codex.expect("stop")
        assert hub.sched.round is None and hub._pending_direct[0] == "m2"
        await claude.send(type="speech_done")
        clock.now += 2
        assert hub.hear("what do you think about redis") == "user"
        a = await codex.expect("ask")                                                     # the next thing said goes to Codex
        assert a["floor"] is True and "redis" in a["prompt"]
        await asyncio.sleep(0.1)
        assert claude.types().count("ask") == 1

    run(scenario)


def test_a_name_and_a_question_in_one_breath_is_acted_on_at_once():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        await claude.send(type="speaking", state="start", text="Let me explain how this works in detail.")
        assert hub.hear("codex what about the database indexes") == "interrupt"
        a = await codex.expect("ask")
        assert "database indexes" in a["prompt"] and a["floor"] is True

    run(scenario)


def test_stop_alone_just_stops():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        hub.hear("a question")
        await claude.expect("ask")
        await claude.send(type="speaking", state="start", text="One moment while I look.")
        assert hub.hear("stop") == "interrupt"
        await claude.expect("stop")
        assert hub.sched.round is None

    run(scenario)


def test_talking_over_an_agent_without_a_name_or_stop_is_ignored():
    async def scenario(hub, join, clock):
        claude = await join("Claude", "claude")
        await claude.send(type="speaking", state="start", text="Here is the long explanation of the design.")
        assert hub.hear("what time is it in tokyo") == "ignored"
        assert hub.sched.round is None

    run(scenario)


def test_yes_and_no_answer_the_permission_question_of_the_agent_that_asked():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        await codex.send(type="permission", pending=True)
        hub.hear("yes go ahead")
        a = await codex.expect("answer")
        assert a["allow"] is True
        hub.hear("no")
        assert (await codex.expect("answer"))["allow"] is False
        await asyncio.sleep(0.1)
        assert "ask" not in claude.types() and hub.sched.round is None            # not mistaken for a question
        await codex.send(type="permission", pending=False)
        hub.hear("yes please summarise")
        await claude.expect("ask")

    run(scenario)


def test_duplicate_names_are_told_apart_and_a_member_leaving_is_handled():
    async def scenario(hub, join, clock):
        a, b = await join("Claude", "claude"), await join("Claude", "claude")
        assert [m.name for m in hub.members.values()] == ["Claude", "Claude 2"]
        hub.hear("question")
        ask = await a.expect("ask")
        await a.send(type="claim", round=ask["round"])
        await a.send(type="reply", round=ask["round"], text="Answer.")
        await a.send(type="speech_done")
        await b.expect("contribute")
        await b.close()
        await settle()
        assert hub.sched.round is None                                              # nobody left to add: the round ended
        assert len(hub.members) == 1

    run(scenario)


def test_the_room_closes_when_it_has_been_empty_for_a_while():
    async def scenario(hub, join, clock):
        f = await join("Claude")
        assert hub.tick() is True
        await f.close()
        await settle()
        assert hub.tick() is True
        clock.now += IDLE_EXIT_S + 1
        assert hub.tick() is False

    run(scenario)


def test_unsolicited_speech_takes_turns_and_a_stuck_contributor_is_cut_off():
    async def scenario(hub, join, clock):
        claude, codex = await join("Claude", "claude"), await join("Codex", "codex")
        await claude.send(type="floor_request")
        await claude.expect("floor")
        await codex.send(type="floor_request")
        await asyncio.sleep(0.1)
        assert "floor" not in codex.types()
        await claude.send(type="speech_done")
        await codex.expect("floor")
        await codex.send(type="speech_done")
        clock.now += 1                                                              # the room has gone quiet
        hub.hear("a question")
        ask = await claude.expect("ask")
        await claude.send(type="claim", round=ask["round"])
        await claude.send(type="reply", round=ask["round"], text="Done.")
        await claude.send(type="speech_done")
        await codex.expect("contribute")
        clock.now += 100
        hub.tick()
        await codex.expect("stop")
        assert hub.sched.round is None                                              # nothing left: the round ends

    run(scenario)
