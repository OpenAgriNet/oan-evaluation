"""Synthetic FFS provider, extracted from bharat-oan-api tests/test_ffs.py at dc6f2dc.

No production data or network calls. Kept local so the eval package runs independently.
"""
import base64
from copy import deepcopy
import json
import re
import time
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad
import httpx

KEY, IV = "K" * 32, "I" * 16  # fixture-only AES values
USER_PATH = "/ufa-user-service/ufa-user/v1/api"

def validate_booking_payload(body):
    # Contract from the supplied Bruno v2 collection, not a permissive success stub.
    required = set("seasonId year month requestedDatetime applicantUserId contactNoForCommunication "
                   "isReceiverApplicantSame receiverUserId receiverName receiverMobileNo receiverAadharHash "
                   "stateLgdCode districtLgdCode subDistrictLgdCode villageLgdCode selectedLandIds "
                   "bookingRequest GFRRecommendation gfrType isProceedWithGfr".split())
    assert set(body) == required
    assert body["isReceiverApplicantSame"] is True and body["isProceedWithGfr"] is True
    assert body["receiverUserId"] == body["applicantUserId"]
    assert body["receiverMobileNo"] == body["contactNoForCommunication"]
    assert re.fullmatch(r"[6-9]\d{9}", body["contactNoForCommunication"])
    assert body["receiverName"] and body["receiverAadharHash"]
    assert body["selectedLandIds"]
    for land in body["selectedLandIds"]:
        assert set(land) == {"farmLandOwnershipId", "cropCode", "farmLandPlotRegistryId"}
        assert all(isinstance(x, str) and x for x in land.values())
    assert len(body["bookingRequest"]) == 1
    lines = body["bookingRequest"][0]["productLines"]
    assert lines
    for line in lines:
        assert set(line) == set("productGroupId productId plantId isLooseBuy noBagsRequested quantityOfBagKg totalQuantityKgRequested".split())
        assert line["isLooseBuy"] is False
        assert type(line["noBagsRequested"]) is int and line["noBagsRequested"] > 0
        assert line["totalQuantityKgRequested"] == line["noBagsRequested"] * line["quantityOfBagKg"]
        assert line["productId"] in {1089, 1005}

class Backend:
    def __init__(self):
        self.calls, self.failed, self.fail_path, self.fail_after_write = [], False, None, False
        self.request_headers = []
        self.stock = {}
        self.booking_payloads = []
        self.booking_error = None
        self.booking_http_status = 200
        self.booking_timeout = False
        self.booking_data_override = None
        self.expected_otp = "123456"
        self.token_lifetime = 900
        self.land = [{"farmlandPlotRegistryId": 101 + i, "farmlandOwnershipId": 201 + i,
                      "ownershipMappingId": 301 + i, "landAreaHectares": 1.23 if i == 0 else 0.96,
                      "stateLgdCode": 27, "districtLgdCode": 496, "subDistrictLgdCode": 4245,
                      "villageLgdCode": 561972, "villageName": "Mock Village", "surveyNumber": str(i + 1),
                      "subSurveyNumber": "A", "crops": [], "claimApprovalStatus": None,
                      "disputeStatus": "NoDispute"} for i in range(2)]
        self.crops = [{"cropCode": "0101900", "cropName": "गहू"}, {"cropCode": "0101000", "cropName": "मका"},
                      {"cropCode": "0801200", "cropName": "सोयाबीन"}]
        self.gfr = [{"gfrType": 1, "recommendations": [
            {"productGroupId": 11, "offeredBags": 6, "noOfBagsRequired": 7, "kgPerBag": 45, "alreadyPurchasedBags": 1},
            {"productGroupId": 12, "offeredBags": 2, "noOfBagsRequired": 3, "kgPerBag": 50, "alreadyPurchasedBags": 0}]}]

    def count(self, suffix):
        return sum(path.endswith(suffix) for path, _ in self.calls)

    async def respond(self, request):
        path = request.url.path
        body = json.loads(request.content) if request.content and not path.startswith(USER_PATH) else None
        if path.startswith(USER_PATH):
            body = json.loads(unpad(AES.new(KEY.encode(), AES.MODE_CBC, IV.encode()).decrypt(base64.b64decode(request.content)), 16))
        self.calls.append((path, deepcopy(body)))
        self.request_headers.append(dict(request.headers))
        if self.fail_path and path.endswith(self.fail_path) and not self.failed and not self.fail_after_write:
            self.failed = True
            raise httpx.ReadTimeout("test failure", request=request)
        meta = {}
        if path.endswith("/loginByFarmerId"):
            data = None
        elif path.endswith("/validateLoginByFarmerId"):
            if body["otp"] != self.expected_otp:
                return httpx.Response(400, json={"code": 400, "data": None})
            claims = base64.urlsafe_b64encode(json.dumps({"exp": time.time() + self.token_lifetime}).encode()).decode().rstrip("=")
            data = {"userToken": "test." + claims + ".signature", "userId": 42, "userAadhaarHash": "test-hash",
                    "userFullName": "Mock Farmer", "userFarmerId": "99999999999", "userStateLGDCode": 27,
                    "userMobileNumber": "9999999999"}
        elif path.endswith("getCurrentSeasonAndYear"):
            data = {"season": {"seasonId": 2, "seasonName": "Rabi"}, "year": {"yearId": 1, "yearValue": "2026"}}
        elif path.endswith("/bharatVistar/applicantFetch"):
            data = {"landData": deepcopy(self.land)}
        elif path.endswith("getCropListByState"):
            data = self.crops
        elif path.endswith("/land/crop/add"):
            land = next(l for l in self.land if l["farmlandPlotRegistryId"] == body["farmlandPlotRegistryId"])
            data = {"farmlandPlotRegistryId": land["farmlandPlotRegistryId"], "cropCode": body["cropCode"],
                    "seasonId": body["seasonId"], "yearId": body["yearId"]}
            land["crops"] = [data]
            if self.fail_after_write and not self.failed and land["farmlandPlotRegistryId"] == 102:
                self.failed = True
                raise httpx.ReadTimeout("response lost after write", request=request)
        elif path.endswith("getApplicantDashboardQuota"):
            data = []
        elif path.endswith("getRecommendationsByGFRV4"):
            data = self.gfr
        elif path.endswith("getProductsByAvailability"):
            data = [{"productId": 1089, "productName": "Neem Coated Urea", "productGroupId": 11, "plantId": 99, "bagWeightKg": 45},
                    {"productId": 1005, "productName": "DAP", "productGroupId": 12, "plantId": 99, "bagWeightKg": 50}]
        elif path.endswith("getRetailersByProductAvailabilityV4"):
            limit = {"V": 2, "T": 6, "D": 8, "S": 8}[body["territoryLevel"]]
            data = [{"retailerId": i, "retailerName": f"Shop {i}", "address1": "Mock address", "availableProducts": [
                {"products": [{"productId": 1089, "noBagsAvailable": self.stock.get(i, (10 + i, 10 + i))[0]}]},
                {"products": [{"productId": 1005, "noBagsAvailable": self.stock.get(i, (10 + i, 10 + i))[1]}]}]} for i in range(limit)]
            meta = {"totalPages": 1, "totalRecords": limit}
        elif path.endswith("createMultipleBookingV2"):
            self.booking_payloads.append(deepcopy(body))
            validate_booking_payload(body)
            if self.booking_error:
                return httpx.Response(self.booking_http_status, json=self.booking_error)
            if self.booking_timeout:
                raise httpx.ReadTimeout("Booking accepted but response lost", request=request)
            data = {
                "isValid": True,
                "message": "All 1 booking(s) created successfully",
                "bookingResults": [{
                    "retailerId": body["bookingRequest"][0]["retailerId"],
                    "isValid": True,
                    "statusCode": 304,
                    "expiryDatetime": "2026-11-06 23:00:00",
                    "procurementId": 123456,
                    "procurementApplicationCode": "PRC-TEST-001",
                    "isWaitlisted": False,
                }],
                "totalBookingsRequested": 1,
                "totalBookingsSuccessful": 1,
                "totalBookingsFailed": 0,
                "applicantUserId": body["applicantUserId"],
            }
            if self.booking_data_override is not None:
                data = deepcopy(self.booking_data_override)
        else:
            raise AssertionError("Unexpected API: " + path)
        return httpx.Response(200, json={"code": 200, "status": "success", "data": data, **meta})
