from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now_ts() -> str:
    now = datetime.now(timezone.utc)
    return now.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _echo_context(body: dict, action: str) -> dict:
    ctx = body.get("context", {})
    return {
        "domain": ctx.get("domain", "schemes:vistaar"),
        "action": action,
        "version": ctx.get("version", "1.1.0"),
        "bap_id": ctx.get("bap_id"),
        "bap_uri": ctx.get("bap_uri"),
        "bpp_id": ctx.get("bpp_id"),
        "bpp_uri": ctx.get("bpp_uri"),
        "transaction_id": ctx.get("transaction_id", str(uuid.uuid4())),
        "message_id": str(uuid.uuid4()),
        "timestamp": _now_ts(),
    }


def build_pmkisan_init_response(body: dict, registry_path: Path) -> dict:
    ctx = _echo_context(body, "on_init")
    response_item = {
        "context": ctx,
        "message": {
            "order": {
                "provider": {"id": "pmkisan-bpp"},
                "items": [
                    {
                        "id": "pmkisan-status",
                        "tags": [
                            {
                                "display": True,
                                "descriptor": {
                                    "short_desc": "OTP sent to registered mobile number."
                                },
                            }
                        ],
                    }
                ],
            }
        },
    }
    return {"context": ctx, "responses": [response_item]}


def build_pmkisan_status_response(body: dict, registry_path: Path) -> dict | None:
    with open(registry_path, "r", encoding="utf-8") as f:
        reg = json.load(f)

    otp = str(body.get("message", {}).get("order_id", ""))
    # Prod PM-KISAN OTPs are 4 digits; PMFBY's are 6. run_simulated_scenario.py
    # tells the user simulator to reply "111111", so accept both canonical test
    # OTPs rather than silently falling through to upstream on the 6-digit one.
    if not (otp.isdigit() and 4 <= len(otp) <= 6):
        return None

    farmers = reg.get("farmers", {})
    if not farmers:
        return None

    # Use the first farmer in the registry (Ravi Kumar / 9876543210)
    farmer_phone, farmer = next(iter(farmers.items()))
    pmkisan = farmer.get("pmkisan", {})

    reg_no = pmkisan.get("registration_number", "UNKNOWN")
    reg_date = pmkisan.get("registration_date", "")
    last_inst = pmkisan.get("last_installment_number", 0)
    ekyc = pmkisan.get("ekyc_status", "")
    location = pmkisan.get("location", "")

    # Same sections and wording as production's PM Kisan Portal response (~670 chars formatted).
    name = farmer.get("name", "Farmer")
    suffix = {1: "st", 2: "nd", 3: "rd"}.get(int(last_inst) % 10 if int(last_inst) % 100 not in (11, 12, 13) else 0, "th")
    long_desc = (
        "=== PM KISAN BENEFICIARY STATUS ===\n\n"
        f"Beneficiary Name - {name}\n"
        f"Beneficiary Location - {location}\n"
        f"Registration Number - {reg_no}\n"
        f"Registration Date - {reg_date} 10:42:17 AM\n"
        f"Last Installment Status - {last_inst}{suffix} Installment payment done\n"
        f"eKYC - {ekyc}\n\n"
        "=== PAYMENT STATUS & ISSUES ===\n\n"
        f"{name}, your {last_inst}{suffix} installment has been processed successfully. "
        f"Registration date: {reg_date}"
    )

    ctx = _echo_context(body, "on_status")
    ts = _now_ts()
    response_item = {
        "context": ctx,
        "message": {
            "order": {
                "id": otp,
                "state": "COMPLETED",
                "provider": {
                    "id": "pmkisan-bpp",
                    "descriptor": {"name": "PM Kisan Portal"},
                },
                "items": [
                    {
                        "id": "pmkisan-status",
                        "descriptor": {"name": "Beneficiary Status"},
                    }
                ],
                "fulfillments": [
                    {
                        "customer": {
                            "person": {"name": farmer.get("name", "Farmer")},
                            "contact": {"phone": farmer_phone},
                        },
                        "state": {
                            "descriptor": {
                                "name": "Status",
                                "short_desc": "OTP verified and user data retrieved",
                                "long_desc": long_desc,
                            },
                            "updated_at": ts,
                        },
                    }
                ],
            }
        },
    }
    return {"context": ctx, "responses": [response_item]}
