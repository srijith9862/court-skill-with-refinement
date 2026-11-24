#!/usr/bin/env python3
"""Bookkeeping for the court skill: put an idea on trial in front of a jury of Claudes, and
(optionally) let a founder keep refining and defending it until it is acquitted or can no longer
be defended.

The skill's orchestrator (Claude, "the clerk") drives the trial with these commands. Every
sub-agent reads a brief this script writes and writes exactly one output file; this script checks
the files, counts the votes, decides when the refinement loop stops and renders the verdict.
All state lives in .court/<run>/ in the current directory.

    python3 court.py plan    [--jury N | --quick] [--refine] [--rounds R]
    python3 court.py init    --case-file PATH [--jury N | --quick] [--seed S] [--refine] [--rounds R]
    python3 court.py next                 # what to do now (also advances to the next round)
    python3 court.py prompts <phase>      # write briefs for the current round, list jobs in waves
    python3 court.py check   <phase>
    python3 court.py status
    python3 court.py refine  [--rounds R] # turn the refinement loop on for a finished trial
    python3 court.py answer  (--file PATH | --none)   # the user's answers to the arbiter's questions
    python3 court.py stop    [--reason TEXT]
    python3 court.py tally
    python3 court.py render

Trial phases, in order, every round: opening, rebuttal, jury, judge.
Refinement phases, after a round that did not end the case: founder, arbiter.

Derived from the MIT-licensed court skill by Alex Chen (@nocodealex); see LICENSE.
"""
import argparse
import json
import os
import random
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = ".court"
LATEST = os.path.join(ROOT, "LATEST")
TRIAL_PHASES = ["opening", "rebuttal", "jury", "judge"]
REFINE_PHASES = ["founder", "arbiter"]
PHASES = TRIAL_PHASES + REFINE_PHASES
DEFAULT_JURY = 12
QUICK_JURY = 6
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 5
MAX_REDOS = 1
WAVE = 6
CREDIT = ("_Made with [/court](https://github.com/alexyc9381/court-skill), a free Claude Code skill by "
          "Alex Chen ([@nocodealex](https://instagram.com/nocodealex)). Refinement loop added in [court-skill-with-refinement](https://github.com/srijith9862/court-skill-with-refinement)._")
CHARGE = "The case fails as stated: it should not go ahead the way it is described."

OUTCOMES = {
    "acquitted": "ACQUITTED: the jury found for the idea and the judge agreed",
    "founder-rests": "THE FOUNDER RESTS: the founder could not defend the idea any further",
    "exhausted": "DEFENSE EXHAUSTED: the arbiter ruled the idea cannot be defended any further",
    "out-of-rounds": "OUT OF ROUNDS: the refinement limit was reached before an acquittal",
    "stopped": "STOPPED: the trial was ended early",
}


# ---------- state ----------

def load_jurors():
    with open(os.path.join(HERE, "jurors.json")) as f:
        return json.load(f)


def run_dir():
    if not os.path.exists(LATEST):
        sys.exit("court: no trial yet. Run: court.py init --case-file .court/case.md")
    with open(LATEST) as f:
        return f.read().strip()


def load_state(rd=None):
    rd = rd or run_dir()
    with open(os.path.join(rd, "state.json")) as f:
        return json.load(f)


def save_state(state):
    with open(os.path.join(state["dir"], "state.json"), "w") as f:
        json.dump(state, f, indent=2)


def jury_size(args):
    if getattr(args, "quick", False):
        return QUICK_JURY
    n = getattr(args, "jury", None) or DEFAULT_JURY
    if n < 3 or n > 12:
        sys.exit("court: the jury must be 3 to 12 Claudes")
    return n


def rounds_arg(args, default=DEFAULT_ROUNDS):
    r = getattr(args, "rounds", None) or default
    if r < 2 or r > MAX_ROUNDS:
        sys.exit("court: --rounds must be 2 to %d (round 1 is the original trial)" % MAX_ROUNDS)
    return r


def read(path):
    if not os.path.exists(path):
        return ""
    with open(path) as f:
        return f.read()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


# ---------- paths ----------

def round_dir(state, rnd=None):
    return os.path.join(state["dir"], "round-%d" % (rnd or state["round"]))


def _abs(state, rnd, *parts):
    return os.path.abspath(os.path.join(round_dir(state, rnd), *parts))


def case_path(state, rnd=None):
    return _abs(state, rnd, "case.md")


def open_round(state, rnd, case_text):
    rd = round_dir(state, rnd)
    for sub in ("briefs", "opening", "rebuttal", "jury", "judge", "refine"):
        os.makedirs(os.path.join(rd, sub), exist_ok=True)
    write(os.path.join(rd, "case.md"), case_text.strip() + "\n")


# ---------- jobs ----------

def jobs(state, phase, rnd=None):
    """Each job: id, brief path, output path."""
    rnd = rnd or state["round"]
    if phase in ("opening", "rebuttal"):
        ids, folder = ["prosecution", "defense"], phase
    elif phase == "jury":
        ids, folder = ["juror-%02d" % (i + 1) for i in range(len(state["jury"]))], phase
    elif phase == "judge":
        ids, folder = ["judge"], phase
    elif phase in REFINE_PHASES:
        ids, folder = [phase], "refine"
    else:
        sys.exit("court: unknown phase %r (phases: %s)" % (phase, ", ".join(PHASES)))
    return [{"id": jid,
             "phase": phase,
             "brief": _abs(state, rnd, "briefs", "%s-%s.md" % (phase, jid)),
             "output": _abs(state, rnd, folder, "%s.md" % jid)} for jid in ids]


def done(job):
    text = read(job["output"]).strip()
    if not text:
        return False
    if job["phase"] == "founder":
        return parse_founder(text) is not None
    if job["phase"] == "arbiter":
        return parse_arbiter(text) is not None
    return True


def missing(state, phase, rnd=None):
    return [j for j in jobs(state, phase, rnd) if not done(j)]


def trial_complete(state, rnd=None):
    return not any(missing(state, p, rnd) for p in TRIAL_PHASES)


# ---------- parsing ----------

VOTE_RE = re.compile(r"^\W*VOTE\W*:\W*(NOT[\s_-]+GUILTY|GUILTY)\b", re.I | re.M)
FIELD_RE = r"^\W*%s\W*:\s*(.+)$"


def field(text, name):
    m = re.search(FIELD_RE % re.escape(name).replace(r"\ ", r"\s+"), text or "", re.I | re.M)
    return m.group(1).strip().strip("*").strip() if m else ""


def parse_vote(text):
    m = VOTE_RE.search(text or "")
    if not m:
        return None
    out = {"vote": "NOT GUILTY" if "NOT" in m.group(1).upper() else "GUILTY"}
    for key, name in (("reason", "REASON"), ("fact", "DECIDING FACT"), ("flip", "WOULD FLIP IF")):
        out[key] = field(text, name)
    c = re.search(r"[1-5]", field(text, "CONFIDENCE"))
    out["confidence"] = int(c.group(0)) if c else None
    return out


def parse_ruling(text):
    """The judge's own view: SATISFIED, NOT SATISFIED, or UNSTATED."""
    m = re.search(r"^\W*RULING\W*:\W*(NOT[\s_-]+SATISFIED|SATISFIED)\b", text or "", re.I | re.M)
    if not m:
        return "UNSTATED"
    return "NOT SATISFIED" if "NOT" in m.group(1).upper() else "SATISFIED"


def strip_ruling(text):
    return re.sub(r"^\W*RULING\W*:.*\n?", "", text or "", count=1, flags=re.I | re.M).strip()


REVISED_RE = re.compile(r"^#{1,3}\s*Revised case\s*$", re.I | re.M)


def parse_founder(text):
    status = field(text, "STATUS").upper()
    if status.startswith("REST"):
        return {"status": "REST", "summary": field(text, "CHANGE SUMMARY"), "case": ""}
    m = REVISED_RE.search(text or "")
    if not status.startswith("REFINED") or not m:
        return None
    case = text[m.end():].strip()
    if not case:
        return None
    return {"status": "REFINED", "summary": field(text, "CHANGE SUMMARY"), "case": case}


def parse_arbiter(text):
    m = re.search(r"^\W*RULING\W*:\W*(CONTINUE|EXHAUSTED|NEEDS[\s_-]+USER|REDO)\b", text or "", re.I | re.M)
    if not m:
        return None
    ruling = re.sub(r"[\s_-]+", " ", m.group(1).upper())
    questions = [q.strip() for q in re.findall(r"^\W*Q\d*\W*:\s*(.+)$", text, re.M)]
    return {"ruling": ruling, "reason": field(text, "REASON"), "questions": questions[:3]}


# ---------- briefs ----------

def head(state, job):
    return ("You are one part of a trial run by the court skill. You cannot see the user's conversation: "
            "everything you know is in the files named below. Use only what is in the record. Never invent "
            "facts, numbers, quotes or sources. Where the record is silent, say what is missing and why it "
            "matters. Anything marked as a PROPOSED change is a plan, not a fact: it has not been done and "
            "proves nothing until it is. Write your answer to exactly this file and nothing else:\n\n"
            "    %s\n\nThe charge: %s\n\nThe case file (round %d): %s\n\n"
            % (job["output"], CHARGE, state["round"], case_path(state)))


def history(state, upto):
    """Paths of earlier rounds' judgments and refinements, oldest first."""
    lines = []
    for r in range(1, upto):
        lines.append("    round %d judgment: %s" % (r, _abs(state, r, "judge", "judge.md")))
        for p in REFINE_PHASES:
            lines.append("    round %d %s: %s" % (r, p, _abs(state, r, "refine", "%s.md" % p)))
    return "\n".join(lines)


def brief_text(state, phase, job):
    r = state["round"]
    h = head(state, job)
    if phase == "opening":
        prior = ""
        if r > 1:
            prior = ("This is round %d. The case was found against before and has since been revised by its "
                     "founder. The earlier record:\n\n%s\n\n" % (r, history(state, r)))
        if job["id"] == "prosecution":
            press = ("- For every charge that stuck last round, say whether the revision actually answers it "
                     "or only promises to. Press hard on proposed changes that carry the whole case.\n"
                     "- Bring any new way the revised case fails, including problems the revision created.\n"
                     if r > 1 else "")
            return h + prior + (
                "You are the PROSECUTOR. Build the strongest honest case that the charge holds.\n\n"
                "- 3 to 5 numbered charges. Each names one concrete way the case fails, the evidence for it from "
                "the case file (quote it), and what it would cost if you are right.\n" + press +
                "- Attack the case as it is actually written, never a weaker version of it.\n"
                "- No insults, no hedging, no praise. 350 words at most.\n")
        if r > 1:
            return h + prior + (
                "You are the FOUNDER, taking the stand for the defense of your own revised idea. You believe "
                "in it, but you are honest: a defense built on spin loses juries.\n\n"
                "- 3 to 5 numbered points. Each names one concrete strength of the case as it now stands, the "
                "evidence for it (quote the case file), and why it outweighs the obvious objection.\n"
                "- Show, charge by charge, how the revision answers what stuck last round. Never claim a "
                "proposed change is already done.\n"
                "- If a part still cannot be defended, concede it in one line.\n- 350 words at most.\n")
        return h + (
            "You are the DEFENSE. Build the strongest honest case that the charge does NOT hold: the case "
            "should go ahead as described.\n\n"
            "- 3 to 5 numbered points. Each names one concrete strength, the evidence for it from the case "
            "file (quote it), and why it outweighs the obvious objection.\n"
            "- Defend the case as the user actually wrote it. If a part cannot be defended, concede it in one "
            "line instead of spinning it.\n- 350 words at most.\n")
    if phase == "rebuttal":
        side, other = ("prosecution", "defense") if job["id"] == "prosecution" else ("defense", "prosecution")
        role = "FOUNDER (for the defense)" if (side == "defense" and r > 1) else side.upper()
        return h + (
            "You are the %s. Read the other side's opening statement:\n\n    %s\n\n"
            "Answer each of its numbered points in order: say whether it survives, and why, in one or two "
            "sentences each. Concede the ones that survive. Then name the one point the jury should decide "
            "on. 250 words at most.\n" % (role, _abs(state, r, "opening", other + ".md")))
    if phase == "jury":
        idx = int(job["id"].split("-")[1]) - 1
        juror = state["jury"][idx]
        # Alternate the reading order so no side always gets the first word.
        order = ("prosecution", "defense") if idx % 2 == 0 else ("defense", "prosecution")
        record = "\n".join("    " + _abs(state, r, p, s + ".md") for p in ("opening", "rebuttal") for s in order)
        return h + (
            "You are JUROR %d of %d: %s. %s\n\n"
            "Read the case file and all four statements, in this order:\n\n%s\n\n"
            "Decide on your own: you will never see the other jurors, or any earlier verdict. You owe the "
            "case nothing. Voting for it to be kind, or against it to look tough, is contempt of court. "
            "Judge it through your own eyes as described above. A proposed change counts only as far as it "
            "is concrete, cheap to verify and likely to be done.\n\n"
            "Write exactly these five lines and nothing else:\n\n"
            "VOTE: GUILTY or NOT GUILTY\n"
            "CONFIDENCE: 1 to 5 (5 = certain)\n"
            "REASON: one sentence, in your own voice\n"
            "DECIDING FACT: the one fact from the record that decided it\n"
            "WOULD FLIP IF: the one change to the case that would make you vote the other way\n"
            % (idx + 1, len(state["jury"]), juror["name"], juror["lens"], record))
    if phase == "judge":
        held = ""
        if r > 1:
            held = ("## Did the refinements hold\nFor each charge that stuck in round %d, one line: answered, "
                    "partly answered, or still standing.\n\n" % (r - 1))
        prior = ("\nThe earlier rounds:\n\n%s\n" % history(state, r)) if r > 1 else ""
        return h + (
            "You are the JUDGE. The jury has voted and you cannot overturn the vote. Read the case file, the "
            "four statements in %s and %s, every juror's vote in %s, and the count in %s.%s\n\n"
            % (_abs(state, r, "opening"), _abs(state, r, "rebuttal"), _abs(state, r, "jury"),
               _abs(state, r, "tally.json"), prior) +
            "Your first line is your own view, separate from the jury's, of whether the case as it now stands "
            "has answered the charges well enough to go ahead:\n\n"
            "RULING: SATISFIED or NOT SATISFIED\n\n"
            "Be as hard to satisfy as a careful judge would be: unproven promises do not satisfy you.\n\n"
            "Then write these sections, plainly, for someone who has never seen a courtroom:\n\n"
            "## Charges that stuck\nThe 3 objections the jurors cited most, each in one sentence, with how "
            "many jurors leaned on it.\n\n" + held +
            "## What the defense got right\nThe 2 strongest points in the case's favour.\n\n"
            "## Conditions for acquittal\nThe 3 specific changes that would flip the most votes, built from "
            "the jurors' WOULD FLIP IF lines. Each one is something the user can do this week.\n\n"
            "## The sentence\nOne short paragraph in a judge's voice: what the user should do next.\n\n"
            "300 words at most. Do not repeat the vote count: it is printed above your judgment.\n")
    if phase == "founder":
        rejected = _abs(state, r, "refine", "arbiter-redo.md")
        redo = ""
        if os.path.exists(rejected):
            redo = ("The arbiter sent your first attempt this round back. Read its ruling and fix exactly what "
                    "it names:\n\n    %s\n    (your rejected attempt: %s)\n\n"
                    % (rejected, _abs(state, r, "refine", "founder-rejected.md")))
        prior = ("Earlier rounds, so you never repeat an answer that already failed:\n\n%s\n\n"
                 % history(state, r)) if r > 1 else ""
        return h + (
            "You are the FOUNDER: an entrepreneur whose job is to make this idea better, not to make it "
            "look better. The idea just lost (or did not clearly win) round %d. Read the case file, the "
            "judgment %s, the tally %s and every juror's vote in %s.\n\n%s%s"
            "Refine the idea so it answers the charges that stuck and meets the conditions for acquittal.\n\n"
            "Rules of refinement:\n"
            "- Keep the core of the idea: the user's goal and what they are trying to build. You may change "
            "scope, sequence, customer, pricing, channel, risk controls, or add a cheap test first. You may "
            "NOT swap in a different idea.\n"
            "- Never invent facts. Everything the user told you is copied over word for word. Every change "
            "you add is written as a PROPOSED change: what to do, what it costs, how the user would know "
            "it worked. Anything only the user can supply goes under Open questions.\n"
            "- Concede a charge you cannot answer without inventing facts or changing the idea.\n"
            "- If you cannot answer enough of the charges to flip votes, stop honestly: write only the "
            "line STATUS: REST and one line CHANGE SUMMARY: why the idea cannot be defended further.\n\n"
            "Otherwise write exactly this, with the Revised case last:\n\n"
            "STATUS: REFINED\n"
            "CHANGE SUMMARY: one sentence on what changed\n\n"
            "## Answers to the charges\nOne numbered line per charge that stuck: the change that answers "
            "it, or CONCEDED.\n\n"
            "## Revised case\nA complete, standalone case file that the next trial will read with no other "
            "context. First line: the refined case in one sentence. Then '### Facts from the user' (every "
            "fact from the current case file, unchanged), '### Proposed changes (not yet done)', and "
            "'### Open questions'. 500 words at most for the revised case.\n"
            % (r, _abs(state, r, "judge", "judge.md"), _abs(state, r, "tally.json"), _abs(state, r, "jury"),
               redo, prior))
    if phase == "arbiter":
        traj = "\n".join("    round %d: %s, judge %s" % (x["round"], x["verdict"], x["ruling"])
                         for x in trajectory(state))
        return h + (
            "You are the ARBITER. You take no side. A founder has revised the idea after round %d. Your only "
            "job is to rule whether the idea can still be honestly defended in another round, or whether "
            "the defense is exhausted. Read the case file, the round %d judgment %s, the founder's revision "
            "%s, and the earlier rounds:\n\n%s\n\nThe verdicts so far:\n\n%s\n\n"
            "Check each of these:\n"
            "1. Answers: does the revision give a concrete, new answer to most of the charges that stuck?\n"
            "2. Honesty: are the user's facts carried over unchanged, with no invented facts, numbers or "
            "results, and every addition marked as proposed?\n"
            "3. Same idea: is it still the user's idea, or has it drifted into a different one?\n"
            "4. New ground: does it avoid repeating answers that already failed in earlier rounds?\n"
            "5. Progress: is the case getting stronger round over round, or circling?\n\n"
            "Rule one of:\n"
            "- CONTINUE: all five checks pass well enough for a fair retrial.\n"
            "- REDO: the revision could pass but breaks check 2 or 3 in a way the founder can fix now "
            "(name exactly what to fix).\n"
            "- NEEDS USER: the remaining charges can only be answered with facts only the user has. Ask "
            "for them.\n"
            "- EXHAUSTED: the answers are thin, repeated, or circling, or the only way out is a different "
            "idea. Say so plainly.\n\n"
            "Write exactly this and nothing else:\n\n"
            "RULING: CONTINUE, REDO, NEEDS USER or EXHAUSTED\n"
            "REASON: one sentence\n"
            "CHECKS:\n- Answers: ...\n- Honesty: ...\n- Same idea: ...\n- New ground: ...\n- Progress: ...\n"
            "Then, only for NEEDS USER, up to 3 lines of the form  Q: <a question only the user can answer>\n"
            % (r, r, _abs(state, r, "judge", "judge.md"), _abs(state, r, "refine", "founder.md"),
               history(state, r) or "    (none: this is the first revision)", traj))
    raise ValueError(phase)


# ---------- votes ----------

def tally(state, rnd=None):
    rnd = rnd or state["round"]
    rows = []
    for j, juror in zip(jobs(state, "jury", rnd), state["jury"]):
        v = parse_vote(read(j["output"])) if done(j) else None
        rows.append({"juror": juror["name"], "vote": v["vote"] if v else "NO VOTE",
                     "confidence": v["confidence"] if v else None,
                     "reason": v["reason"] if v else "", "fact": v["fact"] if v else "",
                     "flip": v["flip"] if v else ""})
    guilty = sum(1 for r in rows if r["vote"] == "GUILTY")
    not_guilty = sum(1 for r in rows if r["vote"] == "NOT GUILTY")
    if guilty > not_guilty:
        verdict = "GUILTY"
    elif not_guilty > guilty:
        verdict = "NOT GUILTY"
    else:
        verdict = "HUNG JURY"
    result = {"round": rnd, "verdict": verdict, "guilty": guilty, "not_guilty": not_guilty,
              "no_vote": len(rows) - guilty - not_guilty, "jurors": rows}
    write(os.path.join(round_dir(state, rnd), "tally.json"), json.dumps(result, indent=2))
    return result


def verdict_line(t):
    if t["verdict"] == "HUNG JURY":
        return "HUNG JURY, %d to %d" % (t["guilty"], t["not_guilty"])
    big, small = (t["guilty"], t["not_guilty"]) if t["verdict"] == "GUILTY" else (t["not_guilty"], t["guilty"])
    return "%s, %d to %d" % (t["verdict"], big, small)


def judge_ruling(state, rnd=None):
    return parse_ruling(read(jobs(state, "judge", rnd)[0]["output"]))


def trajectory(state):
    out = []
    for r in range(1, state["round"] + 1):
        if not trial_complete(state, r):
            continue
        t = tally(state, r)
        f = parse_founder(read(jobs(state, "founder", r)[0]["output"])) or {}
        a = parse_arbiter(read(jobs(state, "arbiter", r)[0]["output"])) or {}
        out.append({"round": r, "verdict": verdict_line(t), "ruling": judge_ruling(state, r),
                    "founder": f.get("status", ""), "change": f.get("summary", ""),
                    "arbiter": a.get("ruling", "")})
    return out


# ---------- the loop ----------

def advance(state):
    """Open the next round from the founder's revised case (plus any facts the user gave)."""
    r = state["round"]
    f = parse_founder(read(jobs(state, "founder", r)[0]["output"]))
    case = f["case"]
    answers = state.get("answers", {}).get(str(r))
    if answers is not None:
        a = parse_arbiter(read(jobs(state, "arbiter", r)[0]["output"]))
        if answers.strip():
            case += "\n\n### New facts from the user (given after round %d)\n\n%s\n" % (r, answers.strip())
        elif a and a["questions"]:
            case += ("\n\n### Questions the user chose not to answer (after round %d)\n\n%s\n"
                     % (r, "\n".join("- " + q for q in a["questions"])))
    state["round"] = r + 1
    open_round(state, r + 1, case)
    save_state(state)


def finish(state, outcome, note=""):
    state["outcome"] = outcome
    if note:
        state["outcome_note"] = note
    save_state(state)
    print("court: %s." % OUTCOMES[outcome])
    if note:
        print("court: %s" % note)
    print("court: the case is closed. Run: python3 court.py render")


def step(state):
    """Print the next thing for the clerk to do. Advances rounds and closes the case as needed."""
    if state.get("outcome"):
        print("court: the case is closed (%s). Run: python3 court.py render" % OUTCOMES[state["outcome"]])
        return
    r = state["round"]
    for phase in TRIAL_PHASES:
        if missing(state, phase):
            if phase == "judge":
                print("court: round %d, the jury voted %s." % (r, verdict_line(tally(state))))
            print("next phase: round %d %s. Run: python3 court.py prompts %s" % (r, phase, phase))
            return
    t = tally(state)
    ruling = judge_ruling(state)
    if not state.get("refine"):
        print("court: the trial is over (%s, judge %s). Run: python3 court.py render"
              % (verdict_line(t), ruling.lower()))
        return
    if t["verdict"] == "NOT GUILTY" and ruling == "SATISFIED":
        return finish(state, "acquitted", "round %d: %s, and the judge is satisfied." % (r, verdict_line(t)))
    if r >= state["max_rounds"]:
        return finish(state, "out-of-rounds", "round %d of %d: %s, judge %s."
                      % (r, state["max_rounds"], verdict_line(t), ruling.lower()))
    if missing(state, "founder"):
        print("court: round %d ended %s, judge %s. The founder refines the idea."
              % (r, verdict_line(t), ruling.lower()))
        print("next phase: round %d founder. Run: python3 court.py prompts founder" % r)
        return
    f = parse_founder(read(jobs(state, "founder")[0]["output"]))
    if f["status"] == "REST":
        return finish(state, "founder-rests", f["summary"])
    if missing(state, "arbiter"):
        print("next phase: round %d arbiter. Run: python3 court.py prompts arbiter" % r)
        return
    a = parse_arbiter(read(jobs(state, "arbiter")[0]["output"]))
    if a["ruling"] == "REDO":
        redos = state.setdefault("redos", {})
        if redos.get(str(r), 0) >= MAX_REDOS:
            return finish(state, "exhausted", "the founder's second attempt in round %d was sent back too: %s"
                          % (r, a["reason"]))
        redos[str(r)] = redos.get(str(r), 0) + 1
        out = os.path.dirname(jobs(state, "founder")[0]["output"])
        os.replace(os.path.join(out, "founder.md"), os.path.join(out, "founder-rejected.md"))
        os.replace(os.path.join(out, "arbiter.md"), os.path.join(out, "arbiter-redo.md"))
        save_state(state)
        print("court: the arbiter sent the revision back: %s" % a["reason"])
        print("next phase: round %d founder (redo). Run: python3 court.py prompts founder" % r)
        return
    if a["ruling"] == "EXHAUSTED":
        return finish(state, "exhausted", a["reason"])
    if a["ruling"] == "NEEDS USER" and str(r) not in state.get("answers", {}):
        print("court: the arbiter needs facts only the user has: %s" % a["reason"])
        for q in a["questions"] or ["(the arbiter named no question: ask what it says is missing)"]:
            print("  ask the user: %s" % q)
        print("Ask the user these questions word for word. Save their answers, in their own words, to a file "
              "and run: python3 court.py answer --file <path>   (or, if they decline: python3 court.py answer --none)")
        return
    advance(state)
    print("court: the arbiter allows a retrial. Round %d opened from the founder's revised case: %s"
          % (state["round"], case_path(state)))
    print("next phase: round %d opening. Run: python3 court.py prompts opening" % state["round"])


# ---------- render ----------

def first_line(text):
    for line in (text or "").splitlines():
        if line.strip():
            return line.lstrip("# ").strip()
    return "(empty case)"


def render(state):
    rounds = [r for r in range(1, state["round"] + 1) if trial_complete(state, r)]
    last = rounds[-1] if rounds else state["round"]
    t = tally(state, last)
    judge = jobs(state, "judge", last)[0]
    judgment = read(judge["output"]).strip()
    ruling = parse_ruling(judgment)
    judgment = strip_ruling(judgment) if done(judge) else "_The judge has not ruled yet._"
    refined = state.get("refine") and last > 1
    title = "# The verdict: %s" % verdict_line(t)
    lines = [title, ""]
    if state.get("outcome"):
        lines += ["**Outcome:** %s." % OUTCOMES[state["outcome"]], ""]
        if state.get("outcome_note"):
            lines += ["_%s_" % state["outcome_note"], ""]
    lines += ["**The case:** %s" % first_line(read(case_path(state, 1)))]
    if refined:
        lines += ["", "**The refined case (round %d):** %s" % (last, first_line(read(case_path(state, last))))]
    lines += ["", "**The charge:** %s" % CHARGE, "",
              "**The judge:** %s" % {"SATISFIED": "satisfied", "NOT SATISFIED": "not satisfied",
                                     "UNSTATED": "gave no ruling"}[ruling], ""]
    if state.get("refine"):
        lines += ["## The road to this verdict", "", "| Round | Jury | Judge | Founder's change | Arbiter |",
                  "|---|---|---|---|---|"]
        for x in trajectory(state):
            lines.append("| %d | %s | %s | %s | %s |" % (
                x["round"], x["verdict"], x["ruling"].lower(),
                (x["change"] or ("rested" if x["founder"] == "REST" else "")).replace("|", "/"),
                x["arbiter"].lower()))
        lines.append("")
        if refined:
            lines += ["_Everything under \"Proposed changes\" in the refined case is a plan, not a result. "
                      "The verdict holds only if those changes are actually made._", ""]
    lines += ["## How the jury voted" + (" (round %d)" % last if refined else ""), "",
              "| Juror | Vote | Sure | Why |", "|---|---|---|---|"]
    for r in t["jurors"]:
        lines.append("| %s | %s | %s | %s |" % (r["juror"], r["vote"],
                                               "%d/5" % r["confidence"] if r["confidence"] else "-",
                                               r["reason"].replace("|", "/")))
    lines += ["", judgment, ""]
    if refined:
        lines += ["_The refined case file is %s._" % case_path(state, last), ""]
    lines += ["_The full trial is in %s: every round's statements, votes, judgments and refinements._"
              % os.path.abspath(state["dir"]), "", CREDIT]
    text = "\n".join(lines) + "\n"
    write(os.path.join(state["dir"], "VERDICT.md"), text)
    return text


# ---------- commands ----------

def calls(n, rounds):
    trial = 2 + 2 + n + 1
    return trial, trial + (rounds - 1) * (trial + 2)


def cmd_plan(args):
    n = jury_size(args)
    trial, _ = calls(n, 1)
    if not args.refine:
        print("jury of %d: 2 opening statements, 2 rebuttals, %d jurors, 1 judge = %d sub-agent calls"
              % (n, n, trial))
        return
    rounds = rounds_arg(args)
    _, worst = calls(n, rounds)
    print("jury of %d, refinement on, up to %d rounds: %d calls per trial, plus a founder and an arbiter "
          "between trials = %d to %d sub-agent calls (it stops early on acquittal or exhaustion)"
          % (n, rounds, trial, trial, worst))


def cmd_init(args):
    n = jury_size(args)
    if not os.path.exists(args.case_file):
        sys.exit("court: case file not found: %s" % args.case_file)
    case = read(args.case_file).strip()
    if not case:
        sys.exit("court: the case file is empty")
    seed = args.seed if args.seed is not None else random.randrange(1, 10 ** 6)
    jurors = load_jurors()
    jury = jurors[:] if n == len(jurors) else random.Random(seed).sample(jurors, n)
    os.makedirs(ROOT, exist_ok=True)
    rd = os.path.join(ROOT, time.strftime("%Y%m%d-%H%M%S"))
    k = 1
    while os.path.exists(rd):
        k += 1
        rd = os.path.join(ROOT, time.strftime("%Y%m%d-%H%M%S") + "-%d" % k)
    os.makedirs(rd)
    state = {"dir": rd, "seed": seed, "jury": jury, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
             "round": 1, "refine": bool(args.refine),
             "max_rounds": rounds_arg(args) if args.refine else 1,
             "outcome": None, "answers": {}, "redos": {}}
    open_round(state, 1, case)
    save_state(state)
    write(LATEST, rd)
    print("court: trial opened in %s (jury of %d, seed %d%s)"
          % (rd, n, seed, ", refinement up to %d rounds" % state["max_rounds"] if args.refine else ""))
    print("next: python3 court.py prompts opening")


def cmd_prompts(args):
    state = load_state()
    if state.get("outcome"):
        sys.exit("court: the case is closed. Run: court.py render")
    if args.phase in REFINE_PHASES and not state.get("refine"):
        sys.exit("court: refinement is off. Run: court.py refine")
    todo = missing(state, args.phase)
    for j in jobs(state, args.phase):
        write(j["brief"], brief_text(state, args.phase, j))
    if not todo:
        print("court: round %d %s is complete. Run: court.py next" % (state["round"], args.phase))
        return
    for w in range(0, len(todo), WAVE):
        print("wave %d:" % (w // WAVE + 1))
        for j in todo[w:w + WAVE]:
            print("  %s  brief=%s" % (j["id"], j["brief"]))
    print("Launch one Agent call per job, one wave at a time. Prompt: "
          "\"Read <brief> and follow it exactly. It is your whole brief.\"")


def cmd_check(args):
    state = load_state()
    todo = missing(state, args.phase)
    if not todo:
        print("court: round %d %s complete" % (state["round"], args.phase))
    for j in todo:
        bad = " (present but not in the required format)" if read(j["output"]).strip() else ""
        print("missing: %s -> %s%s" % (j["id"], j["output"], bad))


def cmd_status(args):
    state = load_state()
    print("court: %s, round %d of %d, refinement %s%s" % (
        state["dir"], state["round"], state["max_rounds"], "on" if state.get("refine") else "off",
        ", closed: " + OUTCOMES[state["outcome"]] if state.get("outcome") else ""))
    for x in trajectory(state):
        print("  round %d: %s, judge %s%s%s" % (
            x["round"], x["verdict"], x["ruling"].lower(),
            ", founder %s" % x["founder"].lower() if x["founder"] else "",
            ", arbiter %s" % x["arbiter"].lower() if x["arbiter"] else ""))


def cmd_refine(args):
    state = load_state()
    if state.get("outcome"):
        sys.exit("court: the case is closed. Open a new trial with init --refine")
    state["refine"] = True
    state["max_rounds"] = max(rounds_arg(args), state["round"] + 1)
    save_state(state)
    print("court: refinement on, up to %d rounds. Run: court.py next" % state["max_rounds"])


def cmd_answer(args):
    state = load_state()
    if args.none:
        text = ""
    else:
        if not os.path.exists(args.file):
            sys.exit("court: answers file not found: %s" % args.file)
        text = read(args.file).strip()
        if not text:
            sys.exit("court: the answers file is empty (use --none if the user declined)")
    state.setdefault("answers", {})[str(state["round"])] = text
    save_state(state)
    print("court: %s. Run: court.py next" % ("answers recorded" if text else "the user declined; noted"))


def cmd_stop(args):
    state = load_state()
    finish(state, "stopped", args.reason or "")


def cmd_next(args):
    step(load_state())


def cmd_tally(args):
    print(verdict_line(tally(load_state())))


def cmd_render(args):
    sys.stdout.write(render(load_state()))


def main(argv=None):
    p = argparse.ArgumentParser(prog="court.py")
    sub = p.add_subparsers(dest="cmd")
    for name in ("plan", "init"):
        sp = sub.add_parser(name)
        sp.add_argument("--jury", type=int)
        sp.add_argument("--quick", action="store_true")
        sp.add_argument("--refine", action="store_true")
        sp.add_argument("--rounds", type=int)
        if name == "init":
            sp.add_argument("--case-file", required=True)
            sp.add_argument("--seed", type=int)
    for name in ("prompts", "check"):
        sub.add_parser(name).add_argument("phase", choices=PHASES)
    sub.add_parser("refine").add_argument("--rounds", type=int)
    pa = sub.add_parser("answer")
    g = pa.add_mutually_exclusive_group(required=True)
    g.add_argument("--file")
    g.add_argument("--none", action="store_true")
    sub.add_parser("stop").add_argument("--reason")
    for name in ("next", "status", "tally", "render"):
        sub.add_parser(name)
    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 1
    {"plan": cmd_plan, "init": cmd_init, "prompts": cmd_prompts, "check": cmd_check,
     "status": cmd_status, "refine": cmd_refine, "answer": cmd_answer, "stop": cmd_stop,
     "next": cmd_next, "tally": cmd_tally, "render": cmd_render}[args.cmd](args)
    return 0


if __name__ == "__main__":
    sys.exit(main())
