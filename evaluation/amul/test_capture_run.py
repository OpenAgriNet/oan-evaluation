"""Capture planning, sequential requests and restart reconciliation without network."""
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, Mock, patch

from evaluation.amul.capture_run import (
    CaptureError, RUNTIME_CHECKS, build_plan, capture, check_runtime, digest,
    journal_lock, main, send_chat, verify_trace, write_json, _reconcile_links,
)

RELEASE = 'a' * 40
URL = 'http://127.0.0.1:18097/api/chat/'


def item(sid, number, **metadata):
    return {'id': f'item-{sid}-{number}', 'input': {'session_id': sid, 'question_id': f'{sid}-{number}',
            'question_gujarati': f'પ્રશ્ન {number}', 'question_english': f'Question {number}'},
            'metadata': metadata, 'expectedOutput': None, 'status': 'ACTIVE'}


def plan_for(items):
    return build_plan(items, dataset='amul-seed-questions', run='offline-test', chat_url=URL,
                      langfuse_host='https://langfuse.dev.amulai.in', release=RELEASE, capture_id='b' * 32)


def runtime(plan):
    return {'schema_version': 'amul.capture-runtime-check.v1', 'chat_url': plan['chat_url'],
            'langfuse_host': plan['langfuse_host'], 'release': RELEASE,
            'verified_at_utc': datetime.now(timezone.utc).isoformat(),
            'capture_allowed': True, 'checks': dict.fromkeys(RUNTIME_CHECKS, True)}


class FakeLangfuse:
    host = 'https://langfuse.dev.amulai.in'

    def __init__(self, items):
        self.items, self.links, self.writes = items, [], []
        self.exists = False

    def dataset_items(self, dataset):
        return {row['id']: row for row in self.items}

    def _request(self, method, path, body):
        assert method == 'POST' and path == '/api/public/dataset-run-items'
        self.writes.append(body)
        self.links.append({'datasetItemId': body['datasetItemId'], 'traceId': body['traceId']})
        self.exists = True
        return {'id': str(len(self.links))}


class CaptureTests(unittest.TestCase):
    def test_numeric_order_and_fresh_session_namespace_are_deterministic(self):
        rows = [item('milk', n) for n in range(12, 0, -1)]
        plan = plan_for(rows)
        session = plan['sessions'][0]
        self.assertEqual([t['turn_index'] for t in session['items']], list(range(1, 13)))
        self.assertNotEqual(session['session_id'], 'milk')
        self.assertEqual(plan, plan_for(list(reversed(rows))))

    def test_ai_exclusion_applies_to_the_whole_session_but_health_hint_is_preserved(self):
        plan = plan_for([item('ai-home', 1), item('ai-home', 2),
                         item('hidden-booking', 1, tool_hint='create_ai_call'), item('hidden-booking', 2),
                         item('health-book', 1, tool_hint='create_health_call', category='Health_call')])
        self.assertEqual(plan['counts']['excluded_turns'], 4)
        self.assertEqual([s['seed_session_id'] for s in plan['sessions']], ['health-book'])
        self.assertEqual(plan['sessions'][0]['items'][0]['tool_hint'], 'create_health_call')

    def test_duplicate_ids_gaps_and_missing_questions_fail_before_requests(self):
        bad_question = item('milk', 1)
        bad_question['input']['question_gujarati'] = ''
        for rows in ([item('milk', 1), item('milk', 1)], [item('milk', 2)], [bad_question]):
            with self.subTest(rows=rows), self.assertRaises(CaptureError):
                plan_for(rows)

    def test_urls_cannot_embed_credentials_or_rely_on_redirects(self):
        for url in ('https://user:secret@example.com/api/chat/', 'https://example.com/api/chat', URL + '?token=x'):
            with self.subTest(url=url), self.assertRaises(CaptureError):
                build_plan([item('milk', 1)], dataset='seed', run='test', chat_url=url,
                           langfuse_host='https://langfuse.dev.amulai.in', release=RELEASE)

    def test_every_runtime_guard_must_be_explicitly_verified(self):
        plan = plan_for([item('milk', 1)])
        for key in RUNTIME_CHECKS:
            report = runtime(plan)
            report['checks'][key] = None
            with self.subTest(key=key), self.assertRaisesRegex(CaptureError, key):
                check_runtime(plan, report)

    def test_stale_runtime_wrong_build_and_altered_manifest_are_rejected(self):
        plan = plan_for([item('milk', 1)])
        report = runtime(plan)
        report['verified_at_utc'] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
        with self.assertRaisesRegex(CaptureError, 'last hour'):
            check_runtime(plan, report)
        report = runtime(plan)
        report['release'] = 'c' * 40
        with self.assertRaisesRegex(CaptureError, 'release'):
            check_runtime(plan, report)
        plan['sessions'][0]['items'][0]['question'] = 'changed'
        with self.assertRaisesRegex(CaptureError, 'digest'):
            check_runtime(plan, runtime(plan))

    def test_capture_sends_one_turn_then_verifies_and_links_before_next_turn(self):
        rows = [item('z', 1), item('a', 2), item('a', 1)]
        plan, lf, events = plan_for(rows), FakeLangfuse(rows), []
        def sender(p, s, t, headers):
            events.append(('send', t['question_id'], s['session_id']))
            return {'trace_id': digest(t)[:32], 'answer': 'જવાબ'}
        def waiter(client, p, s, t, response):
            events.append(('verify', t['question_id']))
            return {'reply_latency_s': 2}
        original = lf._request
        def link(method, path, body):
            events.append(('link', body['datasetItemId']))
            return original(method, path, body)
        lf._request = link
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', side_effect=lambda *_: {'datasetRunItems': lf.links} if lf.exists else None), patch('evaluation.amul.capture_run.chat_headers', return_value={'X-API-Key': 'not-logged'}):
            journal_path = Path(folder) / 'capture.json'
            result = capture(plan, runtime(plan), lf, journal_path, sender=sender, waiter=waiter)
            self.assertTrue(result['complete'])
            self.assertEqual([e[0] for e in events], ['send', 'verify', 'link'] * 3)
            sent = [e for e in events if e[0] == 'send']
            self.assertEqual([e[1] for e in sent], ['a-1', 'a-2', 'z-1'])
            self.assertEqual(sent[0][2], sent[1][2])
            self.assertNotEqual(sent[1][2], sent[2][2])
            self.assertNotIn('not-logged', journal_path.read_text())
            self.assertEqual(journal_path.stat().st_mode & 0o777, 0o600)
            self.assertTrue(all('scores' not in body for body in lf.writes))

    def test_changed_dataset_blocks_capture_before_any_app_call(self):
        rows = [item('milk', 1)]
        plan, sender = plan_for(rows), Mock()
        changed = item('milk', 1)
        changed['input']['question_gujarati'] = 'changed'
        with TemporaryDirectory() as folder, self.assertRaisesRegex(CaptureError, 'Dataset changed'):
            capture(plan, runtime(plan), FakeLangfuse([changed]), Path(folder) / 'journal.json', sender=sender)
        sender.assert_not_called()

    def test_uncertain_chat_failure_is_never_automatically_replayed(self):
        rows, sender = [item('milk', 1)], Mock(side_effect=CaptureError('transport failed'))
        plan, lf = plan_for(rows), FakeLangfuse(rows)
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', return_value=None), patch('evaluation.amul.capture_run.chat_headers', return_value={}):
            path = Path(folder) / 'journal.json'
            with self.assertRaisesRegex(CaptureError, 'transport failed'):
                capture(plan, runtime(plan), lf, path, sender=sender)
            with self.assertRaisesRegex(CaptureError, 'Uncertain request/link'):
                capture(plan, runtime(plan), lf, path, sender=sender, resume=True)
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(lf.writes, [])

    def test_resume_links_saved_response_without_resending_question(self):
        rows = [item('milk', 1)]
        plan, lf, sender = plan_for(rows), FakeLangfuse(rows), Mock()
        iid = rows[0]['id']
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', side_effect=lambda *_: {'datasetRunItems': lf.links} if lf.exists else None), patch('evaluation.amul.capture_run.chat_headers', return_value={}):
            path = Path(folder) / 'journal.json'
            write_json(path, {'plan_sha256': plan['plan_sha256'], 'entries': {iid: {'state': 'response_received', 'response': {'trace_id': 'd' * 32, 'answer': 'જવાબ'}}}})
            result = capture(plan, runtime(plan), lf, path, resume=True, sender=sender, waiter=Mock(return_value={'reply_latency_s': 2}))
            self.assertTrue(result['complete'])
            sender.assert_not_called()
            self.assertEqual(len(lf.writes), 1)

    def test_resume_recognizes_successful_link_after_a_lost_post_response(self):
        rows = [item('milk', 1)]
        plan, lf, sender = plan_for(rows), FakeLangfuse(rows), Mock()
        iid = rows[0]['id']
        lf.exists, lf.links = True, [{'datasetItemId': iid, 'traceId': 'd' * 32}]
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', return_value={'datasetRunItems': lf.links}), patch('evaluation.amul.capture_run.chat_headers', return_value={}):
            path = Path(folder) / 'journal.json'
            write_json(path, {'plan_sha256': plan['plan_sha256'], 'entries': {iid: {'state': 'link_started', 'response': {'trace_id': 'd' * 32, 'answer': 'જવાબ'}}}})
            result = capture(plan, runtime(plan), lf, path, resume=True, sender=sender)
            self.assertTrue(result['complete'])
            sender.assert_not_called()
            self.assertEqual(lf.writes, [])

    def test_uncertain_link_is_not_posted_twice_when_remote_confirmation_is_missing(self):
        rows, sender = [item('milk', 1)], Mock()
        plan, lf = plan_for(rows), FakeLangfuse(rows)
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', return_value=None), patch('evaluation.amul.capture_run.chat_headers', return_value={}):
            path = Path(folder) / 'journal.json'
            write_json(path, {'plan_sha256': plan['plan_sha256'], 'entries': {rows[0]['id']: {'state': 'link_started', 'response': {'trace_id': 'd' * 32}}}})
            with self.assertRaisesRegex(CaptureError, 'Uncertain request/link'):
                capture(plan, runtime(plan), lf, path, resume=True, sender=sender)
            sender.assert_not_called()
            self.assertEqual(lf.writes, [])

    def test_duplicate_foreign_and_wrong_trace_links_are_rejected(self):
        rows = [item('milk', 1)]
        plan, iid = plan_for(rows), rows[0]['id']
        entries = {iid: {'response': {'trace_id': 'd' * 32}}}
        good = {'datasetItemId': iid, 'traceId': 'd' * 32}
        for links in ([good, good], [{'datasetItemId': 'foreign', 'traceId': 'd' * 32}], [dict(good, traceId='wrong')]):
            with self.subTest(links=links), self.assertRaises(CaptureError):
                _reconcile_links(plan, entries, links)
        with self.assertRaisesRegex(CaptureError, 'every planned item'):
            _reconcile_links(plan, entries, [], complete=True)

    def test_trace_validation_failure_stops_the_session_with_response_saved(self):
        rows = [item('milk', 1), item('milk', 2)]
        plan, lf = plan_for(rows), FakeLangfuse(rows)
        sender = Mock(return_value={'trace_id': 'd' * 32, 'answer': 'જવાબ'})
        waiter = Mock(side_effect=CaptureError('build mismatch'))
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', return_value=None), patch('evaluation.amul.capture_run.chat_headers', return_value={}):
            path = Path(folder) / 'journal.json'
            with self.assertRaisesRegex(CaptureError, 'build mismatch'):
                capture(plan, runtime(plan), lf, path, sender=sender, waiter=waiter)
            self.assertEqual(sender.call_count, 1)
            self.assertEqual(lf.writes, [])
            self.assertEqual(json.loads(path.read_text())['entries'][rows[0]['id']]['state'], 'response_received')

    def test_existing_run_and_unexpected_links_are_not_overwritten(self):
        rows, sender = [item('milk', 1)], Mock()
        plan, lf = plan_for(rows), FakeLangfuse(rows)
        with TemporaryDirectory() as folder, patch('evaluation.amul.capture_run._run', return_value={'datasetRunItems': []}), patch('evaluation.amul.capture_run.chat_headers', return_value={}):
            with self.assertRaisesRegex(CaptureError, 'already exists'):
                capture(plan, runtime(plan), lf, Path(folder) / 'journal.json', sender=sender)
        sender.assert_not_called()

    def test_trace_verification_requires_exact_ids_build_io_and_root_timing(self):
        plan = plan_for([item('milk', 1)])
        session, response = plan['sessions'][0], {'trace_id': 'd' * 32, 'answer': 'જવાબ'}
        planned = session['items'][0]
        raw = {'id': response['trace_id'], 'sessionId': session['session_id'], 'name': 'turn',
               'input': planned['question'], 'output': response['answer'], 'metadata': {
                   'amul.schema_version': 'turn.v1', 'amul.pipeline': 'webapp', 'amul.release': RELEASE,
                   'amul.source_lang': 'gu', 'amul.target_lang': 'gu'},
               'observations': [{'id': 'root', 'name': 'turn', 'type': 'SPAN', 'latency': 2.277}]}
        self.assertEqual(verify_trace(raw, plan, session, planned, response)['reply_latency_s'], 2.277)
        for field, value in [('id', 'wrong'), ('sessionId', 'wrong'), ('input', 'wrong'), ('output', 'wrong'), ('name', 'suggestions')]:
            with self.subTest(field=field), self.assertRaises(CaptureError):
                verify_trace(dict(raw, **{field: value}), plan, session, planned, response)
        for key, value in [('amul.release', 'wrong'), ('amul.source_lang', 'en')]:
            with self.subTest(key=key), self.assertRaises(CaptureError):
                verify_trace(dict(raw, metadata=dict(raw['metadata'], **{key: value})), plan, session, planned, response)
        raw['observations'].append({'name': 'suggestions', 'parentObservationId': None})
        with self.assertRaisesRegex(CaptureError, 'Suggestions'):
            verify_trace(raw, plan, session, planned, response)

    def test_streaming_transport_uses_trace_header_and_separates_artifact_frame(self):
        plan = plan_for([item('milk', 1)])
        session = plan['sessions'][0]
        response = Mock()
        response.headers = {'X-Trace-Id': 'd' * 32}
        response.read1.side_effect = ['જવાબ'.encode(), b'\n__CHAT_ARTIFACTS__e30=__END_CHAT_ARTIFACTS__\n', b'']
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch('evaluation.amul.capture_run.build_opener', return_value=opener):
            result = send_chat(plan, session, session['items'][0], {'X-API-Key': 'private'})
        self.assertEqual(result['trace_id'], 'd' * 32)
        self.assertEqual(result['answer'], 'જવાબ')
        self.assertTrue(result['artifact_frame_present'])
        request = opener.open.call_args.args[0]
        self.assertIn('stream=true', request.full_url)
        self.assertIn('channel=web', request.full_url)
        self.assertNotIn('private', request.full_url)

    def test_missing_trace_header_never_falls_back_to_latest_session_trace(self):
        plan = plan_for([item('milk', 1)])
        session = plan['sessions'][0]
        response, opener = Mock(), MagicMock()
        response.headers = {}
        opener.open.return_value.__enter__.return_value = response
        with patch('evaluation.amul.capture_run.build_opener', return_value=opener), self.assertRaisesRegex(CaptureError, 'X-Trace-Id'):
            send_chat(plan, session, session['items'][0], {})
        response.read1.assert_not_called()

    def test_planning_command_has_no_mutating_or_chat_calls(self):
        rows = [item('milk', 1)]
        lf = Mock(host=FakeLangfuse.host)
        lf.dataset_items.return_value = {rows[0]['id']: rows[0]}
        with TemporaryDirectory() as folder:
            output = Path(folder) / 'plan.json'
            args = ['capture', 'plan', '--run', 'offline-test', '--chat-url', URL, '--expected-release', RELEASE,
                    '--output', str(output), '--expected-total', '1', '--expected-eligible', '1']
            with patch('sys.argv', args), patch('evaluation.amul.capture_run.Langfuse', return_value=lf), patch('evaluation.amul.capture_run.send_chat') as sender, patch('builtins.print'):
                main()
            self.assertTrue(output.exists())
            lf._request.assert_not_called()
            sender.assert_not_called()

    def test_journal_lock_prevents_two_capturers_using_the_same_state(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / 'journal.json'
            with journal_lock(path), self.assertRaisesRegex(CaptureError, 'locked'):
                with journal_lock(path):
                    pass


if __name__ == '__main__':
    unittest.main()
