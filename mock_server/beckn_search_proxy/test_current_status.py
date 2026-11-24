import unittest
from mock_current_status import SESSIONS, build_response


def request(provider, action, request_type, transaction='test', **fields):
    fields['request_type'] = request_type
    return {'context': {'action': action, 'transaction_id': transaction}, 'message': {'order': {
        'provider': {'id': provider}, 'fulfillments': [{'customer': {'person': {'tags': [
            {'descriptor': {'code': key}, 'value': value} for key, value in fields.items()]}}}]}}}


def outcome(response, action):
    order = response['responses'][0]['message']['order']
    return (order['items'][0]['tags'] if action == 'init' else order['tags'])[0]['descriptor']['code']


class CurrentStatusTests(unittest.TestCase):
    def setUp(self):
        SESSIONS.clear()

    def test_aif_otp_gates_and_transaction_isolation(self):
        def call(action, kind, **kwargs):
            return outcome(build_response(request('aif-agri', action, kind, **kwargs)), action)
        self.assertEqual(call('status', 'loan_status'), 'otp_required')
        self.assertEqual(call('init', 'verify_otp', otp='111111'), 'invalid_otp')
        self.assertEqual(call('init', 'get_otp'), 'otp_sent')
        self.assertEqual(call('init', 'verify_otp', otp='000000'), 'invalid_otp')
        self.assertEqual(call('init', 'verify_otp', otp='111111'), 'otp_verified')
        self.assertEqual(call('status', 'loan_status'), 'loan_status')
        self.assertEqual(call('status', 'loan_status', transaction='different'), 'otp_required')

    def test_kcc_list_then_selection_without_new_otp(self):
        build_response(request('kcc-agri', 'init', 'get_otp'))
        listed = build_response(request('kcc-agri', 'status', 'application_status', otp='111111'))
        self.assertEqual(outcome(listed, 'status'), 'multiple_applications')
        self.assertEqual(len(listed['responses'][0]['message']['order']['items']), 2)
        selected = build_response(request('kcc-agri', 'status', 'application_status', application_no='51002'))
        self.assertEqual(outcome(selected, 'status'), 'application_status')

    def test_khc_requires_verification_and_confirmation(self):
        self.assertEqual(outcome(build_response(request('khc-agri', 'init', 'create_grievance', confirmed='true')), 'init'), 'otp_required')
        build_response(request('khc-agri', 'init', 'get_otp'))
        build_response(request('khc-agri', 'init', 'verify_otp', otp='111111'))
        self.assertEqual(outcome(build_response(request('khc-agri', 'init', 'create_grievance')), 'init'), 'invalid_request')
        self.assertEqual(outcome(build_response(request('khc-agri', 'init', 'create_grievance', confirmed='true')), 'init'), 'grievance_created')

    def test_khc_status_otp_is_separate_and_single_use(self):
        def call(kind, **fields):
            return outcome(build_response(request('khc-agri', 'status', kind,
                           grievanceNo='BR/240926/2', **fields)), 'status')
        self.assertEqual(call('verify_status_otp', otp='111111'), 'invalid_otp')
        self.assertEqual(call('get_status_otp'), 'status_otp_sent')
        self.assertEqual(call('verify_status_otp', otp='000000'), 'invalid_otp')
        self.assertEqual(call('verify_status_otp', otp='111111'), 'grievance_status')
        self.assertEqual(call('verify_status_otp', otp='111111'), 'invalid_otp')

    def test_khc_status_rotating_transactions_and_session_isolation(self):
        def call(kind, transaction, session='session-1', grievance='BR/240926/2', **fields):
            body = request('khc-agri', 'status', kind, transaction=transaction,
                           grievanceNo=grievance, **fields)
            body['context']['tags'] = {'session_id': session}
            return outcome(build_response(body), 'status')
        self.assertEqual(call('get_status_otp', 'tx-1'), 'status_otp_sent')
        self.assertEqual(call('verify_status_otp', 'tx-2', session='other', otp='111111'), 'invalid_otp')
        self.assertEqual(call('verify_status_otp', 'tx-3', grievance='BR/240926/3', otp='111111'), 'invalid_otp')
        self.assertEqual(call('verify_status_otp', 'tx-4', otp='000000'), 'invalid_otp')
        self.assertEqual(call('verify_status_otp', 'tx-5', otp='111111'), 'grievance_status')
        self.assertEqual(call('verify_status_otp', 'tx-6', otp='111111'), 'invalid_otp')
