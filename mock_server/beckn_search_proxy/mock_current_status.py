"""Synthetic AIF/KCC/KHC network contracts; real API tool functions parse these.

No network access. Registration OTP state is isolated by provider and transaction.
KHC status uses the conversation and grievance: its real client rotates transactions.
"""
import json

SESSIONS = {}
REFERENCES = {
    'states': [dict(stateid=10, statename='Bihar')],
    'districts': [dict(districtid=230, districtname='Patna', stateid=10)],
    'sub-districts': [dict(subdistrictid=1400, subdistrictname='Maner', districtid=230)],
    'villages': [dict(villageid=219159, villagename='Baluwa', subdistrictid=1400)],
    'schemes': [dict(scheme=38, schemename='Agriculture Infrastructure Fund', division=31)],
    'categories': [dict(problemcategory=39, categoryname='Loan application', scheme=38)],
    'sub-categories': [dict(problemsubcategory=1725, subcategoryname='Bank refused loan without explanation', problemcategory=39)],
}


def tag(code, message='', **fields):
    return dict(descriptor=dict(code=code, short_desc=message), list=[
        dict(descriptor=dict(code=k), value=str(v)) for k, v in fields.items()])


def build_response(body, then_cfg=None, config_dir=None):
    action = body['context']['action']
    request_order = body['message']['order']
    provider = request_order['provider']['id']
    values = {t['descriptor']['code']: t.get('value', '')
              for f in request_order.get('fulfillments', [])
              for t in f.get('customer', {}).get('person', {}).get('tags', [])}
    kind = values.get('request_type', '')
    context = body['context']
    key = (provider, context['transaction_id'])
    if provider == 'khc-agri' and action == 'status':
        session = context.get('tags', {}).get('session_id')
        if session:
            key = (provider, 'status', session, values.get('grievanceNo', ''))
    state = SESSIONS.setdefault(key, {})
    items = []
    outcome = tag('invalid_request', 'Unknown synthetic request type.')
    if kind == 'get_otp':
        state.update(sent=True, verified=False)
        outcome = tag('otp_sent', 'OTP sent to registered mobile.', masked_mobile='******3210')
    elif kind == 'get_reference' and provider == 'khc-agri':
        options = REFERENCES.get(values.get('reference_type'), [])
        outcome = tag('khc_reference', 'Reference options.', options=json.dumps(options), total=len(options), page=1)
    elif kind == 'find_location' and provider == 'khc-agri':
        # Discovery precedes OTP in the current KHC contract.
        state_name = values.get('state_name', '').strip().lower()
        district_name = values.get('district_name', '').strip().lower()
        place = values.get('name', '').strip().lower()
        needs = 'state' if state_name != 'bihar' else 'district' if district_name != 'patna' else 'geocode'
        matches = []
        if state_name == 'bihar' and district_name == 'patna' and place == 'baluwa':
            needs = ''
            matches = [dict(level='village', id=219159, name='Baluwa',
                            state=dict(id=10, name='Bihar'), district=dict(id=230, name='Patna'),
                            subDistrict=dict(id=1400, name='Maner'))]
        outcome = tag('khc_location', 'Synthetic location lookup.', matches=json.dumps(matches),
                      total=len(matches), needs=json.dumps(needs), district=json.dumps(dict(id=230, name='Patna')))
    elif kind == 'grievance_tree' and provider == 'khc-agri':
        state_row = dict(id=10, name='Bihar')
        scheme_row = dict(id=38, name='Agriculture Infrastructure Fund (AIF)', division=31)
        category = dict(id=39, name='Loan application')
        sub = dict(id=1725, name='Bank refused loan without explanation')
        scheme_name = ''.join(c for c in values.get('scheme_name', '').lower() if c.isalnum())
        if values.get('state_name', '').strip().lower() != 'bihar':
            outcome = tag('khc_grievance_tree', needs=json.dumps('state'), matches=json.dumps([state_row]))
        elif scheme_name not in {'aif', 'agricultureinfrastructurefund'}:
            outcome = tag('khc_grievance_tree', state=json.dumps(state_row), needs=json.dumps('scheme'),
                          schemes=json.dumps([scheme_row]))
        else:
            concern = values.get('concern', '').lower()
            suggestion = dict(category=category, subCategory=sub) if 'bank' in concern and 'loan' in concern else None
            outcome = tag('khc_grievance_tree', state=json.dumps(state_row), scheme=json.dumps(scheme_row),
                          categories=json.dumps([{**category, 'subCategories': [sub]}]),
                          suggested=json.dumps(suggestion))
    elif kind == 'verify_otp' or (provider == 'kcc-agri' and kind == 'application_status' and values.get('otp')):
        if not state.get('sent') or values.get('otp') != '111111':
            outcome = tag('invalid_otp', 'OTP verification failed. Ask for the correct OTP.')
        else:
            state['verified'] = True
            outcome = tag('otp_verified', 'OTP verified.', isRegistered='true', beneficiary_name='Test Farmer')
    elif provider == 'khc-agri' and action == 'status':
        number = values.get('grievanceNo', '')
        if kind == 'get_status_otp':
            state['status_otp_for'] = number
            outcome = tag('status_otp_sent', 'Status OTP sent to grievance mobile.')
        elif kind == 'verify_status_otp' and state.get('status_otp_for') == number and values.get('otp') == '111111':
            state.pop('status_otp_for')
            outcome = tag('grievance_status', 'Grievance retrieved.', grievanceNo=number,
                          currentStatus='OPEN', currentLevel='STATE', createdAt='2026-10-21')
        else:
            outcome = tag('invalid_otp', 'Send and verify the current grievance status OTP.')
    elif not state.get('verified'):
        outcome = tag('otp_required', 'Verify the current session OTP before continuing.')
    elif provider == 'aif-agri' and kind == 'loan_status':
        outcome = tag('loan_status', 'Under review', status='Under review',
                      loan_application_number=values.get('loan_application_number', ''))
    elif provider == 'aif-agri' and kind == 'grievance_status':
        outcome = tag('grievance_status', 'One support ticket.')
        items = [dict(tags=[tag('ticket', loan_application_number='730015', query_type='Loan delay',
                               status='Open', description='Application awaiting bank review.')])]
    elif provider == 'khc-agri' and kind == 'create_grievance':
        outcome = (tag('grievance_created', 'Grievance registered once.', grievanceNo='BR/240926/2', currentStatus='OPEN')
                   if values.get('confirmed') == 'true' else tag('invalid_request', 'Explicit confirmation required.'))
    if provider == 'kcc-agri' and action == 'status' and state.get('verified') and outcome['descriptor']['code'] != 'invalid_otp':
        if values.get('application_no'):
            outcome = tag('application_status', 'Application retrieved.', application_no=values['application_no'],
                          status='APPROVED', farmer_name='Test Farmer', required_loan_amount='100000',
                          bank_name='Test Cooperative Bank', branch_name='Patna')
        else:
            outcome = tag('multiple_applications', 'Choose an application.')
            items = [dict(tags=[tag('application', application_no=n, status='APPROVED', required_loan_amount='100000')])
                     for n in ['51001', '51002']]
    order = dict(items=[dict(tags=[outcome])]) if action == 'init' else dict(tags=[outcome], items=items)
    return {'responses': [{'message': {'order': order}}]}
