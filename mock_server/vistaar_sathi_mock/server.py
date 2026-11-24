#!/usr/bin/env python3
"""
SATHI Master API mock server (port 5692 by default).

Handles REST API endpoints that sathi_seed.py calls:
  GET /get-crop-group           — list crop groups (Cereals, Pulses, Oilseeds, etc.)
  GET /get-crops-list           — list crops in a group
  POST /search-dealers          — search seed dealers by crop, district, state

Environment (set in mock_server/.env or shell):
  SATHI_HOST         — bind host (default 127.0.0.1)
  SATHI_PORT         — bind port (default 5692)
  SATHI_REGISTRY     — path to common_registry.json (default: ../common_registry.json)
"""
from __future__ import annotations

import json
import os
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

_SERVER_DIR = Path(__file__).resolve().parent
_MOCK_SERVER_ROOT = _SERVER_DIR.parent

# Load registry
_REGISTRY_PATH = os.getenv("SATHI_REGISTRY", _MOCK_SERVER_ROOT / "common_registry.json")

def _load_registry() -> dict:
    try:
        with open(_REGISTRY_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        print(f"Failed to load registry: {e}", file=sys.stderr)
        return {"farmers": {}, "commodities": []}

# Crop groups and their crops
CROP_GROUPS = {
    "cereals": {
        "display_name": "Cereals",
        "crops": ["Rice", "Wheat", "Maize", "Bajra", "Jowar"]
    },
    "pulses": {
        "display_name": "Pulses",
        "crops": ["Chickpea", "Pigeon Pea", "Lentil", "Mung Bean", "Urad"]
    },
    "oilseeds": {
        "display_name": "Oilseeds",
        "crops": ["Soybean", "Sunflower", "Safflower", "Mustard", "Groundnut"]
    },
    "spices": {
        "display_name": "Spices",
        "crops": ["Turmeric", "Chilli", "Cumin", "Coriander", "Fenugreek"]
    },
    "cashcrops": {
        "display_name": "Cash Crops",
        "crops": ["Cotton", "Sugarcane", "Tobacco"]
    },
    "horticulture": {
        "display_name": "Horticulture",
        "crops": ["Tapioca", "Potato", "Onion", "Garlic", "Ginger"]
    }
}

# Seed dealers by district (mock data)
DEALERS_BY_DISTRICT = {
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
    ],
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
    ]
}

# -----------------------------------------------------------------------
# Response builders
# -----------------------------------------------------------------------

def _success_response(data: Any) -> dict:
    return {
        "status_code": 200,
        "message": "Success",
        "data": data
    }

def _error_response(status: int, message: str) -> dict:
    return {
        "status_code": status,
        "message": message,
        "data": []
    }

# -----------------------------------------------------------------------
# Handlers
# -----------------------------------------------------------------------

def handle_crop_groups() -> dict:
    """GET /get-crop-group — return list of crop groups."""
    groups = [
        {
            "group_code": code.upper(),
            "group_name": info["display_name"],
            "display_name": info["display_name"],
            "crops_count": len(info["crops"]),
            "is_active": 1
        }
        for code, info in CROP_GROUPS.items()
    ]
    return _success_response(groups)

def handle_crops_list(group_code: str) -> dict:
    """GET /get-crops-list?apiKey=X&group_code=Y — return crops in a group."""
    group = CROP_GROUPS.get(group_code.lower())
    if not group:
        return _error_response(404, f"Crop group '{group_code}' not found")

    crops = [
        {
            "crop_code": f"{group_code.upper()}-{crop.upper().replace(' ', '-')}",
            "crop_name": crop
        }
        for crop in group["crops"]
    ]
    return _success_response(crops)

def handle_search_dealers(params: dict[str, list[str]]) -> dict:
    """POST /search-dealers with crop_code, district, state."""
    crop = params.get("crop_code", [""])[0].strip()
    district = params.get("district", [""])[0].strip().lower()
    state = params.get("state", [""])[0].strip().lower()

    if not crop or not district:
        return _error_response(400, "crop_code and district are required")

    # Match dealers in this district
    dealers_list = DEALERS_BY_DISTRICT.get(district, [])
    if not dealers_list:
        return _success_response([])

    # Filter by state if provided
    if state:
        dealers_list = [d for d in dealers_list if d["state"].lower() == state]

    # Filter by crop if needed (mock: all dealers have all crops)
    result = [
        {
            "dealer_id": d["dealer_id"],
            "dealer_name": d["dealer_name"],
            "district": d["district"],
            "state": d["state"],
            "contact": d["contact"],
            "varieties": d["crops"]
        }
        for d in dealers_list
    ]

    return _success_response(result)

# -----------------------------------------------------------------------
# HTTP Handler
# -----------------------------------------------------------------------

class SATHIHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        """Handle GET requests."""
        parsed = urlparse(self.path)
        path = parsed.path.lower()
        params = parse_qs(parsed.query)

        if path == "/get-crop-group":
            response = handle_crop_groups()
        elif path == "/get-crops-list":
            response = handle_crops_list(params.get("group_code", [""])[0])
        elif path == "/health":
            self._send_json(200, {"ok": True, "service": "vistaar_sathi_mock"})
            return
        else:
            response = _error_response(404, f"Endpoint '{path}' not found")

        self._send_json(200, response)

    def do_POST(self):
        """Handle POST requests."""
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length).decode("utf-8")

        parsed = urlparse(self.path)
        path = parsed.path.lower()

        # Parse form data or JSON
        params = parse_qs(parsed.query)
        try:
            if body:
                body_data = json.loads(body)
                for k, v in body_data.items():
                    if k not in params:
                        params[k] = [v] if not isinstance(v, list) else v
        except json.JSONDecodeError:
            pass

        if path == "/search-dealers":
            response = handle_search_dealers(params)
        else:
            response = _error_response(404, f"Endpoint '{path}' not found")

        self._send_json(200, response)

    def _send_json(self, status: int, data: dict):
        """Send JSON response."""
        response_json = json.dumps(data, ensure_ascii=False)
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(response_json.encode("utf-8"))

    def log_message(self, format, *args):
        """Suppress default logging."""
        pass

# -----------------------------------------------------------------------
# Main
# -----------------------------------------------------------------------

def main():
    host = os.environ.get("SATHI_HOST", "127.0.0.1")
    port = int(os.environ.get("SATHI_PORT", "5692"))
    httpd = HTTPServer((host, port), SATHIHandler)
    print(f"vistaar_sathi_mock http://{host}:{port}", flush=True)
    print("  endpoints: /get-crop-group  /get-crops-list  /search-dealers", flush=True)
    httpd.serve_forever()

if __name__ == "__main__":
    main()
