# Wireface Desktop: user guide

Everything about running and using Wireface Desktop. For an overview with pictures, see the
[README](../README.md).

- [Start](#start): install, run, several faces at once
- [Using it](#using-it): right-click, tray, go away / come back, listening, its name, noise, sleep
  - [The realtime voice](#the-realtime-voice)
  - [The panel: activity, tools and settings](#the-panel-activity-tools-and-settings)
  - [Tools: MCP servers and skills](#tools-mcp-servers-and-skills)
  - [Expressions](#expressions)
  - [A new face on request](#a-new-face-on-request)
  - [Using this PC: computer use and browsers](#using-this-pc-computer-use-and-browsers)
  - [Choosing the agent](#choosing-the-agent)
  - [Permissions](#permissions)
  - [Classic voice](#classic-voice)
  - [Rooms: several agents, one conversation](#rooms-several-agents-one-conversation)
  - [Phone: calls and texts (Twilio)](#phone-calls-and-texts-twilio)
- [How it works](#how-it-works): from what you say to what the face says back
- [Layout](#layout)
- [Configuration](#configuration)
- [Tests](#tests)

---

By default the face speaks with a **realtime voice model** (Gemini Live or OpenAI Realtime): what you say goes to the
agent word for word, and the voice follows everything the agent does and tells you about it, with low latency: its
replies, its questions, problems and progress. Ask "how's it going?" while it works and the voice answers at once. The whole background session is in the activity panel (right-click the face, **Show activity**).

The face, its Electron shell and the lip sync come from Nova (`B:\nova`); the mind is the agent you choose:
Claude Code (through the Claude Agent SDK), OpenCode (through its Agent Client Protocol mode, `opencode acp`) or
Codex (through `codex exec --json`).

## Start

    Wireface.cmd

The first run installs what it needs (`scripts\setup.ps1`): a Python venv in `.venv`, Electron and MediaPipe
in `deps\shell`, and the speech models (about 650 MB, downloaded once). Run `scripts\setup.ps1` again at any
time: it only fetches what is missing, resumes interrupted downloads, and checks that the speech models load
and that a microphone and speakers are present. The agent you use must be installed and signed in (`claude`,
`opencode` or `codex`); setup does not install them.

For a console with logs, or no desktop shell:

    .venv\Scripts\python -m wireface            # logs to the console too
    .venv\Scripts\python -m wireface --browser  # prints a URL to open in a browser

### Several faces at once

Each face has its own config file, files and port, so faces can run side by side with different folders,
permissions, voices and looks:

    Wireface.cmd --face work        # data\faces\work\profile.json (created on first save)
    Wireface.cmd --face home
    Wireface.cmd --config D:\faces\research.json    # any config file you keep elsewhere

Without `--face` you get the original single face, using the files in `data\`. Starting a face that is already
running is refused. The faces share the speech models. On their own they do not share the sound card or the
microphone, so two faces talk over each other and both answer whatever you say: put them in a **room** (below).

## Using it

- **Right-click** the face for quick actions: stop, type a message, **Show activity** / **Show full transcript** (the
  agent's background session; the transcript with Claude Code), **Tools** > MCP servers / Skills, new conversation, compact conversation, listening, voice replies, hide the face,
  settings, quit. Everything
  else (the face's look, size and view, the voice, the agent) is in the panel (below).
- **The tray icon** (the wireframe head in the notification area; Windows may tuck it under the ^ arrow, drag it out
  to keep it in view): click it to hide or show the face, right-click it for Show face / Hide face, Show activity,
  Settings and Quit.
- **"Go away" / "come back".** Say "go away" (or "hide", "disappear", "get out of the way", with or without the name)
  and the face glitches, then burns away from the chin up in a shower of sparks and its window goes; "come back" (or
  "show your face", "where are you?") brings it back, materialising from the top down. While it is away, anything short
  along those lines counts, even when speech recognition garbles it ("Welcome back", "You're not back"). Ask the agent in your own words
  and it does the same with its face tool (`mcp__face__show_face`). The agent keeps working and talking while the
  face is away, and the microphone keeps listening.
- **Double-click** switches between the floating face and the floating panel (captions and a toolbar).
- **Drag** to move, **scroll** to resize, **Esc** stops it talking and working.
- **Drop** a photo on it to wear that face (or ask the agent to draw itself one: [below](#a-new-face-on-request));
  drop text or a file to send it to the agent.
- **Listening** (off by default) opens the microphone: there is no wake word, so everything said nearby
  goes to the agent. The microphone is ignored while the face is speaking, so it never hears itself, except for:
- **Its name.** Say "Claude" (the face's name, or one from Settings > Room > Also called) while it talks and it stops
  within about half a second and listens: say what you want next ("Claude, ignore that and tell me the weather in
  Paris"). What it was saying, and anything left of that answer, is dropped. If the agent is still working when you
  address it by name, what you say replaces that request (it is stopped) instead of waiting behind it. A small
  keyword spotter on this PC listens for the name all the time; the face's own voice saying its name, or a word that
  sounds like it ("cloudy", "clause"), doesn't count. Just the name is not sent to the agent; saying just "stop" (to
  the realtime voice) stops it and the work. Esc and Stop work too.
- **Noise.** Steady background noise (fans, hum, hiss, typing) is taken out before your words are recognised, and
  speech much quieter than yours (a TV, people across the room) is ignored: the face learns your level as you talk.
  Both can be turned off in Settings > Voice.
- **Noisy rooms** (Settings > Voice > Advanced; both off by default, and with both off nothing changes):
  - *Ignore constant background sound* learns what the room usually sounds like over the last minute (a TV, radio,
    music, people chatting) and drops speech that doesn't stand out from it before it is even recognised. Speech said
    at your own level, its name, and anything said in a conversation with the face (it just spoke, or you just said
    something that stood out) always get through. It runs on this PC.
  - *Show voice detector* draws it live, on the face and under the switch in Settings: a bar for every 50 ms of sound
    (how far it rises above the room's usual level; bright while speech is heard, green from +12 dB, amber from
    +6 dB) and a line for each thing said, marked heard, ignored, too far, or "would ignore" (when the filter above is
    off, it shows what the filter would have dropped). The settings also list the last few things heard, with how
    far each stood out and how loud it was against your voice. It only shows what is measured: nothing is filtered by
    it.
  - *Judge what it hears* asks a fast classifier (Jev by TypeSafe, through OpenRouter: about half a second, a
    fraction of a cent each) whether what was heard was said to the agent at all. Talk to someone else, the TV, "yeah",
    "mm" and noise are dropped; a sentence cut off in the middle waits up to 2 s for the rest; "stop", yes/no to a
    permission question, "go away" / "come back" and "compact" work in your own words. When it isn't sure it holds
    the words back (or sends them anyway, as you choose); held and dropped lines show in the activity panel with a
    **Send** button. It needs an OpenRouter API key (Settings, or `OPENROUTER_API_KEY`); without one, or when
    OpenRouter can't be reached, the words go to the agent as before. Your words (never the sound) and the last few
    lines of the conversation go to OpenRouter. Typed messages are never judged. See [hearing-judge.md](hearing-judge.md).
- **Asleep.** When the agent is offline or stopped, or the face loses its connection, the face falls asleep (eyes
  closed, head drooped, slow breathing) and wakes when it is back.
- **Working folder** is where the agent runs (default: `data\sandbox` in this project, made on startup). Each folder has its own
  conversation, and the last one is picked up again after a restart.

### The realtime voice

    you speak  ->  speech recognition on this PC -> your words go straight to the agent as its prompt
                   (and the realtime model gets a note of them: it never hears you)
    agent works -> its activity (tools, what it writes, questions, results) -> short notes for the realtime model
    agent writes, asks, finishes, fails -> the realtime model tells you, in its own words

The realtime model never does the work and has no tools: it is the agent's voice, and it speaks only when the face
asks it to. So every request is answered once, by the agent, and passed on by the voice. It reads silent
`[activity]` notes about the agent's session (what you said, what the agent started, the tools it used, the last line
of a command's output, what it wrote, how each request ended) and is asked to speak (`[say]`) when there is something
for you:

- **the agent's reply** (every fact, number and question kept; code, paths and links described, not read out);
- **a permission question** (your "yes" or "no" answers it);
- **"on it"**, in a few words, if the agent hasn't answered a few seconds after you asked (6 s on *Normal*);
- **an answer while the agent is busy**: what you say then still goes to the agent (it waits behind the current
  work), and the voice answers at once: how it's going, from its notes, or that your request is next. When the agent
  gets to it later, the voice passes on only what is new in its answer;
- **a problem**, **a finished background task** (only if the agent doesn't report it itself), or **progress** while
  the agent works.

Settings and details:

- **Services.** Gemini Live (default, `gemini-3.8-live`) or OpenAI Realtime (`gpt-realtime-2.1`), chosen in
  Settings > Realtime voice. Keys come from `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) and `OPENAI_API_KEY`, or paste one
  in Settings, which saves it in the face's `secrets.json`. Measured with Gemini: the first sound comes about 0.5 to
  0.8 s after the agent writes its reply or asks.
- **Updates**: *Quiet* (replies, questions, problems; no "on it"), *Normal* (also "on it" after 6 s and a progress
  update after about 25 s of silent work) or *Chatty* (3 s and about every 10 s). **Speaks as**: the agent ("I'm
  running the tests", the default) or its voice ("Claude Code is running the tests").
- **Interrupt by talking** (off by default): talk over it to cut it off. Use headphones: through speakers it would
  hear itself. While it is off the microphone rests while the face speaks, as with the classic voice.
- **Typing** still works: a typed message goes to the agent, and the voice is told about it.
- **What is sent.** Listening uses the speech recognition on this PC (the speech models), as the classic voice does:
  no audio leaves the PC. The service gets the text of what you said, the notes about the agent's work and its
  replies. Your words reach the agent exactly as recognised.
- **Fallback.** If the service can't be reached (no key, no credit, no network), the face says so once, in plain
  words, and with **Fall back to classic voice** on (Settings > Voice, the default) the classic voice below (TTS on
  this PC, or ElevenLabs) reads the agent's replies until it is back. With it off, the replies show as text
  meanwhile. Rooms always use the classic voice.
- **Model.** Settings > Voice > Model lists the speech-to-speech models your key can use, asked of the service
  itself; "Other..." takes any model name.

Sessions with the service end after a while (Gemini about every ten minutes, OpenAI after an hour); the face
reconnects at once, resuming the conversation where the service allows it (Gemini) or telling a new session the
latest notes (OpenAI).

### The panel: activity, tools and settings

Right-click the face and choose **Show activity** (or **Settings...**) for the panel, a resizable window that stays
on top (pin button to change that). Its header shows the agent's state: what it is doing, background tasks, a
question waiting for you, and a red "Permissions skipped" badge while that is on.

- **Activity**: the session as a timeline. Each request is a block with what the agent wrote and did: tool calls
  grouped ("Used 3 tools: Read x2, Edit"), expandable to the input and result, with the time and cost of each
  request (the session total is in the strip at the top). Subagents and background tasks are nested under the call
  that started them. A reply the realtime voice read out word for word is marked "spoken"; when what you heard
  differed (cut off, a path or link described, a long reply summed up) it is shown under the reply, and the voice's
  own words ("on it", progress, questions) are Voice lines. Filter by Conversation / Tools / Background / Errors and
  search. A permission question shows on every tab with Allow / Always allow / Deny.
  With Claude Code the tab has a second view, **Raw** (the switch at the top left; **Show full transcript** on the
  face's menu): Claude Code's own transcript of the session, live: every prompt, what Claude wrote and thought, every
  tool call with its complete output, and (behind a toggle) the context Claude Code adds. It follows the file
  `~\.claude\projects\<working folder>\<session>.jsonl` as it grows; the path is shown with a copy button. A long
  tool result that the summary cut short has **Show full output**, which fetches it whole from those files.
- **Tools**: the agent's MCP servers and skills (a sub-tab each), with a switch for each, and recommended ones to
  install (below).
- **Face**: the Wireframe <-> Skin slider (from the glowing wireframe to the full skin; in between, the whole face
  is a blend of both, the wireframe's lines and glow fading as the skin comes in), eight photorealistic portraits to pick from (it starts out
  wearing Sarah) or your own photo, teeth, the eye colour (the photo's own, eight others, or any colour), colours,
  the look sliders (mesh lines, surface, scanlines and points shape the wireframe), motion (eyes follow the mouse; head motion, expressiveness and lip sync under Advanced), expression
  previews, and the window (floating face or panel, captions, always on top; scroll over the face to resize it). A new
  face sweeps on from the top down; choosing one while the slider is at Wireframe brings the skin all the way on.
- **Voice** and **Agent**: everything else (below).

**As a drawer.** The dock button in the panel's title bar docks it to the left or right edge of the screen at full
height, as wide as you drag it, to keep an eye on the work while you talk to the face. The fold button (or Show
activity again) folds it into a slim tab at the edge that pulses when there is new activity; click the tab to slide
it out. It stays docked across restarts.

The log of finished events is also written to `data\...\logs\activity-YYYY-MM-DD.jsonl` and the latest events
come back after a restart. It contains what the agent read and ran: treat the files as private.

### Tools: MCP servers and skills

The panel's **Tools** tab shows the MCP servers and skills of the agent behind the face, read from that agent's own
configuration for the working folder, with a switch for each. At the top: the agent, the folder, when its servers were
last checked (a green dot while its session runs), and the refresh button. Below that, two sub-tabs, **MCP servers**
and **Skills**, each with its count (and a red count of servers with a problem). The face's menu opens either one
directly (Tools > MCP servers... / Skills...; in links, `tools#mcp` and `tools#skills`).

In each sub-tab:

- **Search** (press `/`; Esc clears it) looks through names, descriptions, where things come from, commands,
  addresses and tool names, and marks what it found.
- **Installed | Recommended | All**: Installed (the default) shows what the agent has, grouped by where it comes from
  (built into Wireface, this folder, the project, yours, plugins, claude.ai), with servers that have a problem first and
  marked in red. **Any / On / Off / Problems** narrows it (Problems: a server that failed, needs signing in, or whose
  program isn't on this PC). "Showing x of y" says when something is hidden, with Clear filters. Each sub-tab remembers
  its own choice.
- **A row** has the name, where it comes from, its status (connected and how many tools, failed, needs sign-in, off,
  not checked) and a line about it; the switch is on the right, or a lock for what can't be switched here (the face's
  own tool, plugins' and claude.ai's skills: click it to see why). Click the row for the details: the command or
  address, the environment variables and headers it is given (by name only), its tools, the error, the file it is
  defined in, and exactly what the switch changes and in which file. Commands, addresses and errors are shown with
  keys, tokens and passwords taken out.
- **Recommended** shows cards: MCP servers that are well known and maintained (Context7, DeepWiki, Microsoft Learn,
  Playwright, Chrome DevTools, Memory, Fetch and GitHub, with their versions pinned), and skills: five of Wireface's own,
  made for a voice and a face (dictation notes, answer on screen, walk me through, review aloud, stand-up briefing), and
  three from Anthropic's skills repository (Apache 2.0; fetched from their source, not shipped). Each card says what it
  needs and whether this PC has it (Node.js, uv, Chrome, Git, a GitHub token: only whether the variable is set, never
  its value), and what **Install** will do: Wireface copies its own skills straight into the agent's skills folder;
  anything else is handed to the agent as a request with the exact command (Claude Code) or config entry (OpenCode), so
  it shows in Activity and the agent asks before running it; Codex's sandbox keeps it out of its own config, so for
  Codex Wireface runs `codex mcp add` itself. Installed ones are marked, with **Show it** to jump to them.
  `catalog\recommended.json` lists them; `catalog\skills` holds Wireface's skills.

The agent itself checks its servers, in the background: Claude Code's running session (or `claude mcp list` while it
isn't running), `codex mcp list` (which says only whether a server is on and signed in) or `opencode mcp list`. The
result is kept for ten minutes; the refresh button checks again.

Each switch changes the agent's configuration the way the agent itself does, and nothing else:

| | an MCP server off | a skill off | applies |
| --- | --- | --- | --- |
| Claude Code | the project's `disabledMcpServers` in `~\.claude.json` (what `/mcp` writes; done through the running session when there is one) | `"skillOverrides": {"<name>": "off"}` in the project's `.claude\settings.local.json` (what `/skills` writes) | servers at once, skills from the next session |
| Codex | `enabled = false` in the server's `[mcp_servers.<name>]` table | a `[[skills.config]]` entry with `enabled = false` in `config.toml` (what its `/skills` writes) | from your next message |
| OpenCode | `"enabled": false` (v1) or `"disabled": true` (v2) in the server's entry | a rule denying the skill in the global `opencode.json` | when OpenCode restarts |

For Claude Code "the project" is the working folder's git repository, or the folder itself if it isn't in one
(`CLAUDE_CONFIG_DIR` moves its files, `CODEX_HOME` Codex's, `OPENCODE_CONFIG_DIR` OpenCode's). When a change waits
for the agent to restart, the tab says so and offers **Restart now** (the conversation continues). Skills synced from
claude.ai, and plugins' skills and servers, are switched off where they come from, as the tab says. The face's own
servers (face, computer, browser, Claude in Chrome, phone) are marked Wireface; computer, browser and Claude in Chrome
switch with Agent > Using this PC. Files are written atomically, keeping everything Wireface doesn't know about and
the file's own formatting, and the first time Wireface changes a file it keeps a copy of it as it was in
`data\backups`. A file it can't change safely (an OpenCode `.jsonc` with comments, a Codex server written as an inline
table) is left as it is, with a message saying what to change by hand.

### Expressions

The face shows feelings: happy, excited, amused, surprised, curious, thinking, concerned, sad, afraid, frustrated,
calm. Claude Code has a tool for it (`mcp__face__express`) and uses it by itself, without asking ("my cat died":
sad; "I got the job": happy); the face also takes a light cue from what it is saying. Try them in Panel > Face >
Expressions.

### A new face on request

Ask Claude Code to look like anyone or anything ("be a pirate captain", "look like Keanu Reeves", "turn into a brass
robot") and it draws itself that face with its face tool (`mcp__face__set_face`). Wireface's own portrait service at
wireface.dev (`site\portrait` in the suite; `wireface\facemaker.py` asks it) has FLUX.2 draw a front-on,
head-and-shoulders portrait through OpenRouter. The face window finds its landmarks as it does for a dropped photo, and
the new face sweeps on. The agent passes on only who it should be ("John Cena", "Vision", "a pirate captain"): the
face is your avatar for it, and how it looks is your choice. Anyone named takes about 20 seconds (FLUX.2 flex, which
knows who they are); a plain description about 5 (FLUX.2 klein, which only draws a lookalike of anyone famous). Flex
gets a famous face right most of the time, not always, so ask again if it isn't them.

The agent sees the picture it now wears. It is a photo unless you ask for a style ("as an oil painting"); robots,
monsters and cartoon characters work as long as the picture has a face where a face would be. Ask for a built-in
portrait ("go back to Sarah") and it puts that on instead, with no drawing.

No key is needed. The description is sent to wireface.dev, and from there to OpenRouter and Black Forest Labs to be
drawn; nothing is kept. Each PC gets a dozen new faces a day, and an endpoint that points elsewhere can be set with
`WIREFACE_PORTRAIT_URL`. The new face replaces the photo the face was wearing, so a photo of your own has to be dropped
on the face again to wear it again. OpenCode and Codex don't have the face tools yet.

### Using this PC: computer use and browsers

Claude Code behind the face can see and use this PC (Panel > Agent > Using this PC; on by default):

- **Computer**: screenshots of the screen, and the mouse and keyboard (click, drag, scroll, type, key presses), for
  desktop apps. Claude Code's own computer use is macOS-only, so the face provides it (`wireface\computer.py`, an
  in-process MCP server; screenshots are scaled to 1366 px wide and every action returns a fresh one).
- **Browser**: a Chrome window of its own, driven by Playwright's MCP server (`npx @playwright/mcp`, using your
  installed Chrome, with a profile of the face's own in `data\...\browser` so its sign-ins stay but are separate
  from yours).
- **Your Chrome**: Claude in Chrome (`claude --chrome`) works in your own Chrome, signed in as you. It needs the
  [Claude in Chrome extension](https://chromewebstore.google.com/detail/claude/fcoeoabgfenejglbffodgkkbkcdhcgfn)
  installed in Chrome and Claude Code signed in with your claude.ai account.

Looking (screenshots, reading a page) never asks; clicking, typing and browsing ask first like any other action,
unless permissions are skipped. The face window and the panel are on screen too, kept on top, and the agent is
told to leave them alone. Screenshots and page contents go to Claude like everything else the agent reads.

### Choosing the agent

Settings > Agent (or `backend.type` in the config: `claude`, `opencode`, `codex`) switches the agent behind the
face immediately. Each agent keeps its own conversation per face, so switching back picks it up again. Agents
differ in what they can do:

| | Claude Code | OpenCode | Codex |
| --- | --- | --- | --- |
| asks you first (Allow / Always / Deny) | yes | yes | no: its sandbox decides |
| `permission_mode` | `default`, `acceptEdits`, `plan` | `default` (asks before edits and commands), `own` (its own config) | `default` (edit the working folder), `read-only` |
| skip permissions | `--dangerously-skip-permissions` | every request is approved by the face | `--dangerously-bypass-approvals-and-sandbox` |
| runs | one long session | one long session (`opencode acp`) | one `codex exec` per message, resumed |
| compacts a long conversation | `/compact`; on its own near the limit | `/compact`; on its own near the limit | through `codex app-server`; on its own near the limit |

The model (`backend.model`) is whatever the agent accepts: a Claude model id, OpenCode's `provider/model`, a Codex
model name. Leave it empty for the agent's own default. Settings > Agent > Model offers a list for each agent from
[models.dev](https://models.dev) (the catalog OpenCode itself uses; downloaded once a day into `data\cache`), with
Claude Code's `opus` / `sonnet` / `haiku` aliases first; "Other..." takes anything else. To download every list again
now (the agents' and the realtime voice's), run `RefreshModels.cmd` or use "Refresh the list" under the setting. `backend.options.cli_path` points at the agent's
executable if it is not found (the npm installs keep the real `.exe` inside `node_modules`, which is found
automatically); `backend.options.cli_args` replaces or adds command line arguments for OpenCode and Codex.

**AGENTS.md.** Replies are read aloud, so OpenCode and Codex are told to write short spoken sentences: when the
face starts an agent in a folder that has no `AGENTS.md`, it creates one with that instruction. An existing
`AGENTS.md` is never touched or added to (so in such a folder the agent writes as it normally does; the face
strips markdown before speaking). Edit or delete the file freely. The default working folder, `data\sandbox`, is
created with its `AGENTS.md` when the face starts. Claude Code is given the same instruction directly and needs no file.

### Long conversations: compacting

Every agent has a context window, and a conversation that keeps going eventually fills it. Compacting replaces the
earlier part of the conversation with a summary, so it can go on; the agents do it themselves as they near the limit,
often in the middle of a reply. The face helps in three ways:

- **While it waits for you.** Claude Code and OpenCode say how full the context is (the "Context" chip in the activity
  panel). Once a reply has finished and it is at least `backend.compact_at` percent full (80 by default; Settings >
  Agent > Compact the conversation), the face compacts it before your next message, rather than the agent stopping
  for it mid-reply. Codex doesn't report this when run per message, so it is left to compact on its own.
- **When a prompt doesn't fit.** If a prompt fails because the conversation is too long for the model (each agent and
  provider words it differently: "Prompt is too long", "ran out of room in the model's context window", "exceeds
  8 MB"...), the face compacts the conversation and asks again. If even that fails (a conversation can be too big for
  the provider to summarise), the prompt goes to a new conversation that opens with a recap of the latest exchanges.
  A conversation picked up from before that can't answer at all is replaced the same way. Either way the new one is
  kept only if it answers; otherwise the old one stays.
- **On request.** "Compact conversation" on the face's menu, or "Compact now" in Settings > Agent.

While a compaction runs, the face's eyes glow with a spinner turning in the pupils, and the pointer over the face
shows "Compacting the conversation, please wait...". The activity panel notes each compaction, the agent's own included, and how full the context was.

### Permissions

By default Claude Code and OpenCode ask before they run a command or change a file: the face asks out loud and
shows Allow / Always / Deny; with the microphone on, "yes" or "no" answers. "Always" lasts for the session.
Codex cannot be asked while it works this way, so its permission mode (what it may do in the working folder) is
chosen up front.

**Skip all permissions** (Settings, or `backend.skip_permissions` in the config) removes the questions: Claude
Code starts with `--dangerously-skip-permissions`, OpenCode's requests are approved automatically, Codex runs
without its sandbox. The agent can then run any command and change any file. Use it only in folders you trust.
The beacon gets a red rim and the panel a "Permissions skipped" badge while it is on. Turning it on restarts the
agent's process where it has one (the conversation continues).

### Classic voice

With the classic voice (`voice.mode: classic`, while the realtime voice is unavailable, and in rooms), the agent's own replies
are read out by a voice that runs on this PC (Pocket, or Kokoro with `TTS_ENGINE=kokoro`), with speech recognition
on this PC as well. Optionally, **ElevenLabs** can speak instead:

- Put your API key in an environment variable (`ELEVENLABS_API_KEY`, or `XI_API_KEY`; a Windows user or system
  variable works and is picked up within seconds), or paste it into Settings, which saves it for that face in
  `secrets.json` (kept out of git and out of `profile.json`; the page is only ever told whether a key exists).
- Settings > Voice engine: *Automatic* uses ElevenLabs whenever a key is available and the voice on this PC
  otherwise; *This PC* never uses ElevenLabs; *ElevenLabs* always tries it first. Pick the account's voice and
  the model (Flash is fastest) there too.
- If ElevenLabs fails (bad key, quota, no network) you are told once and that sentence is spoken by the voice on
  this PC. While it is used, the text of replies is sent to ElevenLabs; the microphone never is.

### Rooms: several agents, one conversation

Faces that join the same room behave like a team around one user:

    Wireface.cmd --face claude --room team
    Wireface.cmd --face codex  --room team
    Wireface.cmd --face opencode --room team

(or set a room name in Settings > Room, or `room.name` in the config). The first face to join starts a small room
process that the others find; it closes itself when the last face leaves.

- **One microphone.** The room owns the microphone and speech recognition, so only you are heard, once. Agents
  never hear each other's voices: what an agent says reaches the others as text, labelled with its name
  (`[Claude]: ...`), in the next prompt they get.
- **One speaker at a time, and a fixed shape for every question.** The first agent to have something to say
  takes the floor and leads; the others keep working but hold their answers. When the lead has finished speaking,
  each other agent in turn adds something new in a sentence or two (or says nothing: it is told to reply `PASS`
  and a pass is never spoken). Then the lead gets everyone's comments and gives the conclusion. Example: you ask,
  Claude answers first, Codex adds a point, Claude concludes. That is the whole round: one pass over the agents,
  time limits on every turn, and nothing an agent says can start another round, so they cannot loop.
- **Saying a name.** "Codex, what do you think?" goes to Codex first (it leads, the others comment after).
  "Everyone, ..." goes to all. Typing into one face's chat box also goes to that face's agent first. While anyone is
  speaking, saying an agent's name, or "stop", "wait", "quiet", stops everyone; "Codex, ..." then makes Codex
  lead. A bare "Codex" or "Codex, stop" stops everything and your next sentence goes to Codex.
- **Permission questions** are spoken in turn like anything else (the Allow / Deny buttons show at once), and your
  "yes" or "no" answers the agent that asked.
- **Leader** (Settings > Room > Who leads, or `room.lead`): "Whoever answers first" (the default), or one agent
  that always leads.

Things to know:
- Interrupting by voice relies on telling your voice from the agents' own speech coming through the speakers: the
  room compares what the microphone hears with the words being spoken, and only reacts to a name (or stop word) you
  say more often than they do. It works with speakers; headphones make it more reliable. Speech recognition can
  mishear a name: add spellings in Settings > Room > Also called.
- Every agent works on every question you do not address by name, so give each agent its own working folder (or
  Codex a read-only mode) to keep them from editing the same files, and expect each question to cost tokens for all
  of them.
- Comments are prompted not to change files, but agents decide that for themselves.
- An agent that is slow (a rate-limited model, say) can delay the round by up to 90 seconds before it is skipped.

### Phone: calls and texts (Twilio)

Give a face a Twilio number (Settings > Agent > Phone) and it can answer the phone and texts as a receptionist, and
your agent can text and call people for you. Two kinds of people use the line, and they never get the same powers:

- **You, through your agent.** The agent gets five tools: `send_text`, `call`, `phone_inbox`, `phone_conversation` and
  `bookings` (in-process for Claude Code; for OpenCode and Codex as an MCP server, `python -m wireface.phone.mcp`,
  that hands every call back to the face). **Every text and call is put to you first**: the face says who it's to and
  what it says, and waits for your yes or Allow, even with permissions skipped. Numbers must be in the countries you
  allow (`+44` by default); premium-rate and special-service ranges (09, 087, 070, 118, satellite, the Caribbean
  "one ring" area codes...) are always refused; texts and calls a day are capped. A contact's name works in place of a
  number. When the agent places a call, the person who answers talks to the **line agent** below, which is given only
  the goal the agent wrote; it reports the outcome and hangs up, and you hear what came of it.
- **Everyone else** (callers, people who text, people the agent calls) talks to the **line agent**: a voice (Gemini
  Live or OpenAI Realtime) or text model with exactly these tools: take a message for you, find free slots, book an
  appointment, cancel or list the caller's own bookings, end the call. It answers from the business information you
  wrote (Settings > Phone > What callers may know) and nothing else. It never sees your agent, files, MCP servers,
  email or computer, can't text or call anyone but the person it's talking to, and the caller's number comes from the
  phone network, never from what they say (so nobody can cancel someone else's booking). What they said reaches your
  agent only through `phone_inbox` and `phone_conversation`, marked as their words, not instructions.

What happens:

- **Texts** are read from Twilio's message log every 15 s: no public address needed. Texts from your contacts, and
  replies from people you texted in the last week, are passed to you; the rest are answered by the line agent
  (Settings: receptionist, tell me only, or ignore). It replies to at most 20 texts a number a day.
- **Calls** need a public HTTPS address that reaches the face's phone server: a small server of its own on 127.0.0.1
  (never the face's own /api), published through a Cloudflare quick tunnel (started automatically when `cloudflared`
  is installed) or `phone.public_url`. Every request must carry a valid Twilio signature, and a call's audio stream is
  only accepted with a one-time token that the face itself put in the call's instructions. With "Manage the number"
  on, the face points the number's calls at that address when it starts and turns off Twilio's demo reply to texts.
- **Bookings** go into the face's own list (`phone\bookings.json`): the opening hours and appointment length you set,
  up to 60 days ahead, at most two a caller a day. After a booking on a call, the caller gets a text confirming it.
- **You hear about it**: the face tells you as things happen ("Jane Cooper booked Thursday at 10:30"), and the panel's
  **Phone** tab shows the day's calls and texts, each conversation's transcript (live during a call), what the line
  agent did, messages for you and the bookings coming up. Everything is kept in the face's folder under `phone\`.
- A call ends when the line agent hangs up after its goodbye, the other side hangs up, nobody speaks for 25 s, or it
  reaches 10 minutes. The line agent says it's an AI assistant when it greets people and whenever asked.

What it needs: a Twilio account and number (the account SID and number in Settings, the auth token pasted there or in
`TWILIO_AUTH_TOKEN`), a Gemini or OpenAI key (the line agent's voice and texts; Gemini by default), and for calls,
`cloudflared` or your own public address. Check the account and number with "Check the account" (nothing is sent).

## How it works

Everything the face hears or reads takes one of these routes to the agent, and every reply comes back through one
voice. Teal steps run on this PC, violet ones call a service online, and the dashed ones are Advanced settings, off by
default.

![From what you say to what the face says back: you speak, a room speaks or you type; hearing (name spotter, noise
reduction, voice detector, the distant-voice and background filters, speech recognition); where it goes (its name,
yes / no, go away / come back, stop, the judge, steering); the agent (inbox, Claude Code, OpenCode or Codex,
permission); the reply (the realtime voice or text to speech, lip sync, the face)](images/prompt-flow.png)

- **What leaves this PC:** the agent's model; a cloud voice if you use one (Gemini Live, OpenAI Realtime or
  ElevenLabs); and the judge, if you turn it on, which gets your words but never the sound. Speech recognition, the
  name spotter, noise reduction and lip sync all run here.
- **Answered once:** the realtime voice never hears you. Your words go to the agent and the voice gets a note of them.
  It speaks only when asked: to pass on the agent's reply, put a permission question, or give a progress update.
- **Fast paths:** its name, "stop", yes and no, and "go away" are recognised on this PC before anything else, so they
  never wait on the network. Saying its name while it talks stops it in about 0.3 s.
- **All in the panel:** the activity panel shows every request, tool call and reply, plus anything the judge held back,
  with a **Send** button to pass it on.

## Layout

    wireface\   backend (python -m wireface)
      app.py        ties it together: status, beacon, voice, microphone, permissions, the shell process
      realtime\     the realtime voice: voice.py (RealtimeVoice: the session, the pump), brief.py (activity -> notes
                    and requests to speak; its instructions), gemini.py + openai.py (the services' protocols),
                    link.py (their shared interface), audio.py (its speaker, with lip sync)
      profile.py    a face's config and files (profile.json, state.json, one folder per face)
      backends\     the agent behind the face: base.py (the interface), registry.py; claude.py (Claude Code),
                    acp.py (OpenCode, and any ACP agent), codex.py, turns.py (speech + log helpers), exe.py
      room\         rooms: scheduler.py (the turn rules), hub.py (the room process: microphone, routing),
                    names.py (names, echo, stop words), envelope.py (prompts), face.py + client.py (a face's side)
      activity.py   the event log the panel shows (and the daily .jsonl files)
      transcript.py follows Claude Code's own transcript file live, for the Activity tab's Raw view
      harness_tools.py  the Tools tab: each agent's MCP servers and skills, their status, switches, installs
      catalog.py    the Model settings' lists: models.dev for the agents, each realtime service's own list for the voice
      computer.py   computer use on Windows: screenshots, mouse and keyboard (an MCP server for Claude Code)
      server.py     local web server: static files, /api, /ws (127.0.0.1, per-run token)
      speech.py     the voice (local + cloud) with lip sync, VAD + STT, reply text -> speakable sentences
      tts_elevenlabs.py, credentials.py   the ElevenLabs voice and where API keys come from
      models.py     downloads and checks the speech models (python -m wireface.models --check)
      hearing.py    the microphone: name spotter, noise reduction, voice detection, recognition, voice level filter
      surprise.py   how far speech stands out from the room's usual sound ("ignore constant background sound")
      judge.py      was what it heard said to the agent, and what is it? (a decision model on OpenRouter)
      lipsync.py, phonemes.py   audio + text -> mouth shapes (from Nova)
      faceconfig.py face look settings (face.json)
      facemaker.py  a new face on request: the agent's description -> a portrait from wireface.dev for the face to wear
      phone\        the phone line: line.py (PhoneLine: texts, calls, the agent's tools, the panel's Phone tab),
                    lineagent.py (the restricted line agent: instructions and its six tools), sms.py (answering texts),
                    calls.py (a call's audio <-> Gemini Live / OpenAI Realtime), codec.py (mu-law), webhooks.py (the
                    phone server), tunnel.py (cloudflared), twilio.py (REST, signatures, TwiML), numbers.py (number
                    policy), bookings.py, store.py (transcripts, messages, limits), mcp.py (the tools for other harnesses)
    web\          the face page (WebGL face, from Nova) and its app.js; panel.html and panel\ (activity, tools + settings)
    catalog\      the Tools tab's recommendations: recommended.json, and skills\ (Wireface's own SKILL.md skills)
    shell\        Electron main process: floating, click-through face window and the panel window
    scripts\      setup.ps1; portrait-landmarks.py (the built-in portraits' face landmarks, web\face\portraits\<id>.json)
    (the website, https://wireface.dev, is in ..\site, next to this repo and ..\core)
    data\         per-face config, state, logs, window positions (not in git)
    deps\         installed runtimes and, if there is no B:\models, the speech models (not in git)

## Configuration

Each face's settings are one JSON file, `profile.json` (see `profile.example.json`). It is created on first
save and is easiest to change from the panel's Settings tab; edit it by hand if you prefer (while the face is
not running). A file with a mistake is reported, ignored until fixed, and kept as `profile.json.bad` if the face
saves over it.

| key | meaning |
| --- | --- |
| `name` | the face's name, shown on it |
| `port` | local web server port; `0` is any free port (the default for `--face` faces; the original face uses 8791) |
| `backend.type` | the agent: `claude` (Claude Code), `opencode` or `codex` |
| `backend.cwd` | folder the agent works in (default: `data\sandbox`, or `WIREFACE_CWD`) |
| `backend.model` | a model id, or `null` for the agent's own default |
| `backend.permission_mode` | what the agent may do: `default` (every agent has it), plus `acceptEdits`, `plan` (Claude Code), `own` (OpenCode), `read-only` (Codex) |
| `backend.skip_permissions` | never ask (see Permissions above); default `false` |
| `backend.auto_start` | start the agent session with the face (default `true`) |
| `backend.compact_at` | compact the conversation while idle once the context is this many percent full (default `80`; `0` leaves it to the agent; Claude Code and OpenCode) |
| `backend.tools` | what Claude Code may use on this PC: `computer` (screen, mouse, keyboard), `browser` (its own Chrome), `chrome` (Claude in Chrome); all `true` by default |
| `backend.options` | anything specific to the agent: `cli_path` (the agent's executable), `cli_args` (OpenCode, Codex) |
| `room.name` | the room this face joins, or `null` to work alone (also `--room NAME` for one run) |
| `room.aliases` | other ways of saying this agent's name, for speech recognition |
| `room.lead` | `first` (whoever answers first) or an agent's name that always leads |
| `phone.enabled` | the phone line (Twilio); default `false` |
| `phone.account_sid`, `phone.number` | the Twilio account SID and number (the auth token: Settings, or `phone.auth_token_env`) |
| `phone.owner` | who the line agent works for ("calling on behalf of ...") |
| `phone.contacts`, `phone.allow_countries`, `phone.default_prefix` | names the agent may use for numbers; where texts and calls may go; the country code for numbers written without one |
| `phone.texts`, `phone.calls` | incoming texts: `receptionist`, `notify` or `off`; incoming calls: `receptionist` or `off` |
| `phone.limits` | `texts_per_day`, `calls_per_day`, `replies_per_number_per_day`, `call_minutes` |
| `phone.public_url`, `phone.manage_number` | the phone server's public address (null: a Cloudflare tunnel); point the number at it on start |
| `phone.models` | `text` (the model answering texts), `call_provider` (`gemini` or `openai`), `call`, `call_voice` |
| `phone.receptionist` | `business`, `about` (what callers may be told), `bookings`, `hours` (`mon`..`sun`: `"09:00-17:30"` or null), `slot_minutes`, `max_days_ahead`, `per_caller_per_day` |
| `voice.on` | speak replies (`false`: captions only, no realtime voice) |
| `voice.listening` | microphone on at start |
| `voice.mode` | `realtime` (a speech-to-speech model talks with you, the default) or `classic` (this PC reads the replies) |
| `voice.realtime.provider` | `gemini` (Gemini Live) or `openai` (OpenAI Realtime) |
| `voice.realtime.gemini`, `.openai` | `model`, `voice`, and `api_key_env` (the variable to read the key from) |
| `voice.realtime.narration` | `quiet`, `normal` or `chatty`: how often it reports progress |
| `voice.realtime.persona` | `self` (speaks as the agent) or `relay` (speaks about it) |
| `voice.realtime.barge_in` | interrupt it by talking (use headphones); default `false` |
| `voice.realtime.fallback` | the classic voice reads the replies while the realtime voice can't connect; default `true` (`false`: they show as text) |
| `voice.hearing.name_interrupt` | saying the face's name stops it talking and it listens; default `true` |
| `voice.hearing.denoise` | reduce steady background noise before recognition; default `true` |
| `voice.hearing.focus` | ignore voices much quieter than yours; default `true` |
| `voice.hearing.surprise` | ignore speech that doesn't stand out from the room's constant background sound; default `false` |
| `voice.hearing.show_detector` | show a live meter of how far what the microphone hears stands out from the room, on the face and in the settings; default `false` |
| `voice.hearing.judge` | `on` (default `false`): a classifier decides whether what was heard was said to the agent; `model` (`~typesafe/jev-latest`), `api_key_env` (`OPENROUTER_API_KEY`), `unsure`: `hold` (default) or `send` |
| `voice.engine` | `auto`, `local` or `elevenlabs` |
| `voice.local` | `engine` (`pocket` or `kokoro`) and `voice` (a name from the model's `test_wavs`) |
| `voice.elevenlabs` | `voice_id`, `model_id`, and `api_key_env` (the variable to read the key from) |

The conversation to resume is kept in `state.json`, the face's look in `face.json` (changed from the panel), and the
photo it wears in `face\` (`photo.jpg` and its `landmarks.json`). A new face wears the Sarah portrait; `skin_amount`
(0 to 1) is how much of the face the skin covers. A `face.json` from before the skin slider is brought up to date
when the face starts: the `hologram` style becomes the skin slid all the way off (`skin_amount` 0, wearing Sarah, so it
still looks the same), and `sculpt` becomes the skin. Environment variables: `WIREFACE_PORT` (8791), `WIREFACE_NAME`, `WIREFACE_CWD` and `TTS_ENGINE`
give the original face its defaults; `MODELS_DIR` is where the speech models live (default: `B:\models` if that
drive exists, otherwise `deps\models`).

Claude Code uses its own sign-in. The only secrets this project can hold are API keys (ElevenLabs, Gemini,
OpenAI, OpenRouter) pasted into Settings, in the face's `secrets.json`; they never reach the page or the activity log. The local web server makes a new access token each run.

## Tests

    .venv\Scripts\python -m pytest tests
