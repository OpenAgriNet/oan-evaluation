"""PM-KISAN grievance mocks (Beckn), for the tools added on bh-voice-prod 2026-09-16.

`agents/tools/pmkisan_grievance.py` replaced the old `grievance.py` (which used
GRIEVANCE_BASE_URL and the standalone vistaar_grievance_mock) with three Beckn
tools routed through BAP_ENDPOINT:

    pmkisan_grievance_send_otp  -> /init   provider "pmkisan"            (served by mock_pmkisan)
    pmkisan_submit_grievance    -> /init   provider "pmkisan-greviance"  (here; provider typo is upstream's)
    pmkisan_grievance_status    -> /search category name "grievance-agri" (here)

Response shape mirrors what the tool's own model parses: top-level context plus
responses[].message.order.tags[].list[] of {descriptor.code, value, display}.
Tag codes consumed by its __str__: status, grievance-id, lookup-type, identity-no,
grievance-type, grievance-status, grievance-date, message, details.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

_STATUS_TICKET_PREFIX = "PMKGRV"


def _load_registry(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _echo_context(body: dict, action: str) -> Dict[str, Any]:
    src = body.get("context") or {}
    return {
        "domain": src.get("domain", "schemes:vistaar"),
        "action": action,
        "version": src.get("version", "1.1.0"),
        "bap_id": src.get("bap_id"),
        "bap_uri": src.get("bap_uri"),
        "bpp_id": src.get("bpp_id"),
        "bpp_uri": src.get("bpp_uri"),
        "transaction_id": src.get("transaction_id") or str(uuid.uuid4()),
        "message_id": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }


def _tag_list(pairs: List[tuple]) -> Dict[str, Any]:
    return {
        "descriptor": {"code": "grievance-response"},
        "display": True,
        "list": [
            {"descriptor": {"code": code}, "value": str(value), "display": True}
            for code, value in pairs
            if value is not None
        ],
    }


def _wrap(ctx: Dict[str, Any], pairs: List[tuple]) -> Dict[str, Any]:
    return {
        "context": ctx,
        "responses": [
            {"context": ctx, "message": {"order": {"tags": [_tag_list(pairs)]}}}
        ],
    }


def _first_farmer(reg: dict) -> tuple[str, dict]:
    farmers = reg.get("farmers", {})
    return next(iter(farmers.items()), ("", {}))


def _find_by_reg_no(reg: dict, identity: str) -> tuple[str, dict]:
    """Registry farmer whose PM-KISAN reg number matches; else the default farmer."""
    for phone, farmer in (reg.get("farmers") or {}).items():
        pmkisan = farmer.get("pmkisan") or {}
        if identity and pmkisan.get("registration_number", "").upper() == identity.upper():
            return phone, farmer
    return _first_farmer(reg)


def _identity_from_order(body: dict) -> str:
    """Pull the registration number / identity out of an /init order payload."""
    order = ((body.get("message") or {}).get("order") or {})
    for f in order.get("fulfillments") or []:
        cust = (f or {}).get("customer") or {}
        person = cust.get("person") or {}
        for tag in person.get("tags") or []:
            code = ((tag.get("descriptor") or {}).get("code") or "").lower()
            if code in ("identity-no", "identity_no", "reg-no", "reg_no", "registration-number"):
                return str(tag.get("value") or "")
        for group in (f or {}).get("tags") or []:
            for item in group.get("list") or []:
                code = ((item.get("descriptor") or {}).get("code") or "").lower()
                if code in ("identity-no", "identity_no", "reg-no", "reg_no"):
                    return str(item.get("value") or "")
    return ""


def _grievance_type_from_order(body: dict) -> str:
    order = ((body.get("message") or {}).get("order") or {})
    for f in order.get("fulfillments") or []:
        for group in (f or {}).get("tags") or []:
            for item in group.get("list") or []:
                code = ((item.get("descriptor") or {}).get("code") or "").lower()
                if code in ("grievance-type", "grievance_type"):
                    return str(item.get("value") or "")
    return "G003"


def _identity_from_search(body: dict) -> str:
    """Pull the identity out of a /search intent payload (grievance status lookup)."""
    intent = ((body.get("message") or {}).get("intent") or {})
    blob = json.dumps(intent, ensure_ascii=False)
    fulfillment = intent.get("fulfillment") or {}
    for f in ([fulfillment] if isinstance(fulfillment, dict) else fulfillment) or []:
        cust = (f or {}).get("customer") or {}
        person = cust.get("person") or {}
        for tag in person.get("tags") or []:
            code = ((tag.get("descriptor") or {}).get("code") or "").lower()
            if code in ("identity-no", "identity_no", "reg-no", "reg_no"):
                return str(tag.get("value") or "")
    for item in intent.get("tags") or []:
        for li in item.get("list") or []:
            code = ((li.get("descriptor") or {}).get("code") or "").lower()
            if code in ("identity-no", "identity_no", "reg-no", "reg_no"):
                return str(li.get("value") or "")
    # last resort: any registry reg-number appearing anywhere in the intent
    return "" if "KL" not in blob else ""


def build_pmkisan_grievance_submit_response(body: dict, registry_path: Path) -> dict | None:
    """/init provider `pmkisan-greviance` — register a grievance and return its id."""
    reg = _load_registry(registry_path)
    identity = _identity_from_order(body)
    _, farmer = _find_by_reg_no(reg, identity)
    if not farmer:
        return None
    identity = identity or (farmer.get("pmkisan") or {}).get("registration_number", "")
    ctx = _echo_context(body, "on_init")
    grievance_id = f"{_STATUS_TICKET_PREFIX}{uuid.uuid4().hex[:8].upper()}"
    return _wrap(ctx, [
        ("status", "Success"),
        ("grievance-id", grievance_id),
        ("identity-no", identity),
        ("grievance-type", _grievance_type_from_order(body)),
        ("message", "Grievance registered successfully. You will be notified once it is reviewed."),
    ])


_HISTORY_RECORDS = 8
# Made-up older grievances in production's style (no real farmer data).
_OLDER_GRIEVANCES = [
    {"grievance_date": "2026-03-14T10:22:41.53", "description": "किस्त नहीं मिली",
     "officer_reply": "Aap apne bank account ko Aadhaar se link karwaye", "office_reply_date": "2026-03-29T16:05:12.48"},
    {"grievance_date": "2025-12-02T09:47:18.2", "description": "किस्त प्राप्त नहीं हुई है, कृपया अपडेट बताएं कि किस्त कहां फंसी हुई है।",
     "officer_reply": "Farmer ID banwaye aur biometric eKYC karwaye", "office_reply_date": "2025-12-20T11:31:54.117"},
    {"grievance_date": "2025-08-19T14:03:27.9", "description": "Respected Sir/Madam, I am a registered farmer under this scheme. I have not received my last installment yet. My Aadhaar seeding and eKYC have been completed. Please check my payment status and release the pending payment to my registered bank account.",
     "officer_reply": "Payment failed due to inactive account. Please activate your bank account and update details.", "office_reply_date": "2025-09-04T12:44:06.301"},
    {"grievance_date": "2025-05-07T11:16:52.07", "description": "eKYC complete hone ke baad bhi status pending dikha raha hai",
     "officer_reply": "eKYC verified. Record forwarded to state nodal officer for approval.", "office_reply_date": "2025-05-23T15:20:39.66"},
    {"grievance_date": "2025-01-28T08:55:03.41", "description": "Land seeding status No dikh raha hai, kripya sudhar karein",
     "officer_reply": "Please contact your Circle Officer for land record verification and seeding.", "office_reply_date": "2025-02-15T10:08:17.5"},
    {"grievance_date": "2024-10-11T13:39:44.88", "description": "नाम में गलती है, आधार के अनुसार सुधार करना है",
     "officer_reply": "Name corrected as per Aadhaar. Please wait for the next installment.", "office_reply_date": "2024-10-30T17:12:25.903"},
    {"grievance_date": "2024-06-03T16:21:09.15", "description": "Bank account number change karna hai, purana account band ho gaya hai",
     "officer_reply": "Please wait", "office_reply_date": "2024-06-21T09:36:48.2"},
]


def build_pmkisan_grievance_status_response(body: dict, registry_path: Path) -> dict | None:
    """/search category `grievance-agri` — return the farmer's grievance history."""
    reg = _load_registry(registry_path)
    identity = _identity_from_search(body)
    _, farmer = _find_by_reg_no(reg, identity)
    if not farmer:
        return None
    pmkisan = farmer.get("pmkisan") or {}
    identity = identity or pmkisan.get("registration_number", "")
    grievances = farmer.get("grievances") or []
    if not grievances:
        return None

    records = []
    # Production returns the farmer's whole grievance history (mean 6.7 records, ~3.8k chars
    # formatted, all fields below). The registry's grievances come first, then a synthetic
    # older history so the mock is at least production's average size.
    for g in [*grievances, *_OLDER_GRIEVANCES][:_HISTORY_RECORDS]:
        records.append({
            "Farmer_Name": farmer.get("name", ""),
            "Father_Name": farmer.get("father_name", "Ramesh Kumar"),
            "Gender": farmer.get("gender", "M"),
            "Reg_No": g.get("registration_number") or identity,
            "StateName": farmer.get("state", ""),
            "DistrictName": farmer.get("district", ""),
            "BlockName": farmer.get("block", farmer.get("district", "")),
            "RevenueVillageName": farmer.get("village", ""),
            "GrievanceDate": g.get("grievance_date", ""),
            "GrievanceStatus": "Resolved" if g.get("officer_reply") else "Under Process",
            "GrievanceDescription": g.get("description", ""),
            "OfficerReply": g.get("officer_reply") or "",
            "OfficeReplyDate": g.get("office_reply_date") or "",
            "MobileNo": farmer.get("normalized_phone", ""),
        })

    latest = grievances[0]
    ctx = _echo_context(body, "on_search")
    return _wrap(ctx, [
        ("status", "Success"),
        ("lookup-type", "Reg_No_Status"),
        ("identity-no", identity),
        ("grievance-status", "Resolved" if latest.get("officer_reply") else "Under Process"),
        ("grievance-date", latest.get("grievance_date", "")),
        ("details", json.dumps(records, ensure_ascii=False)),
    ])
