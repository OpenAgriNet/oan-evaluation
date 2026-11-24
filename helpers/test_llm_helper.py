import asyncio
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from helpers.llm_helper import llm_call


class UsageCompatibilityTests(unittest.TestCase):
    def test_method_and_property_usage(self):
        usage = SimpleNamespace(input_tokens=2, output_tokens=3, total_tokens=5, details={})
        for accessor in [usage, lambda: usage]:
            class FakeAgent:
                def __init__(self, **kwargs): pass
                async def run(self, prompt):
                    return SimpleNamespace(output='answer', usage=accessor)
            with patch('helpers.llm_helper.Agent', FakeAgent):
                result = asyncio.run(llm_call(prompt='test', llm={'model':'offline-test'}))
            self.assertEqual(result['text'], 'answer')
            self.assertEqual(result['usage']['total_tokens'], 5)


if __name__ == '__main__':
    unittest.main()
