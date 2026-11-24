"""Standalone FFS fixture contracts; ASGI transport never opens a network socket."""
import base64
import json
import unittest
from unittest.mock import patch

from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import httpx

from mock_server.ffs_mock import mock_ffs
from mock_server.ffs_mock.backend import IV, KEY, USER_PATH


class FfsMockTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=mock_ffs.app), base_url='http://fixture')
        self.log = patch.object(mock_ffs, 'log').start()

    async def asyncTearDown(self):
        await self.client.aclose()
        patch.stopall()

    async def user_request(self, suffix, payload):
        encrypted = AES.new(KEY.encode(), AES.MODE_CBC, IV.encode()).encrypt(pad(json.dumps(payload).encode(), 16))
        return await self.client.post(USER_PATH + suffix, content=base64.b64encode(encrypted))

    async def test_health_and_invalid_identity(self):
        health = (await self.client.get('/health')).json()
        self.assertTrue(health['mock'])
        self.assertFalse(health['forwards'])
        for farmer, state in [('99000000009', 24), ('99000000001', 27)]:
            response = await self.user_request('/loginByFarmerId', {'userFarmerId': farmer, 'userStateLgdCode': state})
            self.assertEqual(response.status_code, 400)

    async def test_wrong_otp_and_synthetic_success(self):
        response = await self.user_request('/validateLoginByFarmerId', {'otp': '000000'})
        self.assertEqual(response.status_code, 400)
        response = await self.user_request('/validateLoginByFarmerId', {'otp': '246810'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['data']['userFullName'], 'Mock Farmer')

    async def test_complete_booking_and_stock_failure(self):
        body = dict(seasonId=2, year=2026, month=11, requestedDatetime='2026-11-24 10:00:00',
                    applicantUserId=42, contactNoForCommunication='9876543210', isReceiverApplicantSame=True,
                    receiverUserId=42, receiverName='Mock Farmer', receiverMobileNo='9876543210',
                    receiverAadharHash='test-hash', stateLgdCode=24, districtLgdCode=468,
                    subDistrictLgdCode=3719, villageLgdCode=518001,
                    selectedLandIds=[dict(farmLandOwnershipId='201', cropCode='0101100', farmLandPlotRegistryId='101')],
                    bookingRequest=[dict(retailerId=1, productLines=[dict(productGroupId=11, productId=1089,
                        plantId=99, isLooseBuy=False, noBagsRequested=1, quantityOfBagKg=45, totalQuantityKgRequested=45)])],
                    GFRRecommendation=[], gfrType=1, isProceedWithGfr=True)
        response = await self.client.post('/createMultipleBookingV2', json=body)
        self.assertTrue(response.json()['data']['isValid'])
        body['bookingRequest'][0]['retailerId'] = 0
        response = await self.client.post('/createMultipleBookingV2', json=body)
        self.assertFalse(response.json()['data']['isValid'])


if __name__ == '__main__':
    unittest.main()
