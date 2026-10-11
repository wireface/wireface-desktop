# Judging what the face hears

Without a wake word, everything the microphone transcribes goes to the agent. In the first five days of real use,
143 of the 209 prompts the agent received were six words or fewer and 56 were a single word ("Yeah.", "Uh", "Mm."); many
of the longer ones were conversations in the room that were never meant for it. Each one cost a full agent turn and a
spoken reply.

Two filters, both in **Settings > Voice > Advanced**, both off by default. With both off nothing changes.

    microphone -> noise reduction -> voice detection
               -> room sound: does it stand out from what the room usually sounds like?   (surprise.py, on this PC)
                  "Ignore constant background sound": if not, dropped here, before recognition
               -> speech recognition (Parakeet, on this PC)
               -> the judge: was it said to the agent, and what is it?                     (judge.py, OpenRouter)
                  "Judge what it hears": send / drop / hold / wait for the rest / a command
               -> the agent

## Ignore constant background sound (`wireface/surprise.py`)

`RoomSound` keeps the last minute of what the microphone heard as the energy in 20 frequency bands per 50 ms block.
Each utterance is compared with the room's usual loud level (per band, the level the room is at or below 80% of the
time): `lift_db` is how far the louder half of the utterance rises above it in the speech bands (250-4000 Hz), and
`stands_out` is `strongly` (12 dB or more), `somewhat` (6 dB or more), `no`, or `unknown` (the first 5 s).

Everything heard is learned, so a sound that keeps going (a TV, a radio, music, a room full of people) becomes the
background within a minute, and you talking now and then does not. With the switch on, an utterance that doesn't stand
out is dropped before recognition, unless:

- the face's name was heard in it;
- the face is in a conversation: it spoke, its name was said, or something stood out or was said at your level in the
  last 8 s (`hearing.CONVERSATION_WINDOW_S`);
- it was said at your own level (within 3 dB of what the voice level filter learned, `hearing.OWN_LEVEL_DB`): after a
  minute of dictation your own voice is a large part of what the room sounds like.

With the switch on, your level is only learned from speech that stood out, so a TV heard while the room was still
being learned can't become "your level".

**Measured** with real speech (the speech models' test clips, with a reverberant "TV" playing other clips
continuously):

| case | lift | stands out |
|---|---|---|
| a quiet room, you | +34..+41 dB | strongly, 5 of 5 |
| after a minute of TV, more TV | -15..-3 dB | no, 13 of 13 |
| you 6 dB louder than the TV, over it | +7..+13 dB | strongly or somewhat, 5 of 5 |
| you 12 dB louder than the TV, over it | +12..+19 dB | strongly or somewhat, 5 of 5 |
| a loud steady fan, no noise reduction, you | +10..+16 dB | strongly or somewhat, 5 of 5 |
| after a minute of dictation (60% talking), you | -2..+6 dB | no (hence the own-level rule) |

It can't tell you from someone else at the same distance and level: that is the judge's job.

**Show voice detector** (also under Advanced, off by default) draws the same measurement live, whether or not the
filter is on. `Ears` records, for every 50 ms block, how far it rises above the room's usual level (`RoomSound.now_lift`,
the room's level refreshed every half second), whether the voice detector hears speech, and whether the face was
talking; and for each utterance what became of it (`heard`, `would` (the filter is off but would have dropped it),
`background`, `distant` (the voice level filter), `empty`) with its lift and its level against yours. The hearing thread
sends them every 0.1 s as a `meter` message; `web/face/meter.js` draws them on the face window and in the settings,
which also list the last few utterances. Measured on a real microphone in a quiet room: blocks between -32 and +20 dB,
median -11 (the room's "usual loud level" is its 80th percentile, so a quiet moment is below it).

## Judge what it hears (`wireface/judge.py`)

A decision model reads what was heard and a short description of the moment, and answers three typed questions in one
call: [Jev](https://docs.typesafe.ai) by TypeSafe (`~typesafe/jev-latest`) through OpenRouter's Decisions API
(`POST https://openrouter.ai/api/alpha/decisions`). Jev doesn't write text; it returns calibrated probabilities.

| question | type | used for |
|---|---|---|
| `for_assistant` | yes/no | was it said to the assistant at all? |
| `kind` | choice | task, follow-up, status question, correction, stop, yes / no to the permission question (only while one waits), hide / show the face, compact, new conversation, general question, small talk |
| `complete` | yes/no | is it a whole thought, or cut off mid-sentence? |

**What it is told** (`build_state`): the words; the assistant's name and whether it is busy; how it sounded, in words
(about as loud as the user's usual voice, much quieter, stands out from the room or sounds like the room's background:
Jev is weak with numbers); the moment ("right after the assistant asked the user a question", "after a long quiet
spell"); the last six lines of the conversation; the permission question, if one waits.

**What it does** (`route`, pure and unit-tested):

- for the assistant (0.6 or more, or the sentence starts with its name): carry out a command if sure of the kind
  (0.6; "new conversation" 0.9), else send the words to the agent as before;
- not for it (below 0.25, or below 0.5 when it also sounded like the room's background): dropped;
- in between: held back (or sent, as chosen in "When it isn't sure");
- cut off (complete below 0.3): it waits up to 2 s (longer while you keep talking) for the rest and judges the two
  together.

Held and dropped lines show in the activity panel (and faded on the face's caption) with a **Send** button for 10
minutes. "Stop", "yes" / "no" to a waiting question, a bare name and "go away" / "come back" are still recognised on
this PC first, so they never wait for the network. Typed messages are never judged. When the judge can't be asked (no
key, no network, slower than 2 s) the words go to the agent as before, and a notice says so (once, not every time).

**Measured.**

- Replaying the 209 logged prompts (with the conversation before each, but no sound information, which old logs don't
  have): 75 sent, 114 held, 11 dropped, 5 waited for the rest, 4 commands. Clear requests were sent; most room talk
  was held or dropped; short "Yeah." / "Okay." without knowing how they sounded or what preceded them landed in the
  middle (held). With the sound described live, a background "yeah" scored 0.12 (dropped) and a request at the user's
  level 0.91.
- The same replay with the kinds offered in reverse order (Jev leans towards the first option): the same action for
  201 of 209, `for_assistant` moved by 0.02 on average.
- Latency: median 265 ms, 90th percentile 297 ms (UK to the US West Coast, connection kept open; the first call is
  ~450-600 ms). Cost: about $0.00005 per utterance (~1,100 input tokens), so about 5 cents per 1,000 things heard.

**Privacy.** With the judge on, the words you say (never the sound) and the last few lines of the conversation go to
OpenRouter, which passes them to TypeSafe (hosted in the US). The key is the face's `secrets.json` or
`OPENROUTER_API_KEY`, and it never reaches the page or the activity log.

## Later (not built)

**Phase 2: act on the kind while the agent works.**

- `correction` stops the current request and sends the new one (`App._steer`) without the name having to be said.
- `status_question` goes to the realtime voice only (it answers from its notes), never queued behind the work.
- `general_question` is answered by the realtime voice itself: a new `[say]` request, "answer this yourself, briefly",
  with a carve-out in `realtime/brief.py`'s instructions (and optionally Gemini Live's Google Search tool for current
  facts). It never reaches the agent, so the "every prompt answered twice" problem can't come back.
- Voice commands to switch agent or working folder.
- The room hub (`room/hub.py`) uses the same judge for what it hears.

**Phase 3: several project threads inside one face.**

- `state.json` gains `threads: [{id, title, cwd, backend, session_id}]`; `App` holds several `Backend` instances,
  each with its own folder, inbox and session (today the folder is per face, and changing it starts a new
  conversation).
- Activity events are tagged with their thread.
- A `thread` choice picks between thread cards (title, folder, last request, current step), `new_thread` and
  `not_project_work`; when unsure the face asks ("Is that for the website or the desktop app?").
- Relays name the project; the panel gets a thread switcher.

**Other places a decision model could help.**

- Rooms: who should lead a question, by what each agent is good at; skip comment turns that would only say PASS
  (each is a whole agent turn).
- Narration: is this progress worth saying out loud; does the reply ask the user something (keep the microphone open
  for the answer).
- Recognition: map a misheard word onto the project's file and folder names (a choice between candidates).
- Phone line: sort incoming texts (booking, spam, personal, urgent); screen untrusted text before it reaches the
  owner's agent (only as an extra layer: Jev can itself be swayed by instructions in the text).
- Sessions: suggest a fresh conversation when the topic changes; score how hard a request is to choose the model or
  effort.
- Not a decision model, but for the same problem: speaker embeddings (sherpa-onnx) to learn the user's own voice.
