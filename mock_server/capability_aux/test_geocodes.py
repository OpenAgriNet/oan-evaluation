import unittest
from mock_server.capability_aux.server import geocode_fixture

class Geocodes(unittest.TestCase):
    def test_known_city_and_unknown_city(self):
        feature = geocode_fixture('/api?q=Patna%2C+Bihar')['features'][0]
        self.assertEqual(feature['geometry']['coordinates'], [85.1,25.6])
        self.assertEqual(feature['properties']['type'], 'city')
        self.assertEqual(geocode_fixture('/api?q=Unlisted+village')['features'], [])
    def test_reverse_and_alias(self):
        self.assertEqual(geocode_fixture('/reverse?lat=25.6&lon=85.1')['features'][0]['properties']['name'],'Patna')
        self.assertTrue(geocode_fixture('/api?q=Bengaluru')['features'])
