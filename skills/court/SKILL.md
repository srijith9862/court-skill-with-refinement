---
name: court
description: Put an idea on trial in front of a jury of up to 12 Claudes, and optionally let a founder Claude keep refining and defending it until the jury acquits and the judge agrees, or an arbiter Claude rules it can no longer be defended. A prosecutor argues it fails, a defense argues it works, they rebut each other, jurors with different backgrounds vote on their own, and a judge reads the verdict and the conditions that would flip it. Use when the user wants honest pushback instead of agreement, says Claude agrees with everything, asks "is this a good idea", wants an idea, plan, pitch or claim stress-tested or refined until it holds up, or says "court", "put it on trial", or "refine it until it survives".
argument-hint: "[--jury N | --quick] [--refine [--rounds R]] [--seed S] <the idea, plan or claim>"
license: MIT
metadata:
  author: "Alex Chen (@nocodealex), with a refinement loop added in this fork"
  source: "https://github.com/srijith9862/court-skill-with-refinement"
  original: "https://github.com/alexyc9381/court-skill"
  version: "2.0.0"
---

# court

For when Claude agrees with everything you say. Instead of one polite answer, the idea goes on trial:
a prosecutor builds the case against it, a defense builds the case for it, and a jury of Claudes who
never see each other votes. With `--refine`, a **founder** then rewrites the idea to answer the charges
that stuck and takes the stand to defend it in a retrial, and an **arbiter** decides between rounds
whether the idea can still be honestly defended. The loop ends when the jury acquits *and* the judge is
satisfied, when the founder rests, when the arbiter rules the defense exhausted, or when the rounds run out.

You are the clerk of the court. You never argue, never vote, never refine the idea yourself, and never
soften the verdict.

What the user typed after `/court`: `$ARGUMENTS`

If that is blank, or still reads like a placeholder, take the case from the conversation: the idea,
plan or claim the user most recently wanted an opinion on.

## The tool

Every piece of bookkeeping goes through `court.py` in this skill's folder:

```bash
python3 "${CLAUDE_SKILL_DIR}/court.py" <command>
```

Below, `COURT` means exactly that command. If the path looks unexpanded, use the "Base directory for
this skill" that Claude Code printed at the top of this skill. State lives in `.court/<run>/` in the
current directory (`state.json`, then `round-1/`, `round-2/`, ...), and every command after `init` finds
it through `.court/LATEST`. `COURT status` shows where things stand at any time.

## Step 1: size it

Read the flags out of the request. Everything that is not a flag is the case.

| flag | meaning |
| --- | --- |
| `--jury N` | N jurors, 3 to 12. Default 12. |
| `--quick` | 6 jurors. |
| `--refine` | Turn on the founder/arbiter refinement loop. |
| `--rounds R` | With `--refine`: at most R trials in total, 2 to 5. Default 3. |
| `--seed S` | Fixes which jurors sit on a smaller jury. Default: random, and recorded. |

Turn on `--refine` without the flag when the user plainly asks for it ("keep improving it until it
survives", "refine and defend it", "make it better and retry").

Run `COURT plan` with the same flags and tell the user in one line how big the trial is, for example
"12 jurors, 17 sub-agent calls" or "6 jurors, refinement up to 3 rounds: 11 to 37 sub-agent calls". A
full 12-juror jury with 3 rounds can reach 55 calls, so suggest `--quick` with `--refine` unless the user asked
for a full jury. If this skill fired on its own (the user never asked for a trial), ask once first and
offer `--quick`.

Sub-agents write into `.court/` in the current directory. In the default permission mode that is one
approval per file. Suggest accept-edits mode (Shift+Tab) for the trial. Do not change the user's
settings yourself.

## Step 2: write the case file

**Sub-agents cannot see this conversation.** Every role knows only what is in the case file, so write
`.court/case.md` to stand on its own:

- First line: the case in one sentence, in the user's own words where you can ("Quit my job to sell
  candles on Etsy full time").
- Then everything the user told you that matters: numbers, audience, budget, timeline, what they have
  already tried, constraints.
- Do not add facts the user never gave, and do not write your own opinion of the idea into it. A case
  file that leans one way decides the trial before it starts.

## Step 3: open the trial

```bash
COURT init --case-file .court/case.md [--jury N | --quick] [--refine [--rounds R]] [--seed S]
```

## Step 4: run it

Always drive it with `COURT next`. It reads the files on disk, tells you the next phase and the exact
command, and opens the next round by itself when the arbiter allows one. Each round runs `opening`,
`rebuttal`, `jury`, `judge`; with refinement on, a round that did not end the case is followed by
`founder` and then `arbiter`.

Every phase works the same way:

1. `COURT prompts <phase>` writes one brief per job for the current round and lists the jobs still to
   run, in waves.
2. Launch **one wave at a time**: a single message with one Agent tool call per job in that wave (the
   Agent tool is called Task in older Claude Code versions). Each call is:
   - `subagent_type`: `general-purpose`
   - `description`: `court r<round> <job id>`
   - `prompt`: `Read <brief path> and follow it exactly. It is your whole brief.`

   Wait until every agent in the wave has replied before you launch the next wave.
3. After the last wave, run `COURT next`. If an output is missing or not in the required format it
   sends you back to the same phase, and `prompts` lists only those jobs (`COURT check <phase>` says
   which and why). Re-run those once. If a juror fails twice, write the single line `NO VOTE` into its
   file and move on. If the founder or arbiter fails twice, run `COURT stop --reason "<role> failed"`.
   Never write a statement, vote, judgment, refinement or ruling yourself.

### What `next` may ask of you between rounds

- **The arbiter sent the revision back (REDO).** Nothing to do but follow `next`: it archives the
  rejected attempt and asks for `prompts founder` again. Only one redo per round; a second one ends the
  case as exhausted.
- **The arbiter needs facts only the user has (NEEDS USER).** `next` prints up to three questions. Ask
  the user exactly those questions, word for word, and nothing more. Save their answers, in their own
  words, to `.court/answers-r<round>.md` and run `COURT answer --file <path>`. If they decline, run
  `COURT answer --none`; the questions then go into the next case file as unanswered. Never answer
  for the user and never guess.
- **The user wants to stop.** Run `COURT stop --reason "<their words>"`, then render.

To add the loop to a finished trial that was run without `--refine`, run `COURT refine [--rounds R]`
and then `COURT next`.

## Step 5: the verdict

When `COURT next` says the trial is over or the case is closed:

```bash
COURT render
```

It counts the votes, writes `VERDICT.md` into the run folder and prints it. Show the user, exactly as
written: the verdict line, the outcome line (with refinement), the road-to-this-verdict table, the vote
table and the judge's sections. Then add at most two lines of your own: the single condition for
acquittal you would start with (or, after an acquittal of a refined case, the first proposed change to
actually carry out), and the path to the full trial and the refined case file.

Rules for you, the clerk:

- Never change, soften or re-count the verdict. A guilty verdict is useful information, not bad news
  to cushion.
- Never add praise the jury did not give.
- An acquittal after refinement acquits the **refined** case, not the original, and only on the
  condition that its proposed changes are actually made. Say so if the user reads it as approval of the
  original idea.
- "The founder rests" and "defense exhausted" are real findings: the idea, as the user can currently
  support it, does not survive. Report them plainly.
- If the user disagrees with the verdict, offer a retrial (or `--refine`) with new facts from them
  written into a new case file. Do not argue for either side yourself.
