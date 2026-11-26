import unittest

from next_activity.absa import analyze_aspects


def by_category(memo="", reason=""):
    return {aspect["category"]: aspect for aspect in analyze_aspects(memo, reason)}


def labels(memo="", reason=""):
    return {
        key: (aspect["polarity"], aspect["signal"])
        for key, aspect in by_category(memo, reason).items()
    }


class AspectTests(unittest.TestCase):
    def test_canonical_multi_aspect_feedback(self):
        memo = "요리는 재미있었지만 시간이 너무 오래 걸렸어요. 다음에는 팬 하나로 끝나는 걸 하고 싶어요."
        result = by_category(memo)
        self.assertEqual(list(result), ["ENJOYMENT", "DURATION", "COOKING_METHOD"])
        self.assertEqual(
            labels(memo),
            {
                "ENJOYMENT": ("POSITIVE", None),
                "DURATION": ("NEGATIVE", "PREFER_SHORTER"),
                "COOKING_METHOD": ("POSITIVE", "PREFER_ONE_PAN"),
            },
        )

    def test_unicode_offsets_index_original_field(self):
        memo, reason = "🧑‍🍳 정말 재미있었어요. 재미있었어요!", "💰재료비가 비쌌어요."
        sources = {"memo": memo, "notDoneReason": reason}
        result = by_category(memo, reason)
        for aspect in result.values():
            for item in aspect["evidence"]:
                self.assertEqual(
                    sources[item["source"]][item["start"] : item["end"]], item["text"]
                )
        spans = result["ENJOYMENT"]["evidence"]
        self.assertEqual(len(spans), 2)
        self.assertGreater(spans[1]["start"], spans[0]["end"])
        self.assertEqual(result["COST"]["evidence"][0]["source"], "notDoneReason")

    def test_repeated_and_conflicting_cost_observations(self):
        memo = "비용은 괜찮았어요. 재료비가 비쌌어요."
        cost = by_category(memo)["COST"]
        self.assertEqual((cost["polarity"], cost["signal"]), ("MIXED", None))
        self.assertEqual(len(cost["evidence"]), 2)

    def test_logistical_problem_does_not_imply_skill_or_tool_count(self):
        memo = "재료를 구하기 너무 어려웠어요. 도구가 너무 비쌌어요."
        self.assertEqual(labels(memo), {"COST": ("NEGATIVE", "PREFER_LOWER_COST")})

    def test_enjoyment_and_difficulty_can_have_opposite_polarity(self):
        self.assertEqual(
            labels("어려웠지만 재밌었어요."),
            {
                "DIFFICULTY": ("NEGATIVE", None),
                "ENJOYMENT": ("POSITIVE", None),
            },
        )
        self.assertEqual(
            labels("어려워서 좋았어요."), {"DIFFICULTY": ("POSITIVE", None)}
        )

    def test_real_experience_after_reported_or_hypothetical_difficulty(self):
        for memo in (
            "어렵다고 들었어요.",
            "처음엔 어려웠을 것 같아요.",
            "쉬운 활동을 하고 싶어요.",
            "다음에는 더 어려운 걸 하고 싶어요.",
            "‘어려웠어요’라고 친구가 말했어요.",
        ):
            with self.subTest(memo=memo):
                self.assertEqual(by_category(memo), {})
        result = by_category("어렵다고 들었지만 나한테는 쉬웠어요.")
        self.assertEqual(result["DIFFICULTY"]["evidence"][0]["text"], "쉬웠어요")

    def test_scope_of_negation(self):
        self.assertEqual(
            labels(
                "재미있지는 않았어요. 시간이 오래 걸리지 않았어요. 팬 하나로 하고 싶지 않아요."
            ),
            {
                "ENJOYMENT": ("NEGATIVE", None),
                "DURATION": ("NEUTRAL", None),
                "COOKING_METHOD": ("NEGATIVE", None),
            },
        )
        self.assertEqual(
            labels("재료비가 비싸지 않았어요. 도구는 많지 않았어요."),
            {
                "COST": ("NEUTRAL", None),
                "TOOLS": ("NEUTRAL", None),
            },
        )

    def test_cleanup_and_preference_have_distinct_targets(self):
        self.assertEqual(
            labels("설거지가 많았어요. 다음엔 원팬 요리를 원해요."),
            {
                "TOOLS": ("NEGATIVE", "PREFER_FEWER_TOOLS"),
                "COOKING_METHOD": ("POSITIVE", "PREFER_ONE_PAN"),
            },
        )

    def test_countervailing_method_requests_do_not_trigger_a_signal(self):
        self.assertEqual(
            labels("원팬 요리는 싫었어요. 다음에는 원팬 요리를 하고 싶어요."),
            {"COOKING_METHOD": ("MIXED", None)},
        )

    def test_actual_time_burden_and_next_request_point_same_way(self):
        self.assertEqual(
            labels("오래 걸렸어요. 짧은 활동을 하고 싶어요."),
            {
                "DURATION": ("NEGATIVE", "PREFER_SHORTER"),
            },
        )
        self.assertEqual(
            labels("짧은 활동을 하고 싶어요."),
            {
                "DURATION": ("POSITIVE", "PREFER_SHORTER"),
            },
        )

    def test_neutral_value_and_unknown_record_are_distinct(self):
        self.assertEqual(labels("20분 걸렸어요."), {"DURATION": ("NEUTRAL", None)})
        for note in ("", "활동을 기록했습니다.", "기타와 악보를 꺼냈어요."):
            self.assertEqual(labels(note), {})

    def test_explicit_favorable_long_session_or_many_tools(self):
        for text, category in (
            ("시간이 오래 걸려서 좋았어요.", "DURATION"),
            ("시간이 길어서 오히려 좋았어요.", "DURATION"),
            ("설거지가 많아서 좋았어요.", "TOOLS"),
            ("너무 어려워서 좋았어요.", "DIFFICULTY"),
        ):
            with self.subTest(text=text):
                result = by_category(text)
                self.assertEqual(
                    (result[category]["polarity"], result[category]["signal"]),
                    ("POSITIVE", None),
                )
                self.assertTrue(
                    result[category]["evidence"][0]["text"].endswith("좋았어요")
                )

    def test_ambiguous_material_burden_is_not_monetary(self):
        for text in (
            "준비물이 많이 들었어요.",
            "도구는 괜찮았어요.",
            "재료가 부담스러웠어요.",
        ):
            self.assertNotIn("COST", by_category(text))
        self.assertEqual(
            labels("재료비가 많이 들었어요."),
            {"COST": ("NEGATIVE", "PREFER_LOWER_COST")},
        )

    def test_reason_can_supply_time_burden(self):
        aspect = by_category(reason="시간이 부족해서 못 했어요.")["DURATION"]
        self.assertEqual(aspect["signal"], "PREFER_SHORTER")
        self.assertEqual(
            aspect["evidence"],
            [
                {
                    "source": "notDoneReason",
                    "text": "시간이 부족해서",
                    "start": 0,
                    "end": 8,
                }
            ],
        )
