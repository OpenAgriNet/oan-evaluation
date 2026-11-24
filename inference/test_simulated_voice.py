"""Offline checks: python -m unittest inference.test_simulated_voice."""
import json
import asyncio
import os
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

os.environ.setdefault('SCENARIO_GEMMA_BASE_URL', 'http://127.0.0.1:8867/v1')
os.environ.setdefault('SCENARIO_GEMMA_MODEL', 'openai/google/gemma-4-31b-it')
import httpx
from inference.run_simulated_scenario import stream_voice_answer, stream_chat_answer, GemmaUserSimulator
from inference.run_simulated_scenario import run_session, run_goal_session
from inference.run_simulated_scenario import language_passes
from inference.run_simulated_scenario import TurnResult


def event(delta):
    return 'data: ' + json.dumps({'choices': [{'delta': delta}]}) + '\n\n'


class VoiceStreamTest(unittest.IsolatedAsyncioTestCase):
    async def test_goal_session_retains_failed_reply_after_successful_planned_turn(self):
        rows = [dict(session_id='1', question_id='1-1', question='Start booking')]
        bot = SimpleNamespace(turns=[TurnResult('Your farmer ID?', 10, 20)],
            call=AsyncMock(side_effect=['Your farmer ID?', RuntimeError('broken stream')]))
        with patch.dict('inference.run_simulated_scenario.SIM_SPECS',
                        {'1': {'user_messages': ['1-1'], 'facts': {}}}), \
             patch('inference.run_simulated_scenario.decide_farmer_turn',
                   new=AsyncMock(return_value={'action': 'answer', 'message': '123456', 'used': []})):
            result = await run_goal_session('1', rows, bot_agent=bot, language='English',
                                            sem=asyncio.Semaphore(1))
        self.assertEqual([r['question_id'] for r in result], ['1-1', '1-1.r1'])
        self.assertEqual(result[0]['answer'], 'Your farmer ID?')
        self.assertEqual(result[1]['question'], '123456')
        self.assertEqual(result[1]['kind'], 'reply')
        self.assertEqual(result[1]['answer'], 'ERROR: RuntimeError: broken stream')

    async def test_late_session_failure_preserves_successes_and_marks_unattempted_turns(self):
        rows = [dict(session_id='1', question_id=f'1-{i}', question=f'q{i}') for i in range(1, 5)]
        async def run(**kwargs):
            bot = kwargs['agents'][0]
            for question in ['q1', 'q2', 'q3']:
                await bot.call(SimpleNamespace(last_new_user_message_str=lambda: question))
        responses = [TurnResult('a1', 10, 20), TurnResult('a2', 30, 40), RuntimeError('context limit')]
        with patch('inference.run_simulated_scenario.scenario.arun', side_effect=run), \
             patch('inference.run_simulated_scenario.stream_chat_answer', new=AsyncMock(side_effect=responses)):
            result = await run_session('1', rows, client=None, url='http://test', token='',
                run_uuid='test', sem=asyncio.Semaphore(1), fixed_questions=True)
        self.assertEqual([r['answer'] for r in result[:2]], ['a1', 'a2'])
        self.assertEqual(result[1]['latency_answer'], '40.00')
        self.assertIn('context limit', result[2]['answer'])
        self.assertIn('Not attempted', result[3]['answer'])
        self.assertEqual([r['question'] for r in result], ['q1','q2','q3','q4'])

    async def test_consent_fixture_uses_actual_callback_endpoint(self):
        client = SimpleNamespace(post=AsyncMock(return_value=httpx.Response(200, request=httpx.Request('POST','http://test'))))
        rows = [dict(session_id='1008', question_id='1008-1', question='weather', agristack_consent='true')]
        with patch('inference.run_simulated_scenario.scenario.arun', new=AsyncMock(return_value=SimpleNamespace(messages=[]))):
            await run_session('1008', rows, client=client, url='http://test/api/chat/', token='',
                              run_uuid='fixture', sem=asyncio.Semaphore(1), fixed_questions=True)
        self.assertEqual(client.post.call_args.args[0], 'http://test/api/callback')
        self.assertEqual(client.post.call_args.kwargs['json']['consentName'], 'CONTINUE WITH CONSENT')
        self.assertEqual(client.post.call_args.kwargs['json']['callbackSessionId'], 'fixture_en_1008')

    def test_complete_language_sets(self):
        text = language_passes('text')
        voice = language_passes('voice')
        self.assertEqual(len(text), 13)
        self.assertEqual(len(voice), 11)
        self.assertEqual({p['source_lang'] for p in text},
                         {'en','hi','mr','gu','ml','kn','ta','te','bn','as','or','pa','mai'})
        self.assertEqual({p['source_lang'] for p in voice},
                         {'en','hi','mr','gu','ml','kn','ta','te','bn','od','pa'})

    async def test_fixed_questions_preserve_numeric_order_without_simulator(self):
        rows = [dict(session_id='1', question_id=f'1-{i}', question=f'q{i}') for i in [10, 2, 1]]
        async def run(**kwargs):
            self.assertEqual(kwargs['script'], ['q1', 'bot', 'q2', 'bot', 'q10', 'bot', 'done'])
            return SimpleNamespace(messages=[dict(role='user', content=f'q{i}') for i in [1, 2, 10]])
        with patch('inference.run_simulated_scenario.scenario.message', side_effect=lambda m: m['content']), \
             patch('inference.run_simulated_scenario.scenario.agent', return_value='bot'), \
             patch('inference.run_simulated_scenario.scenario.succeed', return_value='done'), \
             patch('inference.run_simulated_scenario.scenario.user', side_effect=AssertionError('simulated user')), \
             patch('inference.run_simulated_scenario.scenario.arun', side_effect=run):
            result = await run_session('1', rows, client=None, url='http://test', token='',
                run_uuid='test', sem=asyncio.Semaphore(1), fixed_questions=True)
        self.assertEqual([r['question'] for r in result], ['q1', 'q2', 'q10'])

    async def test_empty_simulator_message_is_rejected(self):
        response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=' '))])
        with patch('inference.run_simulated_scenario.litellm.acompletion', new=AsyncMock(return_value=response)):
            with self.assertRaisesRegex(RuntimeError, 'empty message'):
                await GemmaUserSimulator('test', ['hello'], 'English').call(SimpleNamespace(messages=[]))

    async def test_snapshots_headers_and_first_spoken_text(self):
        def respond(request):
            self.assertEqual(request.method, 'POST')
            self.assertEqual(request.headers['X-Language'], 'hi')
            self.assertEqual(request.headers['X-Session-ID'], 'test-session')
            self.assertEqual(request.headers['Authorization'], 'Bearer test')
            self.assertEqual(json.loads(request.content)['messages'], [{'role': 'user', 'content': 'hello'}])
            body = ': heartbeat\n\n' + event({'role': 'assistant'})
            for audio in ['', 'नमस्ते', 'नमस्ते किसान']:
                body += event({'content': json.dumps({'audio': audio})})
            return httpx.Response(200, text=body + event({}) + 'data: [DONE]\n\n')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch('inference.run_simulated_scenario.time') as clock:
                clock.perf_counter.side_effect = [10, 12, 15]
                result = await stream_voice_answer(client, url='http://test/api/v1/chat/completions',
                    token='test', query='hello', session_id='test-session', language='hi', tenant_id='eval')
        self.assertEqual(result, ('नमस्ते किसान', 2000, 5000))

    async def test_truncated_empty_and_http_errors(self):
        for response in [httpx.Response(200, text=event({'content': json.dumps({'audio': 'Hi'})})),
                         httpx.Response(200, text='data: [DONE]\n\n'),
                         httpx.Response(401, text='Unauthorized')]:
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda _: response)) as client:
                with self.assertRaises((RuntimeError, httpx.HTTPStatusError)):
                    await stream_voice_answer(client, url='http://test/voice', token='',
                        query='hi', session_id='s', language='en', tenant_id='eval')

    async def test_text_mode_unchanged(self):
        def respond(request):
            self.assertEqual(request.method, 'GET')
            self.assertEqual(request.url.params['query'], 'hello')
            return httpx.Response(200, text='Hello farmer')
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            result = await stream_chat_answer(client, url='http://test/api/chat/', token='test',
                                             query='hello', session_id='s')
        self.assertEqual(result.text, 'Hello farmer')
