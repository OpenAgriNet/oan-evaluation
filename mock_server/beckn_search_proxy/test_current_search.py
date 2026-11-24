import unittest
from mock_current_search import build_response


class CurrentSearchTests(unittest.TestCase):
    def test_scheme_network_preserves_code_and_documents_limitations(self):
        result = build_response(dict(message=dict(intent=dict(category=dict(descriptor=dict(code='scheme-agri-qdrant')),
            item=dict(descriptor=dict(code='mif'))))))
        item = result['responses'][0]['message']['catalog']['providers'][0]['items'][0]
        self.assertEqual(item['descriptor']['code'], 'mif')
        self.assertIn('does not specify subsidy rates', item['descriptor']['long_desc'])

    def test_gfr_registry_ids_can_be_used_by_recommendations(self):
        result = build_response(dict(message=dict(order=dict(provider=dict(id='gfr-agri'),
            items=[dict(id='get-registries')]))))
        fields = result['responses'][0]['message']['catalog']['providers'][0]['items'][0]['tags'][0]['list']
        values = {v['descriptor']['code']:v['value'] for v in fields}
        self.assertGreaterEqual(len(values['stateId']), 6)
        self.assertEqual(values['stateName'], 'Bihar')

    def test_weather_preserves_requested_coordinates(self):
        result = build_response(dict(context=dict(action='search'), message=dict(intent=dict(
            category=dict(descriptor=dict(code='WFC')),
            fulfillment=dict(stops=[dict(location=dict(lat=25.60,lon=85.10))])))))
        self.assertEqual(result['context']['action'], 'on_search')
        items = result['responses'][0]['message']['catalog']['providers'][0]['items']
        self.assertEqual(len(items), 7)
        self.assertIn('latitude 25.6, longitude 85.1', items[0]['descriptor']['long_desc'])


if __name__ == '__main__':
    unittest.main()
