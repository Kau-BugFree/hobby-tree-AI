import unittest

from next_activity.ranking import rank_home, recommend


def card(activity_id, stage=3, **changes):
    return {
        "activityId": activity_id,
        "hobbyId": 17,
        "stageId": 100 + stage,
        "stageNumber": stage,
        "title": f"테스트 활동 {activity_id}",
        "durationMinutes": 12,
        "estimatedCost": 1000,
        "requiredToolCount": 3,
        "onePan": False,
        "published": True,
        "startable": True,
        **changes,
    }


def request(cards, **changes):
    return {
        "treeStatus": "ACTIVE",
        "hobbyId": 17,
        "currentStageNumber": 3,
        "currentRecordId": None,
        "completedActivityIds": [],
        "availableMinutes": None,
        "budgetKrw": None,
        "preferredDifficulty": None,
        "signals": [],
        "candidates": cards,
        **changes,
    }


def ids(payload):
    return [item["activityId"] for item in recommend(payload)["items"]]


class RankingTests(unittest.TestCase):
    def test_resume_takes_precedence_over_new_candidates(self):
        result = recommend(request([card(10)], currentRecordId=41))
        self.assertEqual(
            result, {"status": "RESUME", "resumeRecordId": 41, "items": []}
        )

    def test_inactive_tree_cannot_resume(self):
        for status in ["PAUSED", "ENDED"]:
            with self.subTest(status=status):
                result = recommend(
                    request([card(1)], treeStatus=status, currentRecordId=9)
                )
                self.assertEqual(
                    result, {"status": "INACTIVE", "resumeRecordId": None, "items": []}
                )

    def test_only_available_same_hobby_uncompleted_unlocked_cards(self):
        cards = [
            card(1, startable=False),
            card(2, published=False),
            card(3, hobbyId=99),
            card(4, stage=4),
            card(5),
            card(6),
            card(7, stage=2),
        ]
        self.assertEqual(ids(request(cards, completedActivityIds=[5])), [6, 7])
        # After deletion of the last completion, BE can remove ID 5 from its snapshot.
        self.assertEqual(ids(request(cards)), [5, 6])

    def test_nearest_stage_then_stable_catalog_order_and_limit(self):
        cards = [card(4, stage=1), card(27, stage=2), card(83), card(6)]
        self.assertEqual(ids(request(cards)), [83, 6])
        self.assertEqual(ids(request(cards, limit=1)), [83])
        self.assertEqual(ids(request(cards[:2])), [27, 4])

    def test_time_and_budget_are_soft_preferences_including_at_zero(self):
        cards = [
            card(1, durationMinutes=None, estimatedCost=0),
            card(2, durationMinutes=10, estimatedCost=None),
            card(3, durationMinutes=21, estimatedCost=0),
            card(4, durationMinutes=20, estimatedCost=10),
            card(5, durationMinutes=20, estimatedCost=0),
        ]
        result = rank_home(request(cards, availableMinutes=20, budgetKrw=0))
        self.assertEqual(result["evaluatedCount"], 5)
        self.assertFalse(result["priceFallback"])
        within, unknown = result["items"]
        self.assertEqual((within["activityId"], within["budgetStatus"]), (5, "WITHIN"))
        self.assertEqual(
            (unknown["activityId"], unknown["budgetStatus"]), (2, "UNKNOWN")
        )
        self.assertIsNone(unknown["estimatedCost"])

    def test_unknowns_without_caps_are_retained_and_preserved(self):
        item = recommend(
            request(
                [
                    card(
                        1,
                        estimatedCost=None,
                        durationMinutes=None,
                        requiredToolCount=None,
                        onePan=None,
                    )
                ],
                signals=[
                    "PREFER_ONE_PAN",
                    "PREFER_SHORTER",
                    "PREFER_FEWER_TOOLS",
                    "PREFER_LOWER_COST",
                ],
            )
        )["items"][0]
        self.assertIsNone(item["durationMinutes"])
        self.assertEqual(item["reason"], "현재 단계의 미완료 활동입니다.")

    def test_hard_feedback_allows_previous_stage_to_win_on_confirmed_preference(self):
        cards = [card(8, stage=1, onePan=True), card(4, stage=2, onePan=True), card(3)]
        self.assertEqual(ids(request(cards, signals=["PREFER_ONE_PAN"])), [3, 4])
        result = recommend(
            request(cards, preferredDifficulty="HARD", signals=["PREFER_ONE_PAN"])
        )
        self.assertEqual([row["activityId"] for row in result["items"]], [4, 3])
        self.assertIn("팬 하나", result["items"][0]["reason"])
        self.assertNotIn("팬 하나", result["items"][1]["reason"])

    def test_hard_feedback_alone_prefers_immediately_preceding_stage(self):
        cards = [card(8), card(1, stage=1), card(2, stage=2)]
        self.assertEqual(ids(request(cards, preferredDifficulty="HARD")), [2, 8])

    def test_hard_feedback_still_prefers_current_when_it_matches_signals_better(self):
        cards = [card(2, stage=2), card(8, onePan=True)]
        payload = request(cards, preferredDifficulty="HARD", signals=["PREFER_ONE_PAN"])
        self.assertEqual(ids(payload), [8, 2])

    def test_hard_feedback_falls_back_to_current_when_previous_is_unavailable(self):
        cards = [card(1, stage=1), card(2, stage=2, startable=False), card(3)]
        self.assertEqual(ids(request(cards, preferredDifficulty="HARD")), [3, 1])

    def test_hard_feedback_in_first_stage_keeps_catalog_order_and_excludes_locked(self):
        cards = [card(3, stage=2), card(9, stage=1), card(2, stage=1)]
        payload = request(cards, currentStageNumber=1, preferredDifficulty="HARD")
        self.assertEqual(ids(payload), [9, 2])

    def test_small_independent_weights_are_stable_under_signal_reordering(self):
        cards = [
            card(1, durationMinutes=15, estimatedCost=0),
            card(2, durationMinutes=5, estimatedCost=500),
        ]
        self.assertEqual(
            ids(request(cards, signals=["PREFER_SHORTER", "PREFER_LOWER_COST"])), [1, 2]
        )
        self.assertEqual(
            ids(request(cards, signals=["PREFER_LOWER_COST", "PREFER_SHORTER"])), [1, 2]
        )

    def test_fewer_tools_prefers_verified_count_and_gives_evidence(self):
        cards = [
            card(2, requiredToolCount=3),
            card(1, requiredToolCount=None),
            card(3, requiredToolCount=1),
        ]
        result = recommend(request(cards, signals=["PREFER_FEWER_TOOLS"]))
        self.assertEqual([r["activityId"] for r in result["items"]], [3, 2])
        self.assertIn("필수 도구는 1개", result["items"][0]["reason"])

    def test_no_match_at_boundaries(self):
        for payload in [
            request([]),
            request([card(4, stage=6)], currentStageNumber=1),
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(
                    recommend(payload),
                    {"status": "NO_MATCH", "resumeRecordId": None, "items": []},
                )
        self.assertEqual(ids(request([card(6, stage=6)], currentStageNumber=6)), [6])

    def test_hundredth_card_is_scored_even_if_home_limit_is_one(self):
        cards = [card(i) for i in range(1, 100)] + [card(100, onePan=True)]
        result = rank_home(request(cards, signals=["PREFER_ONE_PAN"], limit=1))
        self.assertEqual(result["evaluatedCount"], 100)
        self.assertEqual([row["activityId"] for row in result["items"]], [100])

    def test_relax_cutoffs_to_fill_without_dropping_the_first_item(self):
        for other_stage, expected in [(3, "RELAXED"), (1, "OPEN")]:
            with self.subTest(stage=other_stage):
                cards = [card(1, stage=6), card(2, stage=other_stage)]
                result = rank_home(request(cards, currentStageNumber=6))
                self.assertEqual(
                    [(r["activityId"], r["tier"]) for r in result["items"]],
                    [(1, "STANDARD"), (2, expected)],
                )

    def test_entire_pool_below_cutoffs_still_returns_real_cards(self):
        cards = [card(3, stage=1, estimatedCost=None, durationMinutes=99)]
        previous = [
            {"category": category, "activityGroup": "prep", "polarity": "NEGATIVE"}
            for category in ("ENJOYMENT", "DIFFICULTY")
        ]
        cards[0]["activityGroup"] = "prep"
        payload = request(
            cards,
            currentStageNumber=6,
            availableMinutes=0,
            budgetKrw=0,
            groupPreferences=previous,
        )
        result = rank_home(payload)
        self.assertTrue(result["priceFallback"])
        self.assertEqual(len(result["items"]), 1)
        row = result["items"][0]
        self.assertEqual(
            (row["activityId"], row["score"], row["tier"]), (3, 25, "OPEN")
        )
        self.assertEqual(row["budgetStatus"], "UNKNOWN")
        self.assertIn("초과", row["reason"])

    def test_within_budget_bonuses_a_card_by_two_points_only(self):
        cards = [card(1, estimatedCost=100, stage=2), card(2, estimatedCost=5000)]
        result = rank_home(request(cards, budgetKrw=100))
        self.assertFalse(result["priceFallback"])
        over, within = result["items"]
        self.assertEqual(
            (over["activityId"], over["budgetStatus"], over["score"]), (2, "OVER", 88)
        )
        self.assertEqual(
            (within["activityId"], within["budgetStatus"], within["score"]),
            (1, "WITHIN", 81),
        )
        self.assertIn("5,000원", over["reason"])

    def test_all_over_budget_uses_price_within_band_even_if_scores_differ(self):
        cards = [
            card(1, estimatedCost=None),
            card(2, estimatedCost=800, onePan=True),
            card(3, estimatedCost=500),
        ]
        result = rank_home(request(cards, signals=["PREFER_ONE_PAN"], budgetKrw=0))
        self.assertTrue(result["priceFallback"])
        self.assertEqual(
            [(r["activityId"], r["score"]) for r in result["items"]], [(3, 88), (2, 89)]
        )

    def test_stronger_fit_band_precedes_cheaper_weak_fit_band(self):
        cards = [card(1, estimatedCost=500, stage=2), card(2, estimatedCost=50000)]
        result = rank_home(request(cards, budgetKrw=0))
        self.assertEqual([r["activityId"] for r in result["items"]], [2, 1])
        self.assertTrue(result["priceFallback"])

    def test_budget_fallback_preserves_free_zero_and_unknown_order(self):
        cards = [card(1, estimatedCost=None), card(2, estimatedCost=900)]
        fallback = rank_home(request(cards, budgetKrw=0))
        self.assertEqual(
            [r["budgetStatus"] for r in fallback["items"]], ["OVER", "UNKNOWN"]
        )
        cards[1]["estimatedCost"] = 0
        normal = rank_home(request(cards, budgetKrw=0))
        self.assertFalse(normal["priceFallback"])
        self.assertEqual(normal["items"][0]["budgetStatus"], "WITHIN")
        self.assertFalse(rank_home(request(cards))["priceFallback"])

    def test_each_of_the_six_aspects_has_at_most_one_point_of_influence(self):
        baseline = card(3, activityGroup="KNEAD", onePan=True)
        plain = request([baseline])
        score = rank_home(plain)["items"][0]["score"]
        for category in ["ENJOYMENT", "DIFFICULTY"]:
            for polarity, delta in [("POSITIVE", 1), ("NEGATIVE", -1)]:
                preference = {
                    "activityGroup": "KNEAD",
                    "category": category,
                    "polarity": polarity,
                }
                match = rank_home({**plain, "groupPreferences": [preference]})
                self.assertEqual(match["items"][0]["score"], score + delta)
                other = rank_home(
                    {
                        **plain,
                        "groupPreferences": [{**preference, "activityGroup": "CHOP"}],
                    }
                )
                self.assertEqual(other["items"][0]["score"], score)
        prefs = [
            {"activityGroup": "KNEAD", "category": cat, "polarity": "POSITIVE"}
            for cat in ["ENJOYMENT", "DIFFICULTY"]
        ]
        signals = [
            "PREFER_SHORTER",
            "PREFER_FEWER_TOOLS",
            "PREFER_LOWER_COST",
            "PREFER_ONE_PAN",
        ]
        for signal in signals:
            self.assertEqual(
                rank_home({**plain, "signals": [signal]})["items"][0]["score"],
                score + 1,
            )
        strongest = rank_home(
            {**plain, "budgetKrw": 1000, "groupPreferences": prefs, "signals": signals}
        )
        self.assertEqual(strongest["items"][0]["score"], 96)

    def test_numeric_aspect_is_normalized_across_all_known_cards(self):
        cards = [
            card(1, requiredToolCount=11),
            card(2, requiredToolCount=6),
            card(3, requiredToolCount=1),
            card(4, requiredToolCount=None),
        ]
        result = rank_home(request(cards, signals=["PREFER_FEWER_TOOLS"]))
        self.assertEqual(
            [(r["activityId"], r["score"]) for r in result["items"]],
            [(3, 89), (2, 88.5)],
        )

    def test_only_valid_completed_cards_can_be_recommended_for_replay(self):
        cards = [
            card(1),
            card(2, published=False),
            card(3, hobbyId=19),
            card(4, stage=5),
            card(5, startable=False),
        ]
        result = rank_home(request(cards, completedActivityIds=[1, 2, 3, 4, 5]))
        self.assertEqual(result["evaluatedCount"], 1)
        self.assertEqual(
            [(r["activityId"], r["isRepeat"]) for r in result["items"]], [(1, True)]
        )
        self.assertIn("다시", result["items"][0]["reason"])
        cards.append(card(8, stage=1))
        new_only = rank_home(request(cards, completedActivityIds=[1, 2, 3, 4, 5]))
        self.assertEqual(
            [(r["activityId"], r["isRepeat"]) for r in new_only["items"]], [(8, False)]
        )

    def test_empty_or_unusable_catalog_has_actionable_offline_content(self):
        for cards in [
            [],
            [
                card(1, startable=False),
                card(2, stage=4),
                card(3, hobbyId=99),
                card(4, published=False),
            ],
        ]:
            result = rank_home(request(cards))
            self.assertEqual(result["status"], "SELF_GUIDED")
            self.assertEqual(result["items"], [])
            self.assertEqual(result["evaluatedCount"], 0)
            self.assertFalse(result["priceFallback"])
            self.assertEqual(
                set(result["alternative"]), {"kind", "title", "instruction"}
            )
            self.assertEqual(result["alternative"]["kind"], "SELF_GUIDED")
            self.assertTrue(result["alternative"]["instruction"].strip())

    def test_inactive_or_in_progress_home_always_provides_a_display_target(self):
        for status in ["ENDED", "PAUSED"]:
            output = rank_home(
                request([card(1)], treeStatus=status, currentRecordId=31)
            )
            self.assertEqual(output["status"], "INACTIVE")
            self.assertEqual(output["evaluatedCount"], 0)
            self.assertTrue(output["alternative"]["title"])
        resume = rank_home(request([], currentRecordId=31))
        self.assertEqual(resume["status"], "RESUME")
        self.assertEqual(resume["resumeRecordId"], 31)
        self.assertIsNone(resume["alternative"])

    def test_v1_projects_same_results_without_returning_v2_fields(self):
        payload = request(
            [card(1), card(2, onePan=True)],
            signals=["PREFER_ONE_PAN"],
            completedActivityIds=[1, 2],
        )
        legacy, current = recommend(payload), rank_home(payload)
        self.assertEqual(set(legacy), {"status", "items", "resumeRecordId"})
        self.assertEqual(current["items"][0]["activityId"], 2)
        for before, after in zip(legacy["items"], current["items"], strict=True):
            self.assertEqual(
                before,
                {
                    k: after[k]
                    for k in [
                        "activityId",
                        "stageId",
                        "stageNumber",
                        "title",
                        "durationMinutes",
                        "reason",
                    ]
                },
            )


if __name__ == "__main__":
    unittest.main()
