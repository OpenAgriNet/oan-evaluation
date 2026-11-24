"""Current grievance verifier needs both completed verification and policy data."""
import importlib.util
from pathlib import Path
import unittest

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('mock_pmfby_contract', HERE / 'mock_pmfby.py')
mock = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mock)


class PmfbyContractTest(unittest.TestCase):
    def test_current_status_uses_person_tags_and_exact_ticket(self):
        fields = dict(request_type='status_grievance', requestorMobileNo='9876543210',
                      GrievenceSupportTicketNo='PMFBYGRV_DIFFERENT_TICKET')
        body = dict(context=dict(action='search'),message=dict(order=dict(fulfillments=[dict(
            customer=dict(person=dict(tags=[dict(descriptor=dict(code=k),value=v) for k,v in fields.items()])))])))
        response = mock.build_pmfby_grievance_status_response(body, HERE.parent/'common_registry.json')
        tags = response['responses'][0]['message']['catalog']['providers'][0]['items'][0]['tags'][0]['list']
        group = response['responses'][0]['message']['catalog']['providers'][0]['items'][0]['tags'][0]
        self.assertEqual(group['descriptor']['code'], 'grievance_status_fetched')
        values = {r['descriptor']['name']:r['value'] for r in tags}
        self.assertEqual(values['Requestor Mobile Number'], '9876543210')
        self.assertEqual(values['Grievance Support Ticket Number'], fields['GrievenceSupportTicketNo'])

    def test_verified_status_includes_policy(self):
        response = mock.build_pmfby_status_response(
            {'message': {'order_id': '111111'}}, HERE.parent / 'common_registry.json')
        order = response['responses'][0]['message']['order']
        self.assertEqual(order['state'], 'COMPLETED')
        self.assertTrue(order['providers'][0]['items'])

    def test_invalid_otp_never_has_success_or_policy(self):
        for otp in ['000000', '12', 'abcdef', '']:
            response = mock.build_pmfby_status_response(
                {'message': {'order_id': otp}}, HERE.parent / 'common_registry.json')
            order = response['responses'][0]['message']['order']
            self.assertEqual(order['state'], 'FAILED')
            self.assertNotIn('providers', order)
