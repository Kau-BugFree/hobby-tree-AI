import unittest

from next_activity.feedback import analyze_feedback


class FeedbackTests(unittest.TestCase):
    def test_one_pan_request_has_grounded_signals_and_explicit_rating(self):
        note = "요리는 재미있었고 난이도도 적당했어요. 설거지가 많았어요. 다음엔 팬 하나로 끝나는 걸 하고 싶어요."
        result = analyze_feedback(note, "", "APPROPRIATE")
        self.assertEqual(result["difficulty"], "APPROPRIATE")
        self.assertEqual(result["sentiment"], "POSITIVE")
        self.assertEqual(result["signals"], ["PREFER_FEWER_TOOLS", "PREFER_ONE_PAN"])
        self.assertTrue(result["evidence"])
        self.assertTrue(all(span and span in note for span in result["evidence"]))

    def test_hard_and_positive_can_coexist(self):
        result = analyze_feedback("어려웠지만 재미있었어요.", "", None)
        self.assertEqual(
            (result["difficulty"], result["sentiment"]), ("HARD", "POSITIVE")
        )

    def test_user_rating_wins_and_conflicting_spans_do_not_supply_evidence(self):
        result = analyze_feedback("너무 쉬워서 지루했어요.", "", "HARD")
        self.assertEqual(result["difficulty"], "HARD")
        self.assertEqual(result["sentiment"], "NEGATIVE")
        self.assertNotIn("쉬워서", result["evidence"])

    def test_cost_and_time_do_not_imply_low_skill(self):
        result = analyze_feedback(
            "준비물이 너무 비싸요.", "시간이 부족해서 못 했어요.", None
        )
        self.assertEqual(result["signals"], ["PREFER_SHORTER", "PREFER_LOWER_COST"])
        self.assertIsNone(result["difficulty"])

    def test_acquisition_difficulty_is_not_activity_difficulty(self):
        for note in ("재료를 구하기 어려웠어요.", "재료를 찾기가 어려웠어요."):
            with self.subTest(note=note):
                result = analyze_feedback(note, "", None)
                self.assertIsNone(result["difficulty"])
                self.assertEqual(result["evidence"], [])

    def test_past_negation_and_speculation_are_not_observations(self):
        for note in ("어려웠던 적은 없어요.", "해보지도 않았는데 어려웠을 것 같아요."):
            with self.subTest(note=note):
                result = analyze_feedback(note, "", None)
                self.assertIsNone(result["difficulty"])
                self.assertEqual(result["evidence"], [])

    def test_personal_experience_after_hearsay(self):
        result = analyze_feedback("어렵다고 들었지만 직접 해 보니 쉬웠어요.", "", None)
        self.assertEqual(result["difficulty"], "EASY")
        self.assertEqual(result["evidence"], ["쉬웠어요"])

    def test_negated_sentiment_is_not_positive(self):
        result = analyze_feedback("안 재미있었어요.", "", None)
        self.assertEqual(result["sentiment"], "UNKNOWN")
        self.assertEqual(result["evidence"], [])

    def test_negations_do_not_trigger_positive_claims(self):
        note = "재미있지는 않았어요. 어렵지 않았고 안 쉬웠어요. 오래 걸리지 않았어요. 팬 하나로 하고 싶지 않아요."
        self.assertEqual(
            analyze_feedback(note, "", None),
            {
                "difficulty": None,
                "signals": [],
                "sentiment": "UNKNOWN",
                "evidence": [],
            },
        )

    def test_independent_clause_after_negation_still_counts(self):
        result = analyze_feedback(
            "오래 걸리지 않았어요. 그래도 준비물이 너무 비싸요.", "", None
        )
        self.assertEqual(result["signals"], ["PREFER_LOWER_COST"])

    def test_neutral_mention_and_no_feedback(self):
        for note in ("", "팬 하나로 요리했습니다.", "끝냈어요."):
            with self.subTest(note=note):
                self.assertEqual(
                    analyze_feedback(note, "", None),
                    {
                        "difficulty": None,
                        "signals": [],
                        "sentiment": "UNKNOWN",
                        "evidence": [],
                    },
                )

    def test_mixed_sentiment_and_uncertain_difficulty(self):
        note = (
            "처음은 쉬웠어요. 나중에는 어려웠어요. 재미있었지만 기다리기는 지루했어요."
        )
        result = analyze_feedback(note, "", None)
        self.assertIsNone(result["difficulty"])
        self.assertEqual(result["sentiment"], "MIXED")

    def test_no_time_and_disclaimed_difficulty(self):
        result = analyze_feedback(
            "어렵다고 생각하지 않아요.", "시간이 없어서 못 했어요.", None
        )
        self.assertIsNone(result["difficulty"])
        self.assertEqual(result["signals"], ["PREFER_SHORTER"])

    def test_unique_signals(self):
        result = analyze_feedback(
            "시간이 너무 오래 걸렸어요.", "시간이 부족해요.", None
        )
        self.assertEqual(result["signals"], ["PREFER_SHORTER"])


if __name__ == "__main__":
    unittest.main()
