#!/usr/bin/env python3
"""Hit the local stub only. Does not open any other host."""
from __future__ import annotations

import json
import threading
import urllib.request
from datetime import date, timedelta
from http.server import ThreadingHTTPServer

from server import HOST, PORT, Handler, WRITE_LOG


def main() -> None:
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://{HOST}:{PORT}"

    def get(path: str) -> tuple[int, object]:
        with urllib.request.urlopen(base + path) as resp:
            return resp.status, json.load(resp)

    def post(path: str) -> tuple[int, object]:
        req = urllib.request.Request(base + path, data=b"", method="POST")
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.load(resp)

    status, farmer = get("/GetFarmerDetailsByMobile?mobileNumber=9000000001")
    assert status == 200 and farmer[0]["farmerCode"] == "EVAL-FARMER", farmer
    status, techs = get("/GetAITUserDetailsBySocietyCode?unionCode=EVAL-UNION&societyCode=EVAL-SOC")
    assert status == 200 and len(techs) == 2, techs
    today = date.today()
    week_ago = (today - timedelta(days=6)).isoformat()
    status, milk = get(f"/FarmerMilkCollectionDetails?fromdate={week_ago}&todate={today.isoformat()}")
    assert status == 200 and len(milk["milk"]) == 13, len(milk["milk"])
    assert all(week_ago <= r["date"] <= today.isoformat() for r in milk["milk"] + milk["deduction"])
    status, old = get("/FarmerMilkCollectionDetails?fromdate=2020-01-01&todate=2020-01-31")
    assert status == 200 and old["milk"] == [] and old["deduction"] == [], old
    status, booked = post(
        "/CreateAICall?unionCode=EVAL-UNION&societyCode=EVAL-SOC&farmerCode=EVAL-FARMER&userId=EVAL-TECH-1&species=cow"
    )
    assert status == 200 and booked["ticketNumber"].startswith("EVAL-AI-"), booked
    confirm = json.dumps({
        "context": {"domain": "services:amul-vet-booking", "action": "confirm"},
        "message": {"order": {
            "provider": {"id": "amul-ai-service"},
            "items": [{"id": "ait:EVAL-TECH-1"}],
            "fulfillment": {"customer": {"tags": [
                {"code": "farmer_id", "value": "EVAL-FARMER"},
                {"code": "species", "value": "cow"},
            ]}},
        }},
    }).encode()
    req = urllib.request.Request(
        base + "/confirm", data=confirm, method="POST", headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req) as resp:
        ack = json.load(resp)
    assert ack["message"]["ack"]["status"] == "ACK"
    assert ack["message"]["order"]["id"].startswith("EVAL-NET-")
    status, log = get("/_eval_writes")
    assert status == 200 and log["count"] == 2
    assert WRITE_LOG[0]["query"]["species"] == "cow"
    assert WRITE_LOG[1]["order"]["items"][0]["id"] == "ait:EVAL-TECH-1"
    server.shutdown()
    print("smoke ok: fixture read, booking stored locally, no upstream")


if __name__ == "__main__":
    main()
