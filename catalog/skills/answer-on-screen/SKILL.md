---
name: answer-on-screen
description: Put the detailed part of an answer on screen instead of reading it aloud. Use when a reply would contain anything that is hard to listen to - commands, code, file paths, links, tables, step lists longer than three steps, comparisons, or exact numbers the user will need to copy - or when the user says "show me", "write it down" or "put that on screen".
---

# Answer on screen

Your replies are spoken aloud. People can follow a few sentences by ear, but not a command line, a table or a
twelve-step procedure. Say the gist; put the exact part where the user can see and copy it.

## When to use a file

Use one when the answer includes any of:

- a command, a snippet of code or configuration, a regular expression;
- a path, a URL, an ID, a key name, or a number that has to be exact;
- more than three steps, a table, a comparison of options, or a list of more than five things;
- anything the user asked to "see", "keep" or "have written down".

Short answers that are just words stay spoken only.

## How

1. Write a Markdown file in the working folder: `answers/<short-topic>.md` (lowercase, hyphens, no date needed;
   add `-2`, `-3` if the name is taken). Create the folder when it is missing.
2. Start with a one-line title, then the content. Put every command and snippet in a fenced code block with its
   language, so it can be copied whole. Number steps. Keep explanations short: the spoken reply already gave the
   gist.
3. Open it for the user when you can:
   - Windows: `start "" "answers\<file>.md"` (opens in their default app for Markdown files).
   - macOS: `open answers/<file>.md`; Linux: `xdg-open answers/<file>.md`.
   If opening fails or might be unwelcome (the user is presenting, say), just say where it is.
4. In the spoken reply, give the answer in plain words and say that the details are on screen ("You need to add
   one line to your settings file; I've put it on screen with where it goes."). Never read the file out.

## Keep it tidy

- Update the same file when the user refines the same question, instead of making a new one each time.
- Don't put secrets (API keys, passwords, tokens) in these files. Refer to where they are kept instead.
- If the user says they can't see the screen right now, read out the essential part slowly, one item at a time,
  and offer to repeat.
