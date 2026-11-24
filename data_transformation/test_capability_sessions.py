import unittest
from data_transformation.transform_capability_sessions import calls_for, transform


class CapabilityEvidenceTests(unittest.TestCase):
    def test_return_and_retry_are_bound_by_call_id_not_name(self):
        turn = dict(question_id='1001-1', calls=[
            dict(tool_call_id='a', tool_name='verify', args='{"otp":"000000"}'),
            dict(tool_call_id='b', tool_name='verify', args='{"otp":"111111"}')],
            returns=[dict(tool_call_id='b', content='verified')],
            retries=[dict(tool_call_id='a', content='invalid')])
        first, second = calls_for(turn)
        self.assertIsNone(first['output'])
        self.assertEqual(first['retry'], 'invalid')
        self.assertEqual(second['output'], 'verified')

    def test_all_answers_preserved_and_english_outputs_not_shared(self):
        turn = dict(session_id='1001', question_id='1001-1', question='local question',
            answer='local answer', language='hi', calls=[], returns=[], retries=[])
        rows = transform([turn], [dict(session_id='1001', question_id='1001-1', question_english='English question')],
                         [turn], {'1001':'contract'}, 'text')
        self.assertIn('local answer', rows[0]['conversation_history'])
        self.assertEqual(rows[0]['tool_calls_evidence'], '[]')
        self.assertEqual(rows[0]['english_reference'], [dict(question_id='1001-1', calls=[])])
        with self.assertRaises(AssertionError):
            transform([turn,turn], [dict(session_id='1001', question_id='1001-1', question_english='q')],
                      [turn], {'1001':'contract'}, 'text')


if __name__ == '__main__':
    unittest.main()
