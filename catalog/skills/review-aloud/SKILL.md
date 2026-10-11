---
name: review-aloud
description: Review the current code changes and report the verdict in a way that works out loud - a spoken summary with the most important findings, and the full review written to a file. Use when the user asks to "review my changes", "check what I did", "is this ready to commit", "look over the diff", or wants a second opinion on uncommitted or recent work.
---

# Review aloud

A good review read out in full is too long to follow by ear. Do the full review, write it down, and speak only
the verdict and what matters most.

## 1. Find what to review

- Uncommitted work first: `git status` and `git diff` (staged and unstaged). If there is none, the commits on this
  branch that aren't on the main branch (`git log --oneline main..HEAD` or `master`), then the last commit.
- If the user named files, a branch or a pull request, review that instead.
- If there is no git repository, ask which files to look at.

## 2. Review

Read every changed hunk with enough surrounding code to understand it. Look for, in this order:

1. **Bugs**: wrong logic, off-by-one, unhandled errors or empty cases, races, resource leaks, broken callers of a
   changed function.
2. **Security**: secrets in code, injection, unchecked input, unsafe file or network access.
3. **Tests**: changed behaviour with no test; run the project's tests if that is quick and safe (look for the usual
   runner: `pytest`, `npm test`, `cargo test`, `go test ./...`).
4. **Clarity**: names, dead code, duplicated logic, comments that no longer match.

Note each finding with the file, the line, why it matters and a suggested fix. Don't invent problems to fill the
list; "nothing wrong" is a valid result.

## 3. Write it down

Write `review-notes.md` in the working folder (or the place the user asked for), replacing any earlier one:

- a one-line verdict (ready / ready with small fixes / not ready);
- findings grouped by severity (must fix, should fix, nice to have), each with `path:line`;
- what you ran (tests, linters) and the results.

## 4. Say it

In at most four spoken sentences: the verdict, how many findings of each kind, and the one or two that matter most
in plain words ("The new retry loop never stops if the server keeps failing."). Don't read out paths, line numbers
or code; say the full list is in review notes. Then offer to fix the must-fix items.

Change nothing while reviewing. Fix only after the user says so.
