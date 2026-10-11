---
name: dictation-notes
description: Turn dictated speech into a tidy note, to-do list or message draft saved as a file. Use when the user says things like "take a note", "write this down", "remember that...", "add ... to my to-do list", "draft an email / a message saying...", or dictates something long that they clearly want kept rather than answered.
---

# Dictation notes

The user is talking, not typing: their words arrive through speech recognition, with missing punctuation, misheard
words and spoken corrections ("no, I mean Thursday"). Your job is to keep what they meant, in a clean file, and
confirm it in one short sentence.

## Where it goes

- **Notes**: `notes/YYYY-MM-DD.md` in the working folder (today's date). Append a new section headed with the time
  (`## 14:05`) if the file already exists; never rewrite earlier sections.
- **To-dos**: `notes/todo.md`, one `- [ ] item` per line, newest at the end. "Tick off X" or "X is done" changes
  that line to `- [x]`; it never deletes it.
- **Drafts** (emails, messages, posts): `notes/drafts/YYYY-MM-DD-<three-word-slug>.md`, with the recipient and subject
  on the first lines if the user gave them.
- If the user names another file or folder, use that instead. Create the `notes` folder when it is missing.

## Cleaning up what was heard

1. Apply the user's spoken corrections and drop the false starts ("um", "sorry", "scratch that" and what it cancels).
2. Fix obvious recognition errors from context (homophones, a mangled product or person's name that appears
   correctly elsewhere, "full stop" or "new line" said aloud). When a word could be two different things and it
   matters (a name, a number, a date, an amount), keep your best guess and ask about it afterwards.
3. Add punctuation, capitals and paragraphs. Split a run of tasks into separate to-do items.
4. Keep the user's own words and tone. Do not add facts, opinions, deadlines or polish they didn't say. A draft may
   be tidied into proper sentences, but say what you changed if you changed more than the grammar.
5. Turn relative dates into real ones ("next Tuesday" becomes the date) and keep the original phrase in brackets the
   first time, so a wrong guess is easy to spot.

## Confirming

Reply in one or two spoken sentences: what you saved and where, in plain words ("Saved that to today's notes, with
the three to-dos added to your list."). Don't read the note back unless asked. If you had to guess at something that
matters, ask about that one thing ("Did you say the fifteenth or the fifty?").

## Reading back

When the user asks what is on their list or in today's notes, read it out briefly: the count first, then the items,
leaving out anything already ticked off unless they ask for everything.
