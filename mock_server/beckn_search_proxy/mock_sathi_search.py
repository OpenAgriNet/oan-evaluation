"""
SATHI seed availability /search handler.
Extracts crop_code and location from intent, returns Beckn catalog with dealers.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


def build_sathi_search_response(body: dict) -> dict | None:
    """
    Build Beckn /search response for SATHI seed availability.

    Expects:
      message.intent.provider.id = "sathi-seed"
      message.intent.item.tags[] with crop_code value and location lat/lon

    Returns a Beckn response with catalog containing providers (dealers) and items (stock).
    """
    try:
        # Extract crop code from tags
        crop_code = None
        latitude = None
        longitude = None

        item = body.get("message", {}).get("intent", {}).get("item", {})
        tags = item.get("tags", [])

        for tag in tags:
            if isinstance(tag, dict):
                descriptor = tag.get("descriptor", {})
                if isinstance(descriptor, dict) and descriptor.get("code") == "crop_code":
                    crop_code = tag.get("value", "").strip().upper()
                elif isinstance(tag, dict) and "location" in tag:
                    location = tag.get("location", {})
                    latitude = float(location.get("lat", 0))
                    longitude = float(location.get("lon", 0))

        if not crop_code:
            return None

        # Map crop codes to districts (based on mock data in vistaar_sathi_mock/server.py)
        # For now, return dealers for Pune (Maharashtra) for Soybean and related crops
        dealers = _get_dealers_for_crop(crop_code)
        if not dealers:
            # Return empty response (no dealers found)
            return {
                "responses": [
                    {
                        "message": {
                            "catalog": {
                                "providers": []
                            }
                        }
                    }
                ]
            }

        # Build catalog response
        now = datetime.now(timezone.utc).isoformat() + "Z"

        providers = []
        for dealer in dealers:
            dealer_info = {
                "dealer_id": dealer["dealer_id"],
                "dealer_name": dealer["dealer_name"],
                "district": dealer["district"],
                "state": dealer["state"],
                "contact_number": dealer["contact"],
                "bags": 100,
                "quintals": 25.0
            }

            provider = {
                "id": dealer["dealer_id"],
                "descriptor": {
                    "name": dealer["dealer_name"],
                    "code": dealer["dealer_id"].lower()
                },
                "items": [
                    {
                        "id": f"{dealer['dealer_id']}_{crop_code}_1",
                        "descriptor": {
                            "name": crop_code,
                            "code": crop_code.lower()
                        },
                        "price": {"currency": "INR", "value": "0"},
                        "tags": [
                            {
                                "descriptor": {
                                    "code": "dealer-list",
                                    "name": "Available Dealers"
                                },
                                "list": [
                                    {
                                        "descriptor": {
                                            "code": dealer["dealer_id"],
                                            "name": dealer["dealer_name"]
                                        },
                                        "value": dealer_info
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
            providers.append(provider)

        catalog = {
            "bpp_descriptor": {
                "name": "SATHI Master Registry",
                "symbol": "https://example.com/sathi-logo.png"
            },
            "providers": providers,
            "tags": [
                {
                    "descriptor": {
                        "code": "search-context",
                        "name": "Search Context"
                    },
                    "list": [
                        {
                            "descriptor": {"code": "crop-code"},
                            "value": crop_code
                        },
                        {
                            "descriptor": {"code": "status"},
                            "value": "success"
                        }
                    ]
                }
            ]
        }

        return {
            "responses": [
                {
                    "context": {
                        "timestamp": now,
                        "action": "on_search"
                    },
                    "message": {
                        "catalog": catalog
                    }
                }
            ]
        }

    except Exception as e:
        import sys
        print(f"Error in build_sathi_search_response: {e}", file=sys.stderr)
        return None


def _get_dealers_for_crop(crop_code: str) -> list[dict[str, Any]]:
    """
    Return dealers that have this crop in stock.
    Based on mock data from vistaar_sathi_mock/server.py.
    """
    crop_code = (crop_code or "").strip().upper()

    # Extract the actual crop name from crop codes like "OILSEEDS-SOYBEAN"
    if "-" in crop_code:
        crop_code = crop_code.split("-", 1)[1]

    # All dealers from the mock server
    dealers_db = {
        "pune": [
            {
                "dealer_id": "MH-PUN-001",
                "dealer_name": "MSSC LTD PUNE",
                "district": "Pune",
                "state": "Maharashtra",
                "contact": "9000002001",
                "crops": ["Soybean", "Cotton", "Sunflower"]
            },
            {
                "dealer_id": "MH-PUN-002",
                "dealer_name": "SHRIBALRAM AGRO FARMERS PRODUCER COM",
                "district": "Pune",
                "state": "Maharashtra",
                "contact": "9000002003",
                "crops": ["Soybean", "Cotton", "Groundnut"]
            },
            {
                "dealer_id": "MH-PUN-003",
                "dealer_name": "Pratibha Farmers Producer Co.Ltd",
                "district": "Pune",
                "state": "Maharashtra",
                "contact": "9000002004",
                "crops": ["Soybean", "Sunflower", "Safflower"]
            }
        ],
        "ernakulam": [
            {
                "dealer_id": "KER-ERN-001",
                "dealer_name": "KSCB Ltd Kochi",
                "district": "Ernakulam",
                "state": "Kerala",
                "contact": "9000002002",
                "crops": ["Tapioca", "Cotton"]
            },
            {
                "dealer_id": "KER-ERN-002",
                "dealer_name": "Kerala Agro Products",
                "district": "Ernakulam",
                "state": "Kerala",
                "contact": "9000002005",
                "crops": ["Tapioca", "Cotton"]
            }
        ]
    }

    # Collect all matching dealers
    result = []
    for district_dealers in dealers_db.values():
        for dealer in district_dealers:
            dealer_crops = [c.upper() for c in dealer.get("crops", [])]
            if crop_code in dealer_crops:
                result.append(dealer)

    return result
