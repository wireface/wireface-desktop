---
name: standup-briefing
description: Prepare a short spoken stand-up or progress briefing from the work in the working folder - what changed since the last working day, what is in progress, and what is blocked or next. Use when the user asks for "my stand-up", "what did I do yesterday", "catch me up", "where was I", or a summary of recent work on this project.
---

# Stand-up briefing

About sixty seconds of speech that the user could repeat in a stand-up meeting, built from evidence in the
repository rather than memory.

## Gather

1. **Since when**: the last working day by default (Friday when today is Monday); use the period the user names
   ("this week", "since the release").
2. **Done**: commits in that period by the user. Find their name with `git config user.name` and run
   `git log --since=<date> --author=<name> --oneline --no-merges` in the working folder. If the working folder holds
   several repositories, check each one directly inside it.
3. **In progress**: uncommitted changes (`git status --short`), stashes, and branches with recent commits that
   aren't merged.
4. **Next and blocked**: open items in `TODO.md`, `notes/todo.md` or similar files, `TODO` / `FIXME` comments added
   in the period (`git diff` of the period, not the whole code base), and failing tests if the project has a quick
   test command.

If there is no git history, say so and offer to summarise from the files changed most recently instead.

## Shape

Three short parts, in plain speech:

- **Yesterday** (or the period): what got done, grouped into two or three themes, not commit by commit. Numbers
  rounded ("about a dozen commits, mostly on the login flow").
- **Today**: what is in progress and the obvious next step.
- **Blockers**: anything failing, waiting on someone, or unclear. Say "no blockers" if there are none.

Keep it under 120 words. No commit hashes, file paths or branch names unless they are how the team refers to the
work. Don't claim work you can't see evidence for.

## After

Offer to write the briefing to `notes/standup-YYYY-MM-DD.md` with the commit list underneath, for pasting into a
chat. Do that without asking if the user asked for it "written up".
