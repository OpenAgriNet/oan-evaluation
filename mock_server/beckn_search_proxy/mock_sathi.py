"""
SATHI (Seed Authentication, Traceability and Holistic Inventory) mock handler.
Returns certified seed dealers and availability for specified crops and locations.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

# Crop code mappings (SATHI standard codes)
CROP_CODES = {
    "tapioca": "A0410",
    "cotton": "A0502",
}

# Sample seed dealer data by district
DEALERS_BY_DISTRICT = {
    "ernakulam": [
        {
            "name": "KSCB Ltd Kochi",
            "district": "Ernakulam",
            "state": "Kerala",
            "contact": "9000002002",
            "crops": {
                "tapioca": ["Sree Athulya", "Sree Surya", "Sree Padma"],
                "cotton": ["DCH-32", "MCU-5", "AKA-8"],
            },
            "stock_bags": 850,
            "stock_quintals": 212.5,
        },
        {
            "name": "Kerala Agro Products",
            "district": "Ernakulam",
            "state": "Kerala",
            "contact": "9000002005",
            "crops": {
                "tapioca": ["Sree Athulya", "Sree Avni"],
                "cotton": ["MCU-5", "AKA-8", "BAJ-92"],
            },
            "stock_bags": 520,
            "stock_quintals": 130.0,
        },
        {
            "name": "Edappally Seed Store",
            "district": "Ernakulam",
            "state": "Kerala",
            "contact": None,
            "crops": {
                "tapioca": ["Sree Padma"],
                "cotton": ["DCH-32"],
            },
            "stock_bags": 340,
            "stock_quintals": 85.0,
        },
    ]
}


def build_sathi_response(body: dict, registry_path: Path) -> Optional[dict]:
    """
    Build Beckn on_init response for SATHI seed availability.
    Expects fulfillments with tags containing:
      - crop_code: crop identifier (e.g., "tapioca", "cotton")
      - district: district name (e.g., "ernakulam")
    Returns None if crop/district not found → proxy falls through.
    """
    try:
        fulfillments = body.get("message", {}).get("order", {}).get("fulfillments", [])
        if not fulfillments:
            return None

        # Extract crop and district from tags
        tags = fulfillments[0].get("customer", {}).get("person", {}).get("tags", [])

        crop_code = None
        district = None

        for tag in tags:
            descriptor = tag.get("descriptor", {})
            code = descriptor.get("code", "").lower()
            value = tag.get("value", "").lower().strip()

            if code == "crop":
                crop_code = value
            elif code == "district":
                district = value

        if not crop_code or not district:
            return None

        # Get dealers for this district and crop
        district_key = district.replace(" ", "").lower()
        dealers = DEALERS_BY_DISTRICT.get(district_key, [])

        if not dealers:
            return None

        # Filter dealers that have this crop
        relevant_dealers = []
        for dealer in dealers:
            if crop_code in dealer["crops"]:
                relevant_dealers.append(dealer)

        if not relevant_dealers:
            return None

        # Build dealer items
        dealer_items = []
        for dealer in relevant_dealers:
            varieties = dealer["crops"].get(crop_code, [])

            item = {
                "id": f"sathi-{dealer['name'].lower().replace(' ', '-')}",
                "descriptor": {
                    "name": dealer["name"],
                    "long_desc": f"District: {dealer['district']}, {dealer['state']}\n"
                                 f"Stock: {dealer['stock_bags']} bags ({dealer['stock_quintals']} quintals)\n"
                                 f"Varieties: {', '.join(varieties)}",
                },
                "tags": [
                    {
                        "descriptor": {"name": "Contact"},
                        "value": dealer["contact"] or "Visit directly",
                        "display": True,
                    },
                    {
                        "descriptor": {"name": "Stock"},
                        "value": f"{dealer['stock_bags']} bags ({dealer['stock_quintals']} Q)",
                        "display": True,
                    },
                    {
                        "descriptor": {"name": "Varieties"},
                        "value": ", ".join(varieties),
                        "display": True,
                    },
                ],
            }
            dealer_items.append(item)

        # Build context
        ctx = body.get("context", {})
        response_context = {
            "domain": ctx.get("domain", "agri-services:sathi"),
            "action": "on_init",
            "version": ctx.get("version", "1.1.0"),
            "bap_id": ctx.get("bap_id"),
            "bap_uri": ctx.get("bap_uri"),
            "bpp_id": ctx.get("bpp_id"),
            "bpp_uri": ctx.get("bpp_uri"),
            "transaction_id": ctx.get("transaction_id"),
            "message_id": ctx.get("message_id"),
            "timestamp": ctx.get("timestamp"),
        }

        response_item = {
            "context": response_context,
            "message": {
                "catalog": {
                    "descriptor": {
                        "name": f"SATHI Seed Dealers - {crop_code.capitalize()}",
                        "code": "sathi-seed-dealers",
                    },
                    "providers": [
                        {
                            "id": "sathi-dac",
                            "descriptor": {
                                "name": "SATHI - Department of Agriculture",
                                "short_desc": "Certified Seed Dealers",
                            },
                            "items": dealer_items,
                        }
                    ],
                }
            },
        }

        return {"context": response_context, "responses": [response_item]}

    except Exception:
        return None
