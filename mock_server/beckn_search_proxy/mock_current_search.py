"""Synthetic current-tool search responses, without live provider fallthrough."""
import json
from datetime import date, timedelta
from mock_current_status import tag


def catalog(items, provider='test-provider', name='Synthetic test provider'):
    return {'responses': [{'context': {'action': 'on_search'}, 'message': {'catalog': {
        'descriptor': {'name': 'Test catalog'},
        'providers': [{'id': provider, 'descriptor': {'name': name}, 'items': items}],
    }}}]}


def build_response(body, then_cfg=None, config_dir=None):
    message = body['message']
    order = message.get('order', {})
    intent = message.get('intent', {})
    provider = (order.get('provider') or intent.get('provider') or {}).get('id', '')
    if intent.get('category', {}).get('descriptor', {}).get('code') == 'knowledge-advisory':
        query = intent.get('item', {}).get('descriptor', {}).get('name', '')
        text = ('Synthetic advisory fixture for ' + query + '. This fixture supplies no pesticide dose, '
                'intercrop recommendation or treatment instructions. Confirm field-specific recommendations '
                'with an agricultural extension officer.')
        return catalog([dict(id='advisory-fixture', descriptor=dict(name='Synthetic crop advisory', long_desc=text),
            tags=[tag('chunk-details', text=text, source='Synthetic advisory', score='0.9')])], 'advisory-fixture')
    if intent.get('category', {}).get('descriptor', {}).get('code') == 'scheme-agri-qdrant':
        descriptor = intent.get('item', {}).get('descriptor', {})
        code = descriptor.get('code') or 'unspecified-scheme'
        content = (f'Synthetic benchmark document for {code}. Farmers may request application '
                   'guidance from their local agriculture office. This fixture does not specify '
                   'subsidy rates, eligibility thresholds or exclusions; confirm current details '
                   'with the implementing office before applying.')
        return catalog([dict(id='fixture-'+code, descriptor=dict(name=code, code=code, long_desc=content),
            tags=[tag('chunk-details', text=content, scheme_code=code, scheme_name=code,
                      section='general', source='Synthetic scheme document', score='0.9')])], 'scheme-fixture')
    if intent.get('category', {}).get('descriptor', {}).get('code') == 'WFC':
        location = intent['fulfillment']['stops'][0]['location']
        context = dict(body['context'], action='on_search')
        items = [dict(id=f'forecast-{i}', matched=True, recommended=False,
                      descriptor=dict(name=(date.today()+timedelta(days=i)).isoformat(),
                          long_desc=(f'Synthetic forecast for latitude {location["lat"]}, longitude {location["lon"]}: '
                              'minimum 22 C, maximum 30 C, rainfall 2 mm, humidity 70 percent, '
                              'wind 8 km/h, partly cloudy.'))) for i in range(7)]
        response = catalog(items, 'imd-fixture', 'India Meteorological Department')
        response['context'] = context
        response['responses'][0]['context'] = context
        return response
    if provider == 'smam':
        return catalog([dict(id='SMAM202600123', descriptor=dict(name='Power tiller'),
                             tags=[tag('implement_status', status='Approved', subsidy_amount='25000')])],
                       provider='SMAM202600123', name='SMAM202600123')
    if provider == 'agristack-agri':
        sections = {
            'farmer-data': dict(name='Test Farmer', gender='Male', category='General', state='Bihar', district='Patna'),
            'land-data': [dict(village='Baluwa', subdistrict='Maner', district='Patna', state='Bihar', area_ha=1.2)],
            'crop-survey-data': [dict(crop='Rice', season='Kharif', year='2026', area_ha=1.2)],
            'land-ownership-data': dict(ownership='Owned', area_ha=1.2),
        }
        return catalog([dict(id='farmer-record', tags=[tag(k, data=json.dumps(v)) for k, v in sections.items()])], provider)
    if provider == 'gfr-agri':
        item = (order.get('items') or [{}])[0].get('id', '')
        if 'regist' in item:
            return catalog([dict(id='101', descriptor=dict(name='Rice', long_desc='Rice — irrigated Kharif'),
                tags=[tag('crop_details', variety='General', irrigationType='Irrigated', season='Kharif',
                          GFRavailable='true', stateId='fixture-bihar-10', stateName='Bihar',
                          districtId='fixture-patna-230', districtName='Patna')])], provider)
        data = [dict(crop='Rice', fertilizersdata=[dict(name='Urea', values=100, unit='kg/ha'),
                                                    dict(name='DAP', values=50, unit='kg/ha')])]
        return catalog([dict(id='gfr-recommendation', descriptor=dict(name='Crop recommendation'),
                             tags=[tag('recommendations', data=json.dumps(data))])], provider)
    if body.get('context', {}).get('domain') == 'schemes:amul-union':
        return catalog([dict(id='banas-feed-support', descriptor=dict(name='Banas cattle feed support',
            long_desc='Synthetic fixture: registered Banas dairy members may apply at their village milk society for cattle-feed assistance. Confirm current terms at the society.'))], 'banas-union', 'Banas Dairy')
    code = intent.get('item', {}).get('descriptor', {}).get('code', '')
    if code in ('ndksp-drip-irrigation', 'ndksp-farm-pond-lining', 'aif'):
        return catalog([dict(id=code, descriptor=dict(name='Nanaji Deshmukh Krishi Sanjivani irrigation assistance',
            long_desc='Synthetic fixture: assistance supports irrigation equipment in participating Maharashtra villages. Apply through the local agriculture office; confirm local eligibility and available support there.'))], 'maha-vistaar', 'MahaVistaar')
    return None
