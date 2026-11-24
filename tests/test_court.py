import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "skills", "court"))
import court  # noqa: E402

FOUNDER_OK = ("STATUS: REFINED\nCHANGE SUMMARY: test on weekends before quitting.\n\n"
              "## Answers to the charges\n1. Keep the job for 3 months.\n\n"
              "## Revised case\nSell candles on Etsy on weekends for 3 months before quitting\n\n"
              "### Facts from the user\nI have $8,000 saved.\n")


class CourtTest(unittest.TestCase):
    def setUp(self):
        self.old = os.getcwd()
        self.tmp = tempfile.mkdtemp()
        os.chdir(self.tmp)
        os.makedirs(".court")
        with open(".court/case.md", "w") as f:
            f.write("Quit my job to sell candles on Etsy full time\n\nI have $8,000 saved.\n")

    def tearDown(self):
        os.chdir(self.old)
        shutil.rmtree(self.tmp)

    # ---- helpers ----

    def cli(self, *argv):
        buf = io.StringIO()
        with redirect_stdout(buf):
            court.main(list(argv))
        return buf.getvalue()

    def open_trial(self, *extra):
        self.cli("init", "--case-file", ".court/case.md", "--seed", "7", *extra)
        return court.load_state()

    def write_all(self, state, phase, text):
        for j in court.jobs(state, phase):
            with open(j["output"], "w") as f:
                f.write(text)

    def play_round(self, votes, ruling="NOT SATISFIED"):
        state = court.load_state()
        self.write_all(state, "opening", "1. point")
        self.write_all(state, "rebuttal", "1. answer")
        for j, v in zip(court.jobs(state, "jury"), votes):
            with open(j["output"], "w") as f:
                f.write("NO VOTE" if v == "NO VOTE" else "VOTE: %s\nCONFIDENCE: 4\nREASON: because | pipes\n" % v)
        self.write_all(state, "judge", "RULING: %s\n\n## The sentence\nKeep the job for now." % ruling)
        return state

    def write_one(self, state, phase, text):
        with open(court.jobs(state, phase)[0]["output"], "w") as f:
            f.write(text)

    # ---- original behaviour ----

    def test_jurors_are_distinct(self):
        jurors = court.load_jurors()
        self.assertEqual(len(jurors), 12)
        self.assertEqual(len({j["name"] for j in jurors}), 12)

    def test_quick_jury_is_six_distinct(self):
        state = self.open_trial("--quick")
        self.assertEqual(len(state["jury"]), 6)
        self.assertEqual(len({j["name"] for j in state["jury"]}), 6)

    def test_briefs_name_their_output_and_the_record(self):
        state = self.open_trial("--jury", "3")
        self.cli("prompts", "jury")
        j = court.jobs(state, "jury")[0]
        text = court.read(j["brief"])
        self.assertIn(j["output"], text)
        self.assertIn("VOTE: GUILTY or NOT GUILTY", text)
        self.assertIn("CONFIDENCE:", text)
        self.assertIn(os.path.abspath(os.path.join(court.round_dir(state), "rebuttal", "defense.md")), text)

    def test_jurors_alternate_reading_order(self):
        state = self.open_trial("--jury", "3")
        self.cli("prompts", "jury")
        a, b = [court.read(j["brief"]) for j in court.jobs(state, "jury")[:2]]
        self.assertLess(a.index("opening/prosecution.md"), a.index("opening/defense.md"))
        self.assertLess(b.index("opening/defense.md"), b.index("opening/prosecution.md"))

    def test_every_brief_builds_in_every_round(self):
        # Regression: the judge brief once raised "not all arguments converted during string formatting".
        self.open_trial("--jury", "3", "--refine")
        for rnd in (1, 2):
            state = self.play_round(["GUILTY"] * 3)
            for phase in court.PHASES:
                self.cli("prompts", phase)
                for j in court.jobs(state, phase):
                    text = court.read(j["brief"])
                    self.assertIn(j["output"], text)
                    self.assertNotIn("%s", text)
            judge = court.read(court.jobs(state, "judge")[0]["brief"])
            self.assertIn("RULING: SATISFIED or NOT SATISFIED", judge)
            self.assertEqual("## Did the refinements hold" in judge, rnd == 2)
            if rnd == 1:
                self.write_one(state, "founder", FOUNDER_OK)
                self.write_one(state, "arbiter", "RULING: CONTINUE\nREASON: ok\n")
                self.cli("next")

    def test_parse_vote_variants(self):
        v = court.parse_vote("VOTE: NOT GUILTY\nCONFIDENCE: 3\nREASON: it pays.\nDECIDING FACT: $8,000\n"
                             "WOULD FLIP IF: no savings")
        self.assertEqual((v["vote"], v["confidence"], v["flip"]), ("NOT GUILTY", 3, "no savings"))
        self.assertEqual(court.parse_vote("**VOTE:** guilty\nREASON: x")["vote"], "GUILTY")
        self.assertEqual(court.parse_vote("VOTE: Not-Guilty")["vote"], "NOT GUILTY")
        self.assertIsNone(court.parse_vote("NO VOTE"))

    def test_full_trial_tally_and_render(self):
        self.open_trial("--jury", "5")
        state = self.play_round(["GUILTY", "GUILTY", "NOT GUILTY", "GUILTY", "NO VOTE"])
        t = court.tally(state)
        self.assertEqual((t["verdict"], t["guilty"], t["not_guilty"], t["no_vote"]), ("GUILTY", 3, 1, 1))
        self.assertEqual(court.verdict_line(t), "GUILTY, 3 to 1")
        text = court.render(state)
        self.assertIn("# The verdict: GUILTY, 3 to 1", text)
        self.assertIn("Quit my job to sell candles", text)
        self.assertIn("because / pipes", text)
        self.assertIn("**The judge:** not satisfied", text)
        self.assertNotIn("RULING:", text)
        self.assertIn("## The sentence", text)
        self.assertNotIn("The road to this verdict", text)
        self.assertTrue(text.rstrip().endswith(court.CREDIT))

    def test_hung_jury(self):
        state = self.open_trial("--jury", "4")
        for j, v in zip(court.jobs(state, "jury"), ["GUILTY", "NOT GUILTY", "GUILTY", "NOT GUILTY"]):
            with open(j["output"], "w") as f:
                f.write("VOTE: %s\n" % v)
        self.assertEqual(court.verdict_line(court.tally(state)), "HUNG JURY, 2 to 2")

    def test_next_walks_the_phases(self):
        state = self.open_trial("--jury", "3")
        self.assertIn("round 1 opening", self.cli("next"))
        self.write_all(state, "opening", "x")
        self.assertIn("round 1 rebuttal", self.cli("next"))

    def test_without_refine_the_trial_just_ends(self):
        self.open_trial("--jury", "3")
        self.play_round(["GUILTY"] * 3)
        self.assertIn("the trial is over", self.cli("next"))
        self.assertIsNone(court.load_state()["outcome"])

    # ---- refinement loop ----

    def test_plan_counts_refinement_calls(self):
        out = self.cli("plan", "--quick", "--refine", "--rounds", "3")
        # trial = 2 + 2 + 6 + 1 = 11; worst = 11 + 2 * (11 + 2) = 37
        self.assertIn("11 to 37", out)

    def test_acquittal_in_round_one_needs_the_judge_too(self):
        self.open_trial("--jury", "3", "--refine")
        self.play_round(["NOT GUILTY"] * 3, ruling="NOT SATISFIED")
        self.assertIn("round 1 founder", self.cli("next"))
        state = court.load_state()
        self.write_one(state, "judge", "RULING: SATISFIED\n\n## The sentence\nGo.")
        self.assertIn("ACQUITTED", self.cli("next"))
        self.assertEqual(court.load_state()["outcome"], "acquitted")

    def test_refine_loop_until_acquitted(self):
        self.open_trial("--jury", "3", "--refine", "--rounds", "3")
        self.play_round(["GUILTY"] * 3)
        state = court.load_state()
        self.cli("prompts", "founder")
        brief = court.read(court.jobs(state, "founder")[0]["brief"])
        self.assertIn("You are the FOUNDER", brief)
        self.assertIn("Never invent facts", brief)
        self.write_one(state, "founder", FOUNDER_OK)
        self.assertIn("round 1 arbiter", self.cli("next"))
        self.write_one(state, "arbiter", "RULING: CONTINUE\nREASON: real answers.\n")
        out = self.cli("next")
        self.assertIn("Round 2 opened", out)
        state = court.load_state()
        self.assertEqual(state["round"], 2)
        case2 = court.read(court.case_path(state))
        self.assertTrue(case2.startswith("Sell candles on Etsy on weekends"))
        # round 2: the founder takes the defense seat, the prosecutor sees the history
        self.cli("prompts", "opening")
        p, d = [court.read(j["brief"]) for j in court.jobs(state, "opening")]
        self.assertIn("round 1 judgment", p)
        self.assertIn("You are the FOUNDER, taking the stand", d)
        self.play_round(["NOT GUILTY", "NOT GUILTY", "GUILTY"], ruling="SATISFIED")
        self.assertIn("ACQUITTED", self.cli("next"))
        text = self.cli("render")
        self.assertIn("# The verdict: NOT GUILTY, 2 to 1", text)
        self.assertIn("The road to this verdict", text)
        self.assertIn("| 1 | GUILTY, 3 to 0 | not satisfied | test on weekends before quitting. | continue |", text)
        self.assertIn("**The refined case (round 2):** Sell candles on Etsy on weekends", text)
        self.assertIn("a plan, not a result", text)

    def test_founder_rests(self):
        self.open_trial("--jury", "3", "--refine")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", "STATUS: REST\nCHANGE SUMMARY: no savings runway can be found.\n")
        self.assertIn("THE FOUNDER RESTS", self.cli("next"))
        self.assertEqual(court.load_state()["outcome"], "founder-rests")

    def test_malformed_founder_output_counts_as_missing(self):
        self.open_trial("--jury", "3", "--refine")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", "I think the idea is great.")
        self.assertTrue(court.missing(state, "founder"))
        self.assertIn("not in the required format", self.cli("check", "founder"))

    def test_arbiter_exhausted_stops(self):
        self.open_trial("--jury", "3", "--refine")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", FOUNDER_OK)
        self.write_one(state, "arbiter", "RULING: EXHAUSTED\nREASON: circling.\n")
        self.assertIn("DEFENSE EXHAUSTED", self.cli("next"))

    def test_arbiter_redo_once_then_exhausted(self):
        self.open_trial("--jury", "3", "--refine")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", FOUNDER_OK)
        self.write_one(state, "arbiter", "RULING: REDO\nREASON: invented a sales figure.\n")
        self.assertIn("founder (redo)", self.cli("next"))
        self.assertTrue(court.missing(state, "founder"))
        self.cli("prompts", "founder")
        self.assertIn("sent your first attempt", court.read(court.jobs(state, "founder")[0]["brief"]))
        self.write_one(state, "founder", FOUNDER_OK)
        self.write_one(state, "arbiter", "RULING: REDO\nREASON: still invented.\n")
        self.assertIn("DEFENSE EXHAUSTED", self.cli("next"))

    def test_arbiter_needs_user_then_answers_flow_into_case(self):
        self.open_trial("--jury", "3", "--refine")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", FOUNDER_OK)
        self.write_one(state, "arbiter", "RULING: NEEDS USER\nREASON: no sales history.\n"
                                         "Q: How many sales did you make each month?\n")
        out = self.cli("next")
        self.assertIn("ask the user: How many sales did you make each month?", out)
        with open("answers.md", "w") as f:
            f.write("Month 1: 8, month 2: 14, month 3: 18.")
        self.cli("answer", "--file", "answers.md")
        self.assertIn("Round 2 opened", self.cli("next"))
        case2 = open(court.case_path(court.load_state())).read()
        self.assertIn("New facts from the user (given after round 1)", case2)
        self.assertIn("month 3: 18", case2)

    def test_needs_user_declined_records_open_questions(self):
        self.open_trial("--jury", "3", "--refine")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", FOUNDER_OK)
        self.write_one(state, "arbiter", "RULING: NEEDS USER\nREASON: x\nQ: What is your rent?\n")
        self.cli("next")
        self.cli("answer", "--none")
        self.cli("next")
        case2 = open(court.case_path(court.load_state())).read()
        self.assertIn("Questions the user chose not to answer", case2)
        self.assertIn("- What is your rent?", case2)

    def test_out_of_rounds(self):
        self.open_trial("--jury", "3", "--refine", "--rounds", "2")
        state = self.play_round(["GUILTY"] * 3)
        self.write_one(state, "founder", FOUNDER_OK)
        self.write_one(state, "arbiter", "RULING: CONTINUE\nREASON: ok\n")
        self.cli("next")
        self.play_round(["GUILTY", "GUILTY", "NOT GUILTY"])
        self.assertIn("OUT OF ROUNDS", self.cli("next"))

    def test_refine_can_be_turned_on_after_a_trial(self):
        self.open_trial("--jury", "3")
        self.play_round(["GUILTY"] * 3)
        self.cli("refine", "--rounds", "2")
        self.assertIn("round 1 founder", self.cli("next"))

    def test_closed_case_refuses_new_prompts(self):
        self.open_trial("--jury", "3", "--refine")
        self.cli("stop", "--reason", "user ended it")
        with self.assertRaises(SystemExit):
            self.cli("prompts", "opening")
        self.assertIn("STOPPED", self.cli("render"))


if __name__ == "__main__":
    unittest.main()
