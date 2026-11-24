"""KHC discovery is available before OTP, unlike grievance submission."""
import json
from mock_server.beckn_search_proxy.mock_current_status import build_response


def request(kind, **values):
    values['request_type'] = kind
    return dict(context=dict(action='init', transaction_id='khc-discovery-test'),
                message=dict(order=dict(provider=dict(id='khc-agri'), fulfillments=[
                    dict(customer=dict(person=dict(tags=[dict(descriptor=dict(code=k), value=v)
                                                         for k, v in values.items()])))])))


def fields(body):
    outcome = build_response(body)['responses'][0]['message']['order']['items'][0]['tags'][0]
    return outcome['descriptor']['code'], {p['descriptor']['code']: p['value'] for p in outcome['list']}


def test_location_without_otp_returns_known_ids():
    code, data = fields(request('find_location', state_name='Bihar', district_name='Patna', name='Baluwa'))
    assert code == 'khc_location'
    village = json.loads(data['matches'])[0]
    assert village['id'] == 219159 and village['state']['id'] == 10


def test_unknown_location_is_not_invented():
    code, data = fields(request('find_location', state_name='Bihar', district_name='Patna', name='Unknown'))
    assert code == 'khc_location' and json.loads(data['matches']) == []


def test_options_use_the_supplied_complaint():
    body = request('grievance_tree', state_name='Bihar', scheme_name='A.I.F.', concern='Bank refused loan')
    code, data = fields(body)
    assert code == 'khc_grievance_tree'
    assert json.loads(data['suggested'])['subCategory']['id'] == 1725
    _, missing = fields(request('grievance_tree', state_name='Bihar', scheme_name='AIF'))
    assert json.loads(missing['suggested']) is None
