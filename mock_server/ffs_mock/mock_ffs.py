"""Local FFS mock for benchmarks. Never forwards: no request leaves this machine (FFS sends real OTPs and
creates real bookings, so tests use this mock only; user rule 2026-11-24).

Built on the bundled synthetic provider (backend.py, from the product's test Backend), adjusted
to the benchmark farmer in data/combined_20261123/ffs_sessions_en.json:
  - Farmer ID 99000000001, Gujarat (LGD 24), OTP 246810, village Langhnaj, survey numbers 101 and 102.
  - Entitlement 7 bags urea (group 11), 2 bags DAP (group 12); three named shops.
Failure cases (taken from real bh-dev FFS sessions):
  - login for Farmer ID 99000000009, or any state other than Gujarat -> provider error (farmer not found);
  - any OTP other than 246810 -> provider rejects it;
  - a booking at "Shree Pramukh Agro Center" (retailer 0) -> provider booking error (stock changed).

Run: python -m mock_server.ffs_mock.mock_ffs [port]   (default port 17920)
Every decoded request is appended to calls.jsonl next to this file.
"""
import json
import sys
import time
from pathlib import Path


import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse  # noqa: E402

from mock_server.ffs_mock.backend import Backend

HERE = Path(__file__).resolve().parent
FARMER, STATE, OTP = '99000000001', 24, '246810'
UNREGISTERED = {'99000000009'}
SHOPS = ['Shree Pramukh Agro Center', 'Aarti Agro and Seeds', 'Aadhya Agro Agency', 'Patel Krishi Kendra',
         'Mehsana Fertilizer Depot', 'Gujarat Agro Inputs', 'Kisan Seva Kendra', 'Unjha Agro Traders']
FAILING_SHOP = 0

backend = Backend()
backend.expected_otp = OTP
backend.token_lifetime = 3600
for i, land in enumerate(backend.land):
    land.update(stateLgdCode=STATE, districtLgdCode=468, subDistrictLgdCode=3719, villageLgdCode=518001,
                villageName='Langhnaj', surveyNumber=str(101 + i), subSurveyNumber='')
backend.crops = [{'cropCode': '0101100', 'cropName': 'ડાંગર (Rice)'}, {'cropCode': '0501100', 'cropName': 'કપાસ (Cotton)'},
                 {'cropCode': '0401100', 'cropName': 'મગફળી (Groundnut)'}, {'cropCode': '0101900', 'cropName': 'ઘઉં (Wheat)'},
                 {'cropCode': '0101000', 'cropName': 'મકાઈ (Maize)'}]
backend.gfr = [{'gfrType': 1, 'recommendations': [
    {'productGroupId': 11, 'offeredBags': 7, 'noOfBagsRequired': 7, 'kgPerBag': 45, 'alreadyPurchasedBags': 0},
    {'productGroupId': 12, 'offeredBags': 2, 'noOfBagsRequired': 2, 'kgPerBag': 50, 'alreadyPurchasedBags': 0}]}]

app = FastAPI()


def log(entry):
    with (HERE / 'calls.jsonl').open('a') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


@app.get('/health')
async def health():
    return {'mock': True, 'forwards': False, 'calls': len(backend.calls)}


@app.api_route('/{path:path}', methods=['GET', 'POST'])
async def handle(path: str, request: Request):
    body = await request.body()
    req = httpx.Request(request.method, 'http://ffs.mock/' + path, headers=dict(request.headers), content=body,
                        params=dict(request.query_params))
    before = len(backend.calls)
    try:
        if path.endswith('createMultipleBookingV2') and json.loads(body)['bookingRequest'][0]['retailerId'] == FAILING_SHOP:
            backend.calls.append(('/' + path, json.loads(body)))
            response = httpx.Response(200, json={'code': 200, 'status': 'success', 'data': {
                'isValid': False, 'message': 'Requested quantity is no longer available at this retailer',
                'bookingResults': [{'retailerId': FAILING_SHOP, 'isValid': False, 'statusCode': 409,
                                    'message': 'Stock changed; choose another retailer'}],
                'totalBookingsRequested': 1, 'totalBookingsSuccessful': 0, 'totalBookingsFailed': 1}})
        else:
            response = await backend.respond(req)
        decoded = backend.calls[before][1] if len(backend.calls) > before else None
        if path.endswith('/loginByFarmerId') and decoded and (
                decoded.get('userFarmerId') in UNREGISTERED or str(decoded.get('userStateLgdCode')) != str(STATE)):
            response = httpx.Response(400, json={'code': 400, 'status': 'failure', 'data': None,
                                                 'message': 'Farmer not found for this Farmer ID and state'})
        data = response.json()
        if path.endswith('getRetailersByProductAvailabilityV4') and isinstance(data.get('data'), list):
            for shop in data['data']:
                shop['retailerName'] = SHOPS[shop['retailerId'] % len(SHOPS)]
                shop['address1'] = 'Langhnaj, Mehsana'
        if path.endswith('createMultipleBookingV2') and isinstance(data.get('data'), dict):
            # The test backend's fixed expiry is in the past; a booking expires two days from now.
            expiry = time.strftime('%Y-%m-%d 23:00:00', time.localtime(time.time() + 2 * 86400))
            for booking in data['data'].get('bookingResults') or []:
                if booking.get('isValid'):
                    booking['expiryDatetime'] = expiry
        log(dict(time=time.time(), path='/' + path, status=response.status_code, request=decoded))
        return JSONResponse(data, status_code=response.status_code)
    except Exception as exc:  # noqa: BLE001 - a broken mock must be visible, never silently succeed
        log(dict(time=time.time(), path='/' + path, error=repr(exc)))
        return JSONResponse({'code': 500, 'message': f'mock error: {exc}'}, status_code=500)


if __name__ == '__main__':
    uvicorn.run(app, host='127.0.0.1', port=int(sys.argv[1]) if len(sys.argv) > 1 else 17920, log_level='warning')
