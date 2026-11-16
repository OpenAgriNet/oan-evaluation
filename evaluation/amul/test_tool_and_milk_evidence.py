"""Current tool outcomes and milk-output compatibility, with no external calls."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from evaluation.amul.checks import combine_source_alignment, milk_total
from evaluation.amul.judge import Judge, _fmt_history
from evaluation.amul.langfuse_io import build_turn
from evaluation.amul.reliability import source_catalog
from evaluation.amul.run_amul_eval import main
from evaluation.amul.test_final_metrics import turn
from evaluation.amul.test_langfuse_io import trace


def milk(rows='', summary=None, dates=None, status=None):
    output = ''
    if summary is not None:
        litres, amount = summary
        output += ('### Summary\n| Period | Days with milk | Total milk (L) | Total amount (Rs) | Average FAT | Average SNF |\n'
                   '| --- | --- | --- | --- | --- | --- |\n'
                   f'| 01-10-2026 to 02-10-2026 | 2 | {litres} | {amount} | 4.00 | 8.50 |\n\n')
    output += ('### Milk Collection\n| Date | Shift | Qty (L) | FAT | SNF | Amount |\n'
               '| --- | --- | --- | --- | --- | --- |\n' + rows + '\n\n'
               '### Deductions\n| Date | Account | Amount |\n| --- | --- | --- |\n| 2026-10-01 | Feed | 9999 |')
    call = {'name': 'get_farmer_milk_collection_details', 'output': output}
    if dates:
        call['input'] = {'fromdate': dates[0], 'todate': dates[1]}
    if status:
        call['metadata'] = {'amul.tool_status': status}
    return call


def total_turn(calls, litres=12, amount=None):
    if amount is not None:
        return turn(question_en='Total earnings?', answer_en=f'Total ₹{amount}.', answer_gu=f'કુલ {amount} રૂપિયા.', tool_calls=calls)
    return turn(question_en='Total litres?', answer_en=f'Total {litres} litres.', answer_gu=f'કુલ {litres} લિટર.', tool_calls=calls)


class ToolEvidenceTests(unittest.TestCase):
    def test_current_span_tools_preserve_results_and_status(self):
        for status in ('success', 'empty', 'no_result', 'unavailable', 'error', 'future_status'):
            with self.subTest(status=status):
                raw = trace(observations=[{'id': 'tool', 'name': 'search_documents', 'type': 'SPAN',
                    'metadata': {'amul.tool_status': status}, 'input': {'query': 'heat'}, 'output': 'Retrieved veterinary passage'}])
                result = build_turn(raw, {})
                self.assertEqual(result['tool_calls'][0]['output'], 'Retrieved veterinary passage')
                self.assertEqual(result['tool_calls'][0]['metadata']['amul.tool_status'], status)
                self.assertEqual(result['api_calls'], [])

    def test_current_span_tool_supplies_backend_ancestry_without_double_counting(self):
        raw = trace(observations=[
            {'id': 'tool', 'name': 'fetch_tool', 'type': 'SPAN', 'metadata': {'amul.tool_status': 'error'}, 'output': {'ok': False}},
            {'name': 'provider_request', 'type': 'SPAN', 'parentObservationId': 'tool', 'output': {'ok': False}},
        ])
        result = build_turn(raw, {})
        self.assertEqual([c['name'] for c in result['tool_calls']], ['fetch_tool'])
        self.assertEqual([c['name'] for c in result['api_calls']], ['provider_request'])

    def test_unmarked_wrappers_and_non_span_metadata_do_not_become_tools(self):
        observations = [
            {'name': 'search_documents', 'type': 'SPAN', 'output': 'Name alone is insufficient'},
            {'name': 'running 1 tool', 'type': 'SPAN', 'metadata': {'amul.cache': 'hit'}},
            {'name': 'agent', 'type': 'AGENT', 'metadata': {'amul.tool_status': 'success'}},
            {'name': 'model', 'type': 'GENERATION', 'metadata': {'amul.tool_status': 'success'}},
        ]
        for status in (None, '', '  ', False):
            observations.append({'name': 'plain_span', 'type': 'SPAN', 'metadata': {'amul.tool_status': status}})
        self.assertEqual(build_turn(trace(observations=observations), {})['tool_calls'], [])

    def test_reported_outcomes_and_error_reason_are_retained_without_otel_noise(self):
        for status in ('success', 'empty', 'no_result', 'unavailable', 'error'):
            with self.subTest(status=status):
                tool = {'id': 'tool', 'parentObservationId': 'root', 'type': 'TOOL', 'name': 'search_documents',
                        'input': {}, 'output': 'Provider result', 'level': 'ERROR' if status in ('error', 'unavailable') else 'DEFAULT',
                        'statusMessage': 'Provider timed out', 'metadata': {'amul.tool_status': status, 'amul.cache': 'miss', 'resourceAttributes': {'noise': True}}}
                call = build_turn(trace(observations=[tool]), {})['tool_calls'][0]
                self.assertEqual(call['metadata'], {'amul.tool_status': status, 'amul.cache': 'miss'})
                self.assertEqual(call['status_message'], 'Provider timed out')
                self.assertEqual(call['observation_id'], 'tool')
                self.assertEqual(call['parent_observation_id'], 'root')

    def test_renamed_booking_operation_is_backend_evidence_not_second_tool(self):
        raw = trace(observations=[
            {'id': 'tool', 'name': 'create_ai_call', 'type': 'TOOL'},
            {'name': 'ai_call_booking', 'type': 'SPAN', 'parentObservationId': 'tool',
             'output': {'ok': False, 'status_code': 503}, 'level': 'ERROR', 'statusMessage': 'Confirm unavailable'},
        ])
        result = build_turn(raw, {})
        self.assertEqual([c['name'] for c in result['tool_calls']], ['create_ai_call'])
        self.assertEqual([c['name'] for c in result['api_calls']], ['ai_call_booking'])
        self.assertEqual(result['api_calls'][0]['output'], {'ok': False, 'status_code': 503})

    def test_unknown_operation_needs_explicit_api_result_and_tool_ancestry(self):
        raw = trace(observations=[
            {'id': 'tool', 'name': 'search_documents', 'type': 'TOOL'},
            {'id': 'wrapper', 'name': 'wrapper', 'type': 'SPAN', 'parentObservationId': 'tool'},
            {'name': 'provider_request', 'type': 'SPAN', 'parentObservationId': 'wrapper', 'output': {'ok': True}},
            {'name': 'agent', 'type': 'AGENT', 'output': {'ok': True}},
            {'name': 'post_translation', 'type': 'SPAN', 'output': {'ok': True}},
            {'name': 'background_job', 'type': 'SPAN', 'output': {'ok': True}},
            {'name': 'model_api', 'type': 'GENERATION', 'output': {'ok': True}},
            {'name': 'plain_span', 'type': 'SPAN', 'parentObservationId': 'wrapper', 'output': 'Plain span'},
        ])
        self.assertEqual([c['name'] for c in build_turn(raw, {})['api_calls']], ['provider_request'])

    def test_malformed_ancestor_cycle_does_not_hang_or_invent_an_api_call(self):
        raw = trace(observations=[{'id': 'cycle', 'name': 'unknown', 'type': 'SPAN', 'parentObservationId': 'cycle', 'output': {'ok': False}}])
        self.assertEqual(build_turn(raw, {})['api_calls'], [])

    def test_old_api_naming_is_retained(self):
        raw = trace(observations=[{'name': name, 'type': 'SPAN'} for name in ('milk_api', 'fetch_profile')])
        self.assertEqual([c['name'] for c in build_turn(raw, {})['api_calls']], ['milk_api', 'fetch_profile'])

    def test_tool_outcomes_and_backend_failures_reach_factual_and_workflow_prompts(self):
        tool = {'name': 'get_vistaar_scheme_info', 'input': {}, 'output': 'Temporarily unavailable',
                'metadata': {'amul.tool_status': 'unavailable'}, 'level': 'ERROR', 'status_message': 'Gateway timeout'}
        api = {'name': 'scheme_provider', 'input': {}, 'output': {'ok': False}, 'status_message': 'Provider offline'}
        t = turn(tool_calls=[tool], api_calls=[api])
        judge = object.__new__(Judge)
        judge.farmer_context = '{}'
        for group in ('source_alignment', 'task_success', 'content_gap', 'tool_sequencing', 'completeness'):
            with self.subTest(group=group):
                prompt = judge._render(group, t, [t])
                self.assertIn('Gateway timeout', prompt)
                self.assertIn('unavailable', prompt)
                self.assertIn('Provider offline', prompt)
                self.assertIn('does not confirm a booking', prompt)

    def test_history_and_source_catalog_retain_actual_failure_evidence_only(self):
        prior = turn(tool_calls=[{'name': 'search_documents', 'input': {}, 'output': 'No result',
                                  'metadata': {'amul.tool_status': 'no_result'}, 'status_message': 'No matching passage'}])
        current = turn(turn_index=2)
        self.assertIn('No matching passage', _fmt_history(current, [prior, current]))
        catalog = source_catalog(current, [prior, current], '{}')
        texts = [entry['text'] for entry in catalog.values()]
        self.assertIn('No matching passage', texts)
        self.assertIn('no_result', texts)


class MilkEvidenceTests(unittest.TestCase):
    def test_outputs_from_the_pinned_current_app_formatter(self):
        fixture = json.loads(Path(__file__).with_name('fixtures').joinpath('current_milk_formatter_85412ae.json').read_text())
        for case in fixture['cases']:
            call = {'name': 'get_farmer_milk_collection_details', 'input': case['input'], 'output': case['output']}
            for column, key in [('litres', 'expected_litres'), ('amount', 'expected_amount')]:
                with self.subTest(case=case['name'], column=column):
                    kwargs = {column: case[key]}
                    t = total_turn([call], **kwargs)
                    self.assertEqual(milk_total(t, [t])[0], 1)
                    t = total_turn([call], **{column: str(int(case[key]) + 1)})
                    self.assertEqual(milk_total(t, [t])[0], 0)

    def test_missing_fat_snf_do_not_drop_valid_quantity_or_amount(self):
        call = milk('| 2026-10-01 | morning | 12 | - | - | 480 |')
        for t in [total_turn([call]), total_turn([call], amount=480)]:
            self.assertEqual(milk_total(t, [t])[0], 1)

    def test_distinct_accounts_on_the_same_date_and_shift_are_both_counted(self):
        call = milk('| 2026-10-01 | morning | 5 | 4 | 8.5 | 200 |\n| 2026-10-01 | morning | 7 | 4 | 8.5 | 280 |')
        t = total_turn([call])
        self.assertEqual(milk_total(t, [t])[0], 1)
        t = total_turn([call], litres=7)
        self.assertEqual(milk_total(t, [t])[0], 0)

    def test_identical_account_rows_retain_multiplicity_within_one_lookup(self):
        row = '| 2026-10-01 | morning | 6 | 4 | 8.5 | 240 |'
        call = milk(row + '\n' + row)
        t = total_turn([call])
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_repeated_lookup_does_not_double_the_same_multi_account_rows(self):
        row = '| 2026-10-01 | morning | 6 | 4 | 8.5 | 240 |'
        call = milk(row + '\n' + row)
        t = total_turn([call, call])
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_summary_is_used_instead_of_readding_display_rounded_rows(self):
        # Three raw 1.234 L records render as 1.23 each; the app's sum is 3.70.
        rows = '\n'.join(f'| 2026-10-0{i} | morning | 1.23 | - | - | 10 |' for i in (1, 2, 3))
        t = total_turn([milk(rows, summary=('3.70', '30'))], litres='3.70')
        result = milk_total(t, [t])
        self.assertEqual(result[0], 1)
        self.assertIn('tool summary', result[1])

    def test_current_summary_table_in_answer_is_checked_without_inline_units(self):
        call = milk(summary=('12.00', '480.00'))
        t = total_turn([call])
        t['answer_en'] = call['output'].split('### Milk Collection')[0]
        t['answer_gu'] = '| કુલ દૂધ (લિટર) | કુલ રકમ (રૂપિયા) |\n| --- | --- |\n| ૧૨.૦૦ | ૪૮૦.૦૦ |'
        self.assertEqual(milk_total(t, [t])[0], 1)
        t['answer_gu'] = t['answer_gu'].replace('૧૨.૦૦', '૧૩.૦૦')
        self.assertEqual(milk_total(t, [t])[0], 0)

    def test_unrecognized_numeric_table_requires_review_instead_of_false_failure(self):
        t = total_turn([milk(summary=('12', '480'))])
        t['answer_gu'] = '| અજાણ્યું મથાળું |\n| --- |\n| ૧૨ |'
        self.assert_review_required(t, 'unrecognized numeric table')

    def test_multiple_scalar_amounts_cannot_silently_select_the_first_as_total(self):
        t = total_turn([milk(summary=('12', '480'))])
        t['answer_en'] = '5 litres in the morning, 7 litres in the evening, total 12 litres.'
        self.assert_review_required(t, 'multiple candidate totals')

    def test_disjoint_summary_only_lookups_can_be_added(self):
        calls = [milk(summary=('5', '200'), dates=('2026-10-01', '2026-10-01')),
                 milk(summary=('7', '280'), dates=('2026-10-02', '2026-10-02'))]
        t = total_turn(calls)
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_overlapping_summaries_without_detail_rows_require_review(self):
        calls = [milk(summary=('12', '480')), milk(summary=('12', '480'))]
        t = total_turn(calls)
        self.assert_review_required(t, 'summaries alone')

    def test_conflicting_overlap_cannot_be_assumed_one_or_two_accounts(self):
        calls = [milk('| 2026-10-01 | morning | 5 | - | - | 200 |'),
                 milk('| 2026-10-01 | morning | 7 | - | - | 280 |')]
        t = total_turn(calls)
        self.assert_review_required(t, 'account identity')

    def test_missing_quantity_blocks_litres_but_known_amount_can_still_be_checked(self):
        call = milk('| 2026-10-01 | morning | - | - | - | 480 |')
        self.assert_review_required(total_turn([call]), 'quantity/amount')
        t = total_turn([call], amount=480)
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_summary_cannot_turn_an_unknown_quantity_into_a_verified_zero(self):
        call = milk('| 2026-10-01 | morning | - | - | - | 480 |', summary=('0', '480'))
        self.assert_review_required(total_turn([call], litres=0), 'quantity/amount')

    def test_summary_detail_conflict_requires_review_instead_of_trusting_either(self):
        call = milk('| 2026-10-01 | morning | 7 | - | - | 280 |', summary=('12', '480'))
        self.assert_review_required(total_turn([call]), 'conflicts with its detail rows')

    def test_malformed_detail_row_is_not_silently_skipped(self):
        call = milk('| 2026-10-01 | morning | 12 | - | - | 480 |\n| bad-date | morning | 5 | - | - | 200 |')
        self.assert_review_required(total_turn([call]), 'unparsed milk detail')

    def test_deductions_are_not_counted_as_milk_earnings(self):
        t = total_turn([milk('| 2026-10-01 | morning | 12 | - | - | 480 |')], amount=480)
        self.assertEqual(milk_total(t, [t])[0], 1)

    def test_failed_lookup_does_not_make_an_honest_inability_a_numeric_error(self):
        call = {'name': 'get_farmer_milk_collection_details', 'output': 'Unavailable', 'metadata': {'amul.tool_status': 'unavailable'}}
        t = total_turn([call])
        self.assert_review_required(t, 'failed lookup')
        t['answer_en'], t['answer_gu'] = 'Unable to fetch records.', 'વિગતો ઉપલબ્ધ નથી.'
        self.assertIsNone(milk_total(t, [t]))

    def test_future_retrieval_cannot_supply_the_current_total(self):
        t = total_turn([], litres=5)
        future = total_turn([milk('| 2026-10-01 | morning | 5 | - | - | 200 |')], litres=5)
        future['turn_index'] = 2
        self.assertIsNone(milk_total(t, [t, future]))

    def test_unresolved_total_blocks_a_source_five_before_any_model_request(self):
        t = total_turn([milk('| 2026-10-01 | morning | - | - | - | 480 |')])
        judge = object.__new__(Judge)
        judge.farmer_context = '{}'
        with patch.object(judge, '_complete') as complete:
            with self.assertRaisesRegex(RuntimeError, 'REVIEW_REQUIRED'):
                judge.run_reliable('source_alignment', t, [t])
            complete.assert_not_called()

    def test_unresolved_source_check_is_persisted_as_an_error_not_a_null_pass(self):
        t = total_turn([milk('| 2026-10-01 | morning | - | - | - | 480 |')])
        t.update(trace_id='offline-trace', session_id='offline-session', question_id='offline-question')
        with TemporaryDirectory() as folder:
            cache, results = Path(folder) / 'turns.json', Path(folder) / 'results.json'
            cache.write_text(json.dumps([t]))
            args = ['eval', '--run', 'offline-fixture', '--from-cache', str(cache), '--checks', 'none',
                    '--judges', 'source_alignment', '--results-json', str(results)]
            judge = object.__new__(Judge)
            judge.farmer_context, judge.model = '{}', 'offline-never-called'
            with patch('sys.argv', args), patch('evaluation.amul.run_amul_eval.Judge', return_value=judge), patch.object(judge, '_complete') as complete, patch('builtins.print'):
                with self.assertRaises(SystemExit):
                    main()
                complete.assert_not_called()
            report = json.loads(results.read_text())
            row = report['results'][0]
            self.assertFalse(report['posted'])
            self.assertIsNone(row['value'])
            self.assertIn('REVIEW_REQUIRED', row['error'])
            self.assertEqual(report['summary']['source_alignment']['errors'], 1)
            self.assertEqual(report['summary']['source_alignment']['null'], 0)

    def assert_review_required(self, t, reason):
        result = milk_total(t, [t])
        self.assertIsNone(result[0])
        self.assertIn('REVIEW_REQUIRED', result[1])
        self.assertIn(reason, result[1])
        with self.assertRaisesRegex(ValueError, 'REVIEW_REQUIRED'):
            combine_source_alignment((5, 'Judge proposed a pass'), {'milk_total': result})


if __name__ == '__main__':
    unittest.main()
