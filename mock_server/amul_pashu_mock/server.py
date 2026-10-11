#!/usr/bin/env python3
"""Local stand-in for the original Amul agent's farmer reads and Beckn confirm.

The agent code stays Amul Agent Dev with network booking left on. An eval-only
process points its farmer and booking URLs here. Reads return one fixture
farmer. POST /confirm is the Beckn booking call: it is recorded and answered
with a fake order id. This process has no upstream client, so the confirm
cannot reach PashuGPT or send an SMS.

Do not point the shared amul_app container here.
"""
from __future__ import annotations

import hashlib
import json
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

HOST = "127.0.0.1"
PORT = 5695

FARMER_MOBILE = "9000000001"
FARMER = {
    "state": "Gujarat",
    "district": "Banaskantha",
    "subDistrict": "Palanpur",
    "village": "Evalpur",
    "unionName": "banas",
    "unionCode": "EVAL-UNION",
    "societyName": "Eval Society",
    "societyCode": "EVAL-SOC",
    "farmerName": "Eval Farmer",
    "mobileNumber": FARMER_MOBILE,
    "farmerCode": "EVAL-FARMER",
    "avgMilkPerDayCow": 8.0,
    "avgMilkPerDayBuff": 6.0,
    "tagNo": "EVAL-TAG-COW,EVAL-TAG-BUFF",
    "cow": 1,
    "Buffalo": 1,
}

TECHNICIANS = [
    {"userId": "EVAL-TECH-1", "fullName": "Rameshbhai Chaudhary", "mobileNumber": "9000000002"},
    {"userId": "EVAL-TECH-2", "fullName": "Sureshbhai Patel", "mobileNumber": "9000000003"},
]

MILK_HISTORY_DAYS = 60
MILK_RATE_PER_FAT_KG = 820  # rupees per kg of fat


def _milk_records(today: date) -> tuple[list[dict], list[dict]]:
    """Deterministic morning/evening pours for the last MILK_HISTORY_DAYS days, plus monthly deductions."""
    milk, deduction = [], []
    for back in range(MILK_HISTORY_DAYS - 1, -1, -1):
        day = today - timedelta(days=back)
        for shift in ("morning", "evening"):
            if back == 0 and shift == "evening":
                continue
            seed = int(hashlib.sha1(f"{day}:{shift}".encode()).hexdigest()[:8], 16)
            qty = round((4.5 if shift == "morning" else 3.5) + (seed % 13) / 10, 1)
            fat = round(3.8 + (seed >> 4) % 9 / 10, 1)
            snf = round(8.2 + (seed >> 8) % 5 / 10, 1)
            amount = round(qty * fat / 100 * MILK_RATE_PER_FAT_KG, 2)
            milk.append({"date": day.isoformat(), "shift": shift, "qty": qty, "fat": fat, "snf": snf, "amount": amount})
        if day.day == 5:
            deduction.append({"date": day.isoformat(), "accountname": "Cattle feed", "amount": 1450})
        if day.day == 20:
            deduction.append({"date": day.isoformat(), "accountname": "Mineral mixture", "amount": 240})
    return milk, deduction


def milk_collection(query: dict[str, str]) -> dict:
    milk, deduction = _milk_records(date.today())
    start, end = query.get("fromdate", ""), query.get("todate", "")
    in_range = lambda r: (not start or r["date"] >= start) and (not end or r["date"] <= end)  # noqa: E731
    return {
        "result": "ok",
        "milk": [r for r in milk if in_range(r)],
        "deduction": [r for r in deduction if in_range(r)],
    }

# In-process record of booking attempts. Never forwarded.
WRITE_LOG: list[dict] = []
_TICKETS = {"CreateAICall": 0, "CreateHealthCall": 0}


def _query(path: str) -> dict[str, str]:
    parsed = urlparse(path)
    raw = parse_qs(parsed.query, keep_blank_values=True)
    return {key: values[-1] for key, values in raw.items()}


def _endpoint(path: str) -> str:
    return urlparse(path).path.rstrip("/").rsplit("/", 1)[-1]


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print(f"[amul-pashu-mock] {self.address_string()} {fmt % args}", flush=True)

    def _send(self, status: int, payload) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        name = _endpoint(self.path)
        query = _query(self.path)
        if name == "GetFarmerDetailsByMobile":
            mobile = "".join(ch for ch in query.get("mobileNumber", "") if ch.isdigit())[-10:]
            self._send(200, [FARMER] if mobile == FARMER_MOBILE else [])
            return
        if name == "GetAITUserDetailsBySocietyCode":
            self._send(200, TECHNICIANS)
            return
        if name == "FarmerMilkCollectionDetails":
            self._send(200, milk_collection(query))
            return
        if name == "get-amul-farmer":
            self._send(200, {"Farmer": [FARMER]})
            return
        if name == "_eval_writes":
            self._send(200, {"count": len(WRITE_LOG), "writes": WRITE_LOG})
            return
        self._send(404, {"error": "read endpoint not stubbed", "endpoint": name})

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}

    def do_POST(self) -> None:  # noqa: N802
        name = _endpoint(self.path)
        query = _query(self.path)
        if name == "get-amul-farmer":
            self._body()
            self._send(200, {"Farmer": [FARMER]})
            return
        if name == "confirm":
            body = self._body()
            order = (body.get("message") or {}).get("order") or {}
            _TICKETS["confirm"] = _TICKETS.get("confirm", 0) + 1
            ticket = f"EVAL-NET-{_TICKETS['confirm']:04d}"
            WRITE_LOG.append({
                "refused": False,
                "endpoint": "confirm",
                "order": order,
                "ticketNumber": ticket,
            })
            self._send(200, {
                "context": body.get("context") or {},
                "message": {"ack": {"status": "ACK"}, "order": {"id": ticket}},
            })
            return
        if name not in _TICKETS:
            self._body()
            WRITE_LOG.append({"refused": True, "endpoint": name, "query": query})
            self._send(403, {"error": "write refused", "endpoint": name})
            return
        self._body()
        _TICKETS[name] += 1
        kind = "AI" if name == "CreateAICall" else "HEALTH"
        ticket = f"EVAL-{kind}-{_TICKETS[name]:04d}"
        WRITE_LOG.append({"refused": False, "endpoint": name, "query": query, "ticketNumber": ticket})
        if name == "CreateAICall":
            tech = next((t for t in TECHNICIANS if t["userId"] == query.get("userId")), TECHNICIANS[0])
            self._send(200, {"aitName": tech["fullName"], "ticketNumber": ticket})
            return
        self._send(200, {"ticketNumber": ticket})

    def do_PUT(self) -> None:  # noqa: N802
        self._refuse()

    def do_PATCH(self) -> None:  # noqa: N802
        self._refuse()

    def do_DELETE(self) -> None:  # noqa: N802
        self._refuse()

    def _refuse(self) -> None:
        name = _endpoint(self.path)
        WRITE_LOG.append({"refused": True, "method": self.command, "endpoint": name})
        self._send(403, {"error": "write refused", "endpoint": name})


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"amul pashu mock on http://{HOST}:{PORT} (no upstream)")
    server.serve_forever()


if __name__ == "__main__":
    main()
