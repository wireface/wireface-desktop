import numpy as np
import pytest

from wireface import faceconfig
from wireface.backends.claude import describe_request, describe_tool
from wireface.speech import AudioPlayer, SpeechStream, clean_for_speech, speakable


def stream(*deltas):
    s = SpeechStream()
    out = [s.feed(d) for d in deltas]
    return [p for p in out + [s.flush()] if p]


def test_speech_stream_emits_whole_sentences():
    assert stream("Sure. I fi", "xed it. All", " done") == ["Sure.", "I fixed it.", "All done"]


def test_speech_stream_drops_code_blocks_split_across_deltas():
    assert stream("Here it is:\n``", "`py\nprint(1)\n`", "``\nThat's all.") == ["Here it is:", "That's all."]


def test_speech_stream_unclosed_code_block_is_never_spoken():
    assert stream("Look:\n```\nrm -rf /") == ["Look:"]


def test_clean_for_speech():
    assert clean_for_speech("- **Done**: see [the docs](http://x.y) and `hello_world.py`") == \
        "Done: see the docs and hello world.py"
    assert clean_for_speech("before ```code``` after") == "before after"
    assert not speakable(clean_for_speech("***"))


def test_describe():
    assert describe_tool("Read", {"file_path": "B:/x/app.py"}) == "reading app.py"
    assert describe_tool("mcp__server__do_thing", {}) == "using do_thing"
    show, say = describe_request("Bash", {"command": "npm test"})
    assert show == "Run: npm test" and say == "Can I run a command?"
    assert describe_request("Write", {"file_path": "B:/x/app.py"}) == ("Create or overwrite B:/x/app.py", "Can I write app.py?")


def test_face_validate():
    cfg = faceconfig.validate({"preset": "cyan", "glow": 1.5, "mode": "windowed"})
    assert cfg["colors"]["primary"] == "#22d3ee" and cfg["glow"] == 1.5 and cfg["mode"] == "windowed"
    for bad in ({"mode": "hidden"}, {"glow": 5}, {"colors": {"primary": "red"}}, {"nope": 1}, {"skin_amount": 1.5},
                {"style": "cartoon"}, {"photo": None}):
        with pytest.raises(ValueError):
            faceconfig.validate(bad)


def test_face_eye_colour():
    """A photo face keeps its own eye colour until another is chosen; a colour preset leaves the eyes alone."""
    assert faceconfig.DEFAULTS["iris_from_photo"] is True
    cfg = faceconfig.validate({"iris_from_photo": False, "colors": {"iris": "#C0352B"}})
    assert cfg["iris_from_photo"] is False and cfg["colors"]["iris"] == "#c0352b"
    assert faceconfig.validate({"preset": "violet"}, cfg)["colors"]["iris"] == "#c0352b"
    for bad in ({"iris_from_photo": "no"}, {"colors": {"iris": "brown"}}):
        with pytest.raises(ValueError):
            faceconfig.validate(bad)


def test_player_marks_the_end_on_a_block_boundary():
    """Audio that ends exactly where a sound-card block ends must still report where it ended."""
    p = AudioPlayer(24000, stream=False)
    p.write(np.ones(960, np.float32))
    out = np.zeros((480, 1), np.float32)
    p._cb(out, 480, None, None)
    p._cb(out, 480, None, None)
    assert p.clock[2] is None                 # the last block was full: no end known yet
    p._cb(out, 480, None, None)               # ran dry
    assert p.clock[0] == 480 and p.clock[2] == 960
    assert p.pending_s() == 0
