# court, with refinement

Put your idea on trial in front of a jury of Claudes, then let a **founder** keep refining and
defending it until the jury acquits and the judge agrees, or an **arbiter** rules it can no longer be
defended.

A fork of the MIT-licensed [court skill](https://github.com/alexyc9381/court-skill) by Alex Chen
([@nocodealex](https://instagram.com/nocodealex)). The original trial is unchanged at its core; this
fork adds the refinement loop and a few fairness fixes.

## The trial

1. A **prosecutor** Claude builds the strongest honest case that the idea fails as described.
2. A **defense** Claude builds the strongest honest case that it works.
3. Each side answers the other's points.
4. Up to **12 jurors** (an accountant, a first-time customer, a competitor, a retired judge, a
   teenager, an engineer, a small-business owner, a statistician, a parent, an investor, a designer, a
   lawyer) vote GUILTY or NOT GUILTY on their own, with a confidence score. None sees another's vote.
5. A **judge** reads the verdict, gives an independent `SATISFIED / NOT SATISFIED` ruling, and lists
   the charges that stuck and the conditions for acquittal.

## The refinement loop (`--refine`)

6. The **founder**, an entrepreneur, rewrites the idea to answer the charges that stuck. It keeps your
   goal and copies your facts word for word; every addition is marked as a *proposed* change, never as
   something already done. If it cannot honestly answer enough charges, it rests.
7. The **arbiter**, who takes no side, checks the revision: does it really answer the charges, invent
   nothing, stay the same idea, and break new ground? It rules:
   - `CONTINUE`: retrial, with the founder in the defense seat;
   - `REDO`: one fix-up per round (for example an invented fact or drift into a different idea);
   - `NEEDS USER`: asks you up to 3 questions; your answers go into the next case file;
   - `EXHAUSTED`: the idea cannot be defended any further.
8. The retrial runs in full. The prosecutor and judge see earlier rounds and test whether each fix
   actually holds.

**It stops when** the jury votes NOT GUILTY *and* the judge is satisfied, the founder rests, the
arbiter rules exhausted, or the round limit is reached. `VERDICT.md` shows the verdict, the
round-by-round road to it, every juror's reason and the judge's ruling. An acquittal after refinement
acquits the *refined* idea, and only if its proposed changes are actually made.

**It never touches your files.** Everything is written inside `.court/` in the folder you run it in.

## Install

As a plugin, in Claude Code:

```
/plugin marketplace add srijith9862/court-skill-with-refinement
/plugin install court-refine@court-skill-with-refinement
```

It then shows up as `/court-refine:court`. If you also have the original `court-skill` plugin, turn it
off so the two don't both answer when you just say "put it on trial".

Or copy the skill folder yourself:

```bash
git clone https://github.com/srijith9862/court-skill-with-refinement
cp -r court-skill-with-refinement/skills/court ~/.claude/skills/
```

Installed that way, it is `/court`.

## Use

```
/court-refine:court --quick --refine Quit my job to sell candles on Etsy full time. I have $8,000 saved and 40 sales so far.
```

| flag | meaning |
| --- | --- |
| `--jury N` | 3 to 12 jurors. Default 12. |
| `--quick` | 6 jurors. |
| `--refine` | Turn on the founder/arbiter loop. |
| `--rounds R` | With `--refine`: at most R trials in total, 2 to 5. Default 3. |
| `--seed S` | Fixes which jurors sit on a smaller jury. |

A single trial is 17 sub-agent calls (11 with `--quick`). With `--refine`, each further round adds a
founder, an arbiter and a full retrial: `--quick --refine` is 11 to 37 calls, a full jury with 3
rounds up to 55. Every sub-agent counts toward your Claude plan's usage. Turn on accept-edits mode
(Shift+Tab) first so you are not asked to approve every file. See `PROMPTS.md` for ready-made cases.

## How it works

`skills/court/court.py` does all the bookkeeping: it opens the trial, writes one brief per sub-agent
per round, checks every output (and its format), counts the votes, decides when the loop stops and
renders the verdict. Claude only orchestrates as the clerk: it never argues, votes, refines the idea
or edits the verdict.

```bash
python3 court.py status                  # where the trial stands
python3 court.py refine --rounds 3       # add the loop to a trial that already finished
python3 court.py answer --file a.md      # your answers to the arbiter's questions (or --none)
python3 court.py stop --reason "..."     # end it early
```

Tests:

```bash
python3 -m unittest discover -s tests
```

## Uninstall

```
/plugin uninstall court-refine@court-skill-with-refinement
```

or `rm -rf ~/.claude/skills/court` if you copied the folder.

Not made by Anthropic. The jury is made of language models, so treat the verdict as a structured
second opinion, not legal, financial or professional advice.
