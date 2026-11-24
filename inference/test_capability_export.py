"""Preserve observed turns without inventing tool evidence after stream failures."""
import csv
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from inference.run_capability_comparison import export_pass


class ExportTests(unittest.TestCase):
    def run_export(self, answers, history):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root/'run.log').write_text('run_uuid: test\n')
            rows = [dict(session_id='1', question_id=f'1-{i}', question=f'q{i}', answer=answer)
                    for i, answer in enumerate(answers, 1)]
            for name, records in [('answers.csv', rows), ('fixture.csv', [
                    dict(question_id=r['question_id'], question_english=r['question']) for r in rows])]:
                with (root/name).open('w') as f:
                    writer = csv.DictWriter(f, fieldnames=list(records[0]))
                    writer.writeheader()
                    writer.writerows(records)
            raw = json.dumps(history).encode() if history is not None else b'\n'
            variant = dict(repo='text',variant='after',redis_container='test',redis_db=0,worktree='test')
            with patch('inference.run_capability_comparison.subprocess.check_output', return_value=raw), \
                 patch('inference.run_capability_comparison.registered_tools', return_value=set()):
                export_pass(root, variant, root/'fixture.csv', 'English', 'test')
            self.summary = json.loads((root/'summary.json').read_text())
            return json.loads((root/'per_turn.json').read_text())

    def test_missing_history_is_allowed_only_for_error_rows(self):
        history = [dict(parts=[dict(part_kind='user-prompt', content='q1')])]
        rows = self.run_export(['answer', 'ERROR: stream failed', 'ERROR: Not attempted'], history)
        self.assertEqual(rows[0]['answer'], 'answer')
        self.assertNotIn('history_missing', rows[0])
        self.assertTrue(all(r['history_missing'] and not r['calls'] for r in rows[1:]))
        with self.assertRaises(AssertionError):
            self.run_export(['answer', 'unexplained successful answer'], history)

    def test_first_turn_failure_with_no_redis_history(self):
        rows = self.run_export(['ERROR: stream failed', 'ERROR: Not attempted'], None)
        self.assertTrue(all(r['history_missing'] for r in rows))

    def test_rejected_unknown_tool_is_kept_for_judging(self):
        history = [dict(parts=[dict(part_kind='user-prompt', content='q1'),
                   dict(part_kind='tool-call', tool_name='invented.tool', tool_call_id='bad', args={}),
                   dict(part_kind='retry-prompt', tool_name='invented.tool', tool_call_id='bad', content='Unknown tool')])]
        rows = self.run_export(['Recovered answer'], history)
        self.assertEqual(rows[0]['calls'][0]['tool_name'], 'invented.tool')
        self.assertEqual(rows[0]['retries'][0]['content'], 'Unknown tool')
        self.assertEqual(self.summary['unregistered_calls'], {'invented.tool': 1})
