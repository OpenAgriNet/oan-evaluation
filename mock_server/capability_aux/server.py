"""Loopback-only NPSS and resolver fixtures, not model/agent substitutes."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
import urllib.request
from urllib.parse import urlparse, parse_qs
from pathlib import Path

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=')
CALLBACKS = {f'http://127.0.0.1:{port}/api/resolver/on_resolve' for port in [int(p) for p in os.environ.get('FIXTURE_CALLBACK_PORTS', '8011,8013').split(',')]}


CITIES = json.loads((Path(__file__).resolve().parents[1] / 'common_registry.json').read_text())['cities']
CITIES += json.loads(Path(__file__).with_name('geocodes.json').read_text())

def geocode_fixture(path):
    """Photon-shaped fixture; unknown places return no match instead of invented coordinates."""
    query = parse_qs(urlparse(path).query)
    route = urlparse(path).path
    if route == '/api':
        name = query.get('q', [''])[0].lower().replace('bengaluru', 'bangalore').replace('nasik', 'nashik')
        matches = [city for city in CITIES if city['key'] in name]
    elif route == '/reverse':
        try:
            lat, lon = float(query['lat'][0]), float(query['lon'][0])
        except (KeyError, ValueError):
            return {'features': []}
        matches = sorted(CITIES, key=lambda c:(c['latitude']-lat)**2+(c['longitude']-lon)**2)[:1]
    else:
        return None
    features = []
    for city in matches:
        parts = city['query'].split(', ')
        features.append({'type':'Feature', 'geometry':{'type':'Point', 'coordinates':[city['longitude'],city['latitude']]},
                         'properties':{'name':parts[0], 'city':parts[0], 'county':parts[0],
                                       'state':parts[-2] if len(parts)>2 else parts[0], 'country':'India', 'type':'city'}})
    return {'type':'FeatureCollection', 'features':features}


class Handler(BaseHTTPRequestHandler):
    def send(self, value, content_type='application/json'):
        raw = value if isinstance(value, bytes) else json.dumps(value).encode()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        location = geocode_fixture(self.path)
        if location is not None:
            return self.send(location)
        if self.path == '/fixtures/crop.png':
            return self.send(PNG, 'image/png')
        if self.path == '/health':
            return self.send({'ok': True, 'synthetic': True})
        self.send_error(404)

    def do_POST(self):
        length = int(self.headers.get('Content-Length', '0'))
        if not 0 <= length <= 10_000_000:
            return self.send_error(413)
        raw = self.rfile.read(length)
        if self.path == '/telemetry':
            return self.send({'ok': True})
        if self.path == '/npss/api/Vistaar/token':
            return self.send({'token': 'synthetic-npss-fixture'})
        if self.path == '/npss/api/Vistaar/analyze-image':
            if self.headers.get('Authorization') != 'Bearer synthetic-npss-fixture':
                return self.send_error(401)
            return self.send({'pest': 'Leaf spot', 'crop': 'Rice', 'pathogenClass': 'Fungal',
                              'description': 'Synthetic benchmark diagnosis. Consult a local expert before treatment; no chemical dosage is supplied.'})
        if self.path == '/resolver/v1/resolve':
            body = json.loads(raw)
            if body.get('callback_url') not in CALLBACKS:
                return self.send_error(400, 'Only isolated benchmark callback URLs are allowed')
            callback = json.dumps({'ref': body['ref'], 'status': 'ok', 'cells': [{'key': 'synthetic-patna'}]}).encode()
            request = urllib.request.Request(body['callback_url'], data=callback,
                                             headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(request, timeout=10) as response:
                assert response.status == 200
            return self.send({'accepted': True})
        self.send_error(404)


if __name__ == '__main__':
    assert PNG.startswith(b'\x89PNG\r\n\x1a\n')
    assert all(url.startswith('http://127.0.0.1:') for url in CALLBACKS)
    ThreadingHTTPServer(('127.0.0.1', 17893), Handler).serve_forever()
