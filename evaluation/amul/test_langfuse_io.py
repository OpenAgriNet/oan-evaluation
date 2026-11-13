"""Offline regressions for legacy and turn.v1 trace normalization (#510)."""
import unittest
from unittest.mock import Mock

from evaluation.amul.langfuse_io import build_turn, load_run


def trace(**overrides):
    data = {
        "id": "trace-1", "sessionId": "session-1", "timestamp": "2026-11-02T06:00:00Z",
        "name": "turn", "input": "મારી ગાય ખાતી નથી.", "output": "પશુચિકિત્સકની સલાહ લો.",
        "latency": 4.007, "metadata": {"amul.schema_version": "turn.v1"},
        "observations": [
            {"id": "root", "name": "turn", "type": "SPAN", "parentObservationId": None,
             "startTime": "2026-11-02T06:00:00.000Z", "endTime": "2026-11-02T06:00:02.277Z"},
            {"name": "pre_translation", "type": "GENERATION", "output": "My cow is not eating."},
            {"name": "moderation", "type": "GENERATION", "output": {"category": "in_scope", "action": "allow"}},
            {"name": "stream_translation", "type": "GENERATION", "input": {"text": "Consult a veterinarian."}},
            {"name": "suggestions", "type": "GENERATION", "parentObservationId": "root",
             "startTime": "2026-11-02T06:00:02.279Z", "endTime": "2026-11-02T06:00:04.007Z"},
        ],
    }
    data.update(overrides)
    return data


class TraceReaderTests(unittest.TestCase):
    def test_new_trace_retains_questions_moderation_and_reply_not_suggestions_time(self):
        raw = trace()
        item = {"input": {"question_id": "health-1"}, "metadata": {"category": "Health_call", "tool_hint": "create_health_call"}}
        result = build_turn(raw, item)
        self.assertEqual(result["question_gu"], raw["input"])
        self.assertEqual(result["question_en"], "My cow is not eating.")
        self.assertEqual(result["moderation"], "in_scope")
        self.assertEqual(result["latency_s"], 2.277)
        self.assertEqual(result["answer_en"], "Consult a veterinarian.")
        self.assertEqual(result["answer_gu"], raw["output"])
        self.assertEqual(result["question_id"], "health-1")
        self.assertEqual(result["tool_hint"], "create_health_call")

    def test_legacy_trace_preserves_existing_values_and_call_payloads(self):
        tool = {"name": "search_documents", "type": "TOOL", "input": {"query": "cow"}, "output": "Consult a vet."}
        api = {"name": "milk_api", "type": "SPAN", "input": {}, "output": {"ok": True}}
        raw = trace(name="chat.translation", input={"query": "જૂનો પ્રશ્ન"}, metadata={}, observations=[
            {"name": "query_pretranslation", "output": "Old question"},
            {"name": "Moderation", "output": {"category": "out_of_scope"}},
            {"name": "stream_translation", "input": {"text": "First. "}, "startTime": "2026-11-02T06:00:01Z"},
            {"name": "stream_translation", "input": {"text": "Second."}, "startTime": "2026-11-02T06:00:02Z"}, tool, api,
        ])
        result = build_turn(raw, {})
        self.assertEqual(result["question_gu"], "જૂનો પ્રશ્ન")
        self.assertEqual(result["question_en"], "Old question")
        self.assertEqual(result["moderation"], "out_of_scope")
        self.assertEqual(result["latency_s"], 4.007)
        self.assertEqual(result["answer_en"], "First. Second.")
        for key, call in [("tool_calls", tool), ("api_calls", api)]:
            self.assertEqual(result[key], [{k: call[k] for k in ("name", "input", "output")}])

    def test_current_stage_names_take_precedence_over_old_aliases(self):
        raw = trace()
        raw["observations"][:0] = [
            {"name": "query_pretranslation", "output": "Old alias"},
            {"name": "Moderation", "output": {"category": "wrong alias"}},
        ]
        result = build_turn(raw, {})
        self.assertEqual(result["question_en"], "My cow is not eating.")
        self.assertEqual(result["moderation"], "in_scope")

    def test_direct_english_uses_language_metadata_when_translation_is_skipped(self):
        raw = trace(input="My cow is not eating.", output="Consult a veterinarian.", observations=[],
                    metadata={"amul.schema_version": "turn.v1", "amul.source_lang": "en", "amul.target_lang": "en"})
        result = build_turn(raw, {})
        self.assertEqual(result["question_en"], raw["input"])
        self.assertEqual(result["answer_en"], raw["output"])

    def test_untranslated_non_english_is_not_mislabeled_as_english(self):
        raw = trace(observations=[], metadata={"amul.schema_version": "turn.v1", "amul.source_lang": "gu", "amul.target_lang": "gu"})
        result = build_turn(raw, {})
        self.assertIsNone(result["question_en"])
        self.assertIsNone(result["answer_en"])

    def test_root_is_selected_by_type_and_parent_not_observation_order(self):
        raw = trace()
        raw["observations"][:0] = [
            {"name": "turn", "type": "SPAN", "parentObservationId": "root", "latency": 99},
            {"name": "turn", "type": "GENERATION", "latency": 88},
        ]
        self.assertEqual(build_turn(raw, {})["latency_s"], 2.277)

    def test_timestamp_duration_is_authoritative_and_handles_timezone_offsets(self):
        raw = trace()
        raw["observations"][0].update(startTime="2026-11-02T11:30:00+05:30", endTime="2026-11-02T06:00:02.277+00:00", latency=99)
        self.assertEqual(build_turn(raw, {})["latency_s"], 2.277)

    def test_root_observation_latency_is_used_if_timestamps_are_unavailable(self):
        for duration in (0, 2.277):
            with self.subTest(duration=duration):
                raw = trace(observations=[{"name": "turn", "type": "SPAN", "latency": duration}])
                self.assertEqual(build_turn(raw, {})["latency_s"], duration)

    def test_incomplete_new_trace_never_falls_back_to_whole_trace_latency(self):
        for metadata, name in [({"amul.schema_version": "turn.v1"}, "other"), ({}, "turn")]:
            with self.subTest(metadata=metadata, name=name):
                raw = trace(metadata=metadata, name=name, observations=[])
                self.assertIsNone(build_turn(raw, {})["latency_s"])

    def test_nested_turn_cannot_supply_reply_latency_when_root_is_missing(self):
        raw = trace(observations=[{"name": "turn", "type": "SPAN", "parentObservationId": "another", "latency": 8}])
        self.assertIsNone(build_turn(raw, {})["latency_s"])

    def test_invalid_root_timing_stays_missing_instead_of_using_suggestions_time(self):
        roots = [
            {}, {"latency": -1}, {"latency": True}, {"latency": float("nan")},
            {"latency": float("inf")}, {"latency": "2.277"},
            {"startTime": "bad", "endTime": None},
            {"startTime": "2026-11-02T06:00:02Z", "endTime": "2026-11-02T06:00:01Z"},
            {"startTime": "2026-11-02T06:00:00", "endTime": "2026-11-02T06:00:01Z"},
        ]
        for fields in roots:
            with self.subTest(fields=fields):
                raw = trace(observations=[{"name": "turn", "type": "SPAN", **fields}])
                self.assertIsNone(build_turn(raw, {})["latency_s"])

    def test_missing_fields_are_not_invented_from_dataset_prompts(self):
        result = build_turn(trace(input=None, output=None, observations=[]), {
            "input": {"question_gujarati": "Dataset question", "question_english": "Dataset English"},
        })
        self.assertIsNone(result["question_gu"])
        self.assertIsNone(result["question_en"])
        self.assertIsNone(result["moderation"])

    def test_malformed_stage_outputs_do_not_hide_valid_legacy_aliases(self):
        raw = trace(observations=[
            {"name": "pre_translation", "output": {}},
            {"name": "query_pretranslation", "output": "Usable English"},
            {"name": "moderation", "output": "wrong type"},
            {"name": "Moderation", "output": {"category": "in_scope"}},
        ])
        result = build_turn(raw, {})
        self.assertEqual(result["question_en"], "Usable English")
        self.assertEqual(result["moderation"], "in_scope")

    def test_load_run_preserves_session_order_and_recovered_history(self):
        first = trace()
        second = trace(id="trace-2", timestamp="2026-11-02T06:01:00Z", input="હવે શું કરું?")
        other = trace(id="trace-3", sessionId="session-2")
        lf = Mock()
        lf.dataset_items.return_value = {}
        lf.run_items.return_value = [{"traceId": t["id"], "datasetItemId": "missing"} for t in [second, other, first]]
        lf.trace.side_effect = {t["id"]: t for t in [first, second, other]}.__getitem__
        results = load_run(lf, "dataset", "run")
        self.assertEqual([t["trace_id"] for t in results], ["trace-1", "trace-2", "trace-3"])
        self.assertEqual(results[1]["turn_index"], 2)
        self.assertEqual(results[1]["history"][0]["question_gu"], first["input"])
        self.assertEqual(results[2]["history"], [])


if __name__ == "__main__":
    unittest.main()
