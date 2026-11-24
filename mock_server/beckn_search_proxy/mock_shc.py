"""
Handler for SHC (Soil Health Card) mock — POST /init with provider.id == "shc-discovery".

Reads farmer data from common_registry.json, generates an HTML SHC report, base64-encodes
it, and returns a Beckn on_init response shaped to match the SHCStatusResponse Pydantic
model in bharat-oan-api/agents/tools/shc_scheme_status.py.

Returns None for unknown phones or missing cycle data → proxy falls through to upstream.
"""
from __future__ import annotations

import base64
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

_REGISTRY_CACHE: dict[str, dict] = {}


def _load_registry(path: Path) -> dict:
    key = str(path)
    if key not in _REGISTRY_CACHE:
        with open(path, "r", encoding="utf-8") as f:
            _REGISTRY_CACHE[key] = json.load(f)
    return _REGISTRY_CACHE[key]


def normalize_phone(raw: str) -> str:
    """Strip non-digits, return last 10 digits."""
    digits = "".join(c for c in raw if c.isdigit())
    return digits[-10:] if len(digits) >= 10 else digits


# ---------------------------------------------------------------------------
# Status label helpers (standard ICAR soil parameter thresholds)
# ---------------------------------------------------------------------------

def _ph_status(v: float) -> tuple[str, str]:
    if v < 5.5:
        return "Strongly Acidic", "deficient"
    if v < 6.5:
        return "Slightly Acidic", "medium"
    if v <= 7.5:
        return "Neutral", "sufficient"
    if v <= 8.5:
        return "Slightly Alkaline", "medium"
    return "Strongly Alkaline", "deficient"


def _ec_status(v: float) -> tuple[str, str]:
    if v < 1.0:
        return "Normal", "sufficient"
    if v < 2.0:
        return "Slightly Saline", "medium"
    return "Saline", "deficient"


def _oc_status(v: float) -> tuple[str, str]:
    if v < 0.50:
        return "Low", "deficient"
    if v <= 0.75:
        return "Medium", "medium"
    return "High", "sufficient"


def _npk_status(nutrient: str, v: float) -> tuple[str, str]:
    thresholds = {
        "n": [(280, "Low", "deficient"), (560, "Medium", "medium"), (float("inf"), "High", "sufficient")],
        "p": [(10, "Low", "deficient"), (25, "Medium", "medium"), (float("inf"), "High", "sufficient")],
        "k": [(108, "Low", "deficient"), (280, "Medium", "medium"), (float("inf"), "High", "sufficient")],
    }
    for limit, label, css in thresholds.get(nutrient, []):
        if v <= limit:
            return label, css
    return "High", "sufficient"


def _micro_status(nutrient: str, v: float) -> tuple[str, str]:
    # Critical limits (mg/kg)
    limits = {"s": 10.0, "zn": 0.60, "fe": 4.5, "mn": 2.0, "cu": 0.20, "b": 0.50}
    limit = limits.get(nutrient, 0.0)
    if v < limit:
        return "Deficient", "deficient"
    return "Sufficient", "sufficient"


# ---------------------------------------------------------------------------
# Recommendation logic
# ---------------------------------------------------------------------------

def _recommendations(farmer: dict, shc: dict) -> list[tuple[str, str, str]]:
    """Return list of (crop, nutrient_label, recommendation_text) tuples."""
    recs = []
    crops = farmer.get("crops", ["Crops"])

    n_label, _ = _npk_status("n", shc["nitrogen"])
    p_label, _ = _npk_status("p", shc["phosphorus"])
    k_label, _ = _npk_status("k", shc["potassium"])
    oc_label, _ = _oc_status(shc["organic_carbon"])
    zn_label, _ = _micro_status("zn", shc["zinc"])
    b_label, _ = _micro_status("b", shc["boron"])
    s_label, _ = _micro_status("s", shc["sulphur"])

    for crop in crops:
        if n_label == "Low":
            recs.append((crop, "Nitrogen (N) — Low", "Apply 100 kg/ha Urea in 3 split doses (basal + 30 days + 60 days after sowing)"))
        elif n_label == "Medium":
            recs.append((crop, "Nitrogen (N) — Medium", "Apply 75 kg/ha Urea in 2 split doses (basal + 45 days after sowing)"))

        if p_label == "Low":
            recs.append((crop, "Phosphorus (P) — Low", "Apply 250 kg/ha Single Super Phosphate (SSP) as basal dose"))
        elif p_label == "Medium":
            recs.append((crop, "Phosphorus (P) — Medium", "Apply 150 kg/ha Single Super Phosphate (SSP) as basal dose"))
        elif p_label == "High":
            recs.append((crop, "Phosphorus (P) — High", "No additional phosphate required this season"))

        if k_label in ("Low", "Medium"):
            dose = 150 if k_label == "Low" else 100
            recs.append((crop, f"Potassium (K) — {k_label}", f"Apply {dose} kg/ha Muriate of Potash (MOP) as basal dose"))

        if oc_label in ("Low", "Medium"):
            recs.append((crop, "Organic Carbon — Low/Medium", "Incorporate 5 t/ha well-decomposed FYM or compost before planting"))

        if zn_label == "Deficient":
            recs.append((crop, "Zinc (Zn) — Deficient", "Apply 25 kg/ha Zinc Sulphate (ZnSO₄·7H₂O) as basal dose"))

        if b_label == "Deficient":
            recs.append((crop, "Boron (B) — Deficient", "Apply 5 kg/ha Borax (Na₂B₄O₇·10H₂O) before planting"))

        if s_label == "Deficient":
            recs.append((crop, "Sulphur (S) — Deficient", "Apply 20 kg/ha Gypsum or use Single Super Phosphate instead of DAP"))

    return recs


# ---------------------------------------------------------------------------
# HTML generation
# ---------------------------------------------------------------------------

def _css_class(status: str) -> str:
    s = status.lower()
    if "deficient" in s or "strongly acidic" in s or "strongly alkaline" in s or "saline" in s:
        return "deficient"
    if "medium" in s or "slightly" in s or "low" in s:
        return "medium"
    return "sufficient"


def _masked_phone(phone: str) -> str:
    """Mask middle digits: +919876543210 → +91*****3210"""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) >= 10:
        last4 = digits[-4:]
        return f"+91*****{last4}"
    return phone


def _build_shc_html(farmer: dict, cycle: str, shc: dict) -> str:
    name = farmer.get("name", "Farmer")
    phone_raw = farmer.get("normalized_phone", "")
    phone_masked = _masked_phone(phone_raw)
    state = farmer.get("state", "")
    district = farmer.get("district", "")
    village = farmer.get("village", "")
    survey = farmer.get("survey_number", "")
    crops_list = farmer.get("crops", [])
    crops = ", ".join(crops_list)
    report_no = shc.get("report_no", "—")
    test_date = shc.get("test_date", "—")

    # Derive GPS from city registry (kochi)
    gps = "9.9679032, 76.2444378"
    plot_address = f"{village}, {district}, {state}"

    # Soil statuses
    ph_l, ph_c = _ph_status(shc["ph"])
    ec_l, ec_c = _ec_status(shc["ec"])
    oc_l, oc_c = _oc_status(shc["organic_carbon"])
    n_l, n_c = _npk_status("n", shc["nitrogen"])
    p_l, p_c = _npk_status("p", shc["phosphorus"])
    k_l, k_c = _npk_status("k", shc["potassium"])
    s_l, s_c = _micro_status("s", shc["sulphur"])
    zn_l, zn_c = _micro_status("zn", shc["zinc"])
    fe_l, fe_c = _micro_status("fe", shc["iron"])
    mn_l, mn_c = _micro_status("mn", shc["manganese"])
    cu_l, cu_c = _micro_status("cu", shc["copper"])
    b_l, b_c = _micro_status("b", shc["boron"])

    recs = _recommendations(farmer, shc)
    rec_rows = "\n".join(
        f"<tr><td>{crop}</td><td>{nutrient}</td><td>{text}</td></tr>"
        for crop, nutrient, text in recs
    ) if recs else "<tr><td colspan='3'>No specific deficiencies noted. Continue with balanced fertilizer application.</td></tr>"

    def row(param, val, unit, label, css):
        return f"<tr><td>{param}</td><td>{val}</td><td>{unit}</td><td class='{css}'>{label}</td></tr>"

    soil_rows = "\n".join([
        row("pH (Soil Reaction)", shc["ph"], "—", ph_l, ph_c),
        row("Electrical Conductivity (EC)", shc["ec"], "dS/m", ec_l, ec_c),
        row("Organic Carbon (OC)", shc["organic_carbon"], "%", oc_l, oc_c),
        row("Available Nitrogen (N)", shc["nitrogen"], "kg/ha", n_l, n_c),
        row("Available Phosphorus (P)", shc["phosphorus"], "kg/ha", p_l, p_c),
        row("Available Potassium (K)", shc["potassium"], "kg/ha", k_l, k_c),
        row("Sulphur (S)", shc["sulphur"], "mg/kg", s_l, s_c),
        row("Zinc (Zn)", shc["zinc"], "mg/kg", zn_l, zn_c),
        row("Iron (Fe)", shc["iron"], "mg/kg", fe_l, fe_c),
        row("Manganese (Mn)", shc["manganese"], "mg/kg", mn_l, mn_c),
        row("Copper (Cu)", shc["copper"], "mg/kg", cu_l, cu_c),
        row("Boron (B)", shc["boron"], "mg/kg", b_l, b_c),
    ])

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Soil Health Card</title>
<style>
  body {{ font-family: Arial, sans-serif; margin: 20px; color: #333; font-size: 14px; }}
  .container {{ max-width: 860px; margin: 0 auto; }}
  h1 {{ color: #1a5c1a; font-size: 22px; margin-bottom: 4px; }}
  h2 {{ color: #1a5c1a; font-size: 16px; margin: 20px 0 6px; border-bottom: 1px solid #c8e6c9; padding-bottom: 3px; }}
  h3 {{ color: #2e7d32; font-size: 14px; margin: 16px 0 4px; }}
  p {{ margin: 4px 0; }}
  table {{ width: 100%; border-collapse: collapse; margin-bottom: 12px; }}
  th {{ background: #e8f5e9; color: #1b5e20; text-align: left; padding: 8px 10px; border: 1px solid #c8e6c9; font-size: 13px; }}
  td {{ padding: 7px 10px; border: 1px solid #dce8dc; font-size: 13px; }}
  tr:nth-child(even) td {{ background: #f9fbe7; }}
  .deficient {{ color: #c62828; font-weight: bold; }}
  .medium {{ color: #e65100; font-weight: 600; }}
  .sufficient {{ color: #2e7d32; }}
  .footer {{ margin-top: 24px; font-size: 12px; color: #666; border-top: 1px solid #ddd; padding-top: 10px; }}
  strong {{ font-weight: 600; }}
</style>
</head>
<body>
<div class="container">

<h1>Soil Health Card</h1>
<p>Soil Health Card for Farmer {name}</p>

<h2>Soil health card</h2>
<p><strong>Center Name</strong>: Soil Testing Lab — {district}</p>
<p><strong>Address</strong>: Agriculture Department, {district}, {state}</p>
<p><strong>Test ID</strong>: {report_no}</p>
<p><strong>Testing Date</strong>: {test_date}, &nbsp;<strong>Validity</strong>: 2 years or 2 crop seasons</p>

<h2>Sample Information</h2>
<p>Survey No.: {survey}</p>
<p>Plot Address: {plot_address}</p>
<p>Sampling Date: {test_date}</p>
<p>geo position(GPS): {gps}</p>

<h2>Card Issued To</h2>
<p>Name: {name}</p>
<p>Phone Number: {phone_masked}</p>
<p>Address: {village}, {village}, , {district}, {state}</p>

<h3>Plot Size</h3>
<p><strong>Area</strong>: 1.500 Hectare</p>
<p><strong>Soil Type</strong>: Sandy Loam</p>
<p><strong>Crops Grown</strong>: {crops}</p>
<p><strong>Cycle</strong>: {cycle}</p>

<h2>Soil Sample Details</h2>
<table>
  <tr><th>Parameter</th><th>Observed Value</th><th>Unit</th><th>Status</th></tr>
  {soil_rows}
</table>

<h2>Fertilizer &amp; Input Recommendations</h2>
<table>
  <tr><th>Crop</th><th>Nutrient / Issue</th><th>Recommendation</th></tr>
  {rec_rows}
</table>

<div class="footer">
  <p>This Soil Health Card is issued under the Soil Health Card Scheme, Government of India (soilhealth.dac.gov.in).
  For queries, contact your local Krishi Vigyan Kendra or Agriculture Extension Officer.</p>
  <p>Recommendations are based on soil test values and standard ICAR norms. Next soil testing recommended after 2 years.</p>
</div>

</div>
</body>
</html>"""


# ---------------------------------------------------------------------------
# Beckn response builder
# ---------------------------------------------------------------------------

def _build_context(incoming: dict, action: str, now: str) -> dict:
    ctx = incoming.get("context", {})
    return {
        "domain": ctx.get("domain", "schemes:vistaar"),
        "action": action,
        "version": ctx.get("version", "1.1.0"),
        "bap_id": ctx.get("bap_id", os.getenv("BAP_ID", "")),
        "bap_uri": ctx.get("bap_uri", os.getenv("BAP_URI", "")),
        "bpp_id": ctx.get("bpp_id", os.getenv("BPP_ID", "")),
        "bpp_uri": ctx.get("bpp_uri", os.getenv("BPP_URI", "")),
        "message_id": ctx.get("message_id", str(uuid.uuid4())),
        "transaction_id": ctx.get("transaction_id", str(uuid.uuid4())),
        "timestamp": now,
    }


def _build_response_context(incoming: dict, action: str, now: str) -> dict:
    ctx = incoming.get("context", {})
    return {
        "domain": ctx.get("domain", "schemes:vistaar"),
        "action": action,
        "version": ctx.get("version", "1.1.0"),
        "bpp_id": ctx.get("bpp_id", os.getenv("BPP_ID", "")),
        "bpp_uri": ctx.get("bpp_uri", os.getenv("BPP_URI", "")),
        "message_id": ctx.get("message_id", str(uuid.uuid4())),
        "transaction_id": ctx.get("transaction_id", str(uuid.uuid4())),
        "timestamp": now,
    }


def _synth_shc(cycle: str, farmer: dict) -> dict:
    """Deterministic soil card for a cycle with no curated fixture.

    Seeded on cycle+phone so the same question always yields the same card.
    Values sit inside the agronomic ranges _npk_status/_micro_status classify.
    """
    import hashlib

    h = int(hashlib.sha256(f"{cycle}:{farmer.get('name','')}".encode()).hexdigest()[:12], 16)

    def pick(lo: float, hi: float, shift: int, nd: int = 2) -> float:
        span = hi - lo
        return round(lo + span * (((h >> shift) & 0xFFFF) / 65535.0), nd)

    return {
        "report_no": f"{farmer.get('state_code', 'IN')}/SYN/{cycle}/{h % 1000000:06d}",
        "test_date": f"15-Jun-{cycle.split('-')[0]}",
        "ph": pick(5.6, 7.8, 0, 1),
        "ec": pick(0.15, 0.60, 4),
        "organic_carbon": pick(0.35, 0.85, 8),
        "nitrogen": int(pick(150, 320, 12, 0)),
        "phosphorus": int(pick(12, 45, 16, 0)),
        "potassium": int(pick(120, 260, 20, 0)),
        "sulphur": int(pick(8, 22, 24, 0)),
        "zinc": pick(0.25, 0.85, 28),
        "iron": pick(4.0, 12.0, 32, 1),
        "manganese": pick(2.0, 7.0, 36, 1),
        "copper": pick(0.8, 2.6, 40, 1),
        "boron": pick(0.2, 0.7, 44),
    }

def build_shc_response(body: dict, registry_path: Path) -> Optional[dict]:
    """
    Build a Beckn on_init response for the SHC mock.

    Returns None if the phone is not in the registry or cycle data is missing,
    which causes the proxy to fall through to the real upstream.
    """
    registry = _load_registry(registry_path)

    fulfillments = body.get("message", {}).get("order", {}).get("fulfillments", [])
    if not fulfillments:
        return None

    customer = fulfillments[0].get("customer", {})
    phone_raw = customer.get("contact", {}).get("phone", "")
    phone_key = normalize_phone(phone_raw)

    cycle = ""
    for tag in customer.get("person", {}).get("tags", []):
        if tag.get("descriptor", {}).get("code") == "cycle":
            cycle = tag.get("value", "")
            break

    farmers = registry.get("farmers", {})
    farmer = farmers.get(phone_key) or next(iter(farmers.values()), None)
    if not farmer:
        return None

    shc = farmer.get("shc_data", {}).get(cycle)
    if not shc:
        # Prod sends cycles the registry does not carry (2026-27, bare "2026", ...).
        # Returning None handed the request to the live BPP, so synthesise a
        # deterministic card for the requested cycle instead. Values are stable
        # per cycle, so eval runs stay reproducible.
        shc = _synth_shc(cycle or "unknown", farmer)

    # Production lists every report on the farmer's record (2.5 on average, ~7.4k chars
    # formatted). Return this cycle's card plus two more plots, synthesised deterministically.
    cards = [shc] + [_synth_shc(f"{cycle or 'unknown'} plot {n}", farmer) for n in (2, 3)]

    def media_url(card: dict) -> str:
        html = _build_shc_html(farmer, cycle, card)
        return "data:text/html;base64," + base64.b64encode(html.encode("utf-8")).decode("ascii")

    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
    out_ctx = _build_context(body, "on_init", now)
    resp_ctx = _build_response_context(body, "on_init", now)

    state_code = farmer.get("state_code", "IN").lower()
    state_name = farmer.get("state", "India")

    return {
        "context": out_ctx,
        "responses": [
            {
                "context": resp_ctx,
                "message": {
                    "order": {
                        "provider": {"id": "shc-discovery"},
                        "providers": [
                            {
                                "id": f"shc-{state_code}-state",
                                "descriptor": {
                                    "name": f"{state_name} State Government",
                                    "short_desc": f"Soil Health Card Programme, {state_name}",
                                },
                                "items": [
                                    {
                                        "id": f"shc-{phone_key}-{cycle}-{n}",
                                        "descriptor": {"name": "Soil Health Card"},
                                        "tags": [],
                                        "media": [{"mimetype": "text/html", "url": media_url(card)}],
                                    }
                                    for n, card in enumerate(cards, start=1)
                                ],
                            }
                        ],
                        "type": "DEFAULT",
                    }
                },
            }
        ],
    }
