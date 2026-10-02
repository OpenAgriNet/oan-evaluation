"""Read Amul dataset-run traces from Langfuse and write scores back."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen


def load_env_file(path: str | None) -> None:
    if not path:
        return
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class Langfuse:
    def __init__(self) -> None:
        host = os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST")
        public = os.environ.get("LANGFUSE_PUBLIC_KEY")
        secret = os.environ.get("LANGFUSE_SECRET_KEY")
        if not (host and public and secret):
            raise SystemExit("LANGFUSE_BASE_URL/HOST, LANGFUSE_PUBLIC_KEY and LANGFUSE_SECRET_KEY are required")
        self.host = host.rstrip("/")
        token = base64.b64encode(f"{public}:{secret}".encode()).decode()
        self._headers = {"Authorization": f"Basic {token}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, params: dict | None = None, body: Any = None) -> Any:
        url = self.host + path + ("?" + urlencode(params) if params else "")
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        req = Request(url, data=data, method=method, headers=self._headers)
        with urlopen(req, timeout=120) as resp:
            return json.load(resp)

    def get(self, path: str, params: dict | None = None) -> Any:
        return self._request("GET", path, params)

    def dataset_items(self, dataset: str) -> dict[str, dict]:
        items: dict[str, dict] = {}
        page = 1
        while True:
            payload = self.get("/api/public/dataset-items", {"datasetName": dataset, "limit": 100, "page": page})
            batch = payload.get("data") or []
            items.update({item["id"]: item for item in batch})
            if len(batch) < 100:
                return items
            page += 1

    def run_items(self, dataset: str, run: str) -> list[dict]:
        payload = self.get(f"/api/public/datasets/{quote(dataset, safe='')}/runs/{quote(run, safe='')}")
        return payload.get("datasetRunItems") or []

    def trace(self, trace_id: str) -> dict:
        return self.get(f"/api/public/traces/{trace_id}")

    def post_score(
        self,
        *,
        trace_id: str,
        name: str,
        value: float,
        comment: str,
        metadata: dict | None = None,
    ) -> None:
        score_id = hashlib.sha1(f"{trace_id}:{name}".encode()).hexdigest()
        self._request(
            "POST",
            "/api/public/scores",
            body={
                "id": score_id,
                "traceId": trace_id,
                "name": name,
                "value": value,
                "dataType": "NUMERIC",
                "comment": comment,
                "metadata": metadata or {},
            },
        )


def _sorted_obs(trace: dict) -> list[dict]:
    return sorted(trace.get("observations") or [], key=lambda o: o.get("startTime") or "")


def _api_call(obs: dict) -> bool:
    name = obs.get("name") or ""
    return obs.get("type") == "SPAN" and (name.endswith("_api") or name.startswith("fetch_"))


def build_turn(trace: dict, item: dict) -> dict:
    obs = _sorted_obs(trace)
    item_input = item.get("input") if isinstance(item.get("input"), dict) else {}
    trace_input = trace.get("input") if isinstance(trace.get("input"), dict) else {}

    question_en = next(
        (o.get("output") for o in obs if o.get("name") == "query_pretranslation" and isinstance(o.get("output"), str)),
        None,
    )
    answer_en_parts = [
        o["input"]["text"]
        for o in obs
        if o.get("name") == "stream_translation" and isinstance(o.get("input"), dict) and isinstance(o["input"].get("text"), str)
    ]
    moderation = next(
        (o.get("output") for o in obs if o.get("name") == "Moderation" and isinstance(o.get("output"), dict)),
        {},
    )
    return {
        "trace_id": trace["id"],
        "session_id": trace.get("sessionId"),
        "timestamp": trace.get("timestamp"),
        "latency_s": trace.get("latency"),
        "question_id": item_input.get("question_id"),
        "category": (item.get("metadata") or {}).get("category"),
        "tool_hint": (item.get("metadata") or {}).get("tool_hint"),
        "expected_output": item.get("expectedOutput"),
        "question_gu": trace_input.get("query"),
        "question_en": question_en,
        "answer_en": "".join(answer_en_parts) or None,
        "answer_gu": trace.get("output") if isinstance(trace.get("output"), str) else json.dumps(trace.get("output"), ensure_ascii=False),
        "moderation": moderation.get("category"),
        "tool_calls": [
            {"name": o.get("name"), "input": o.get("input"), "output": o.get("output")}
            for o in obs
            if o.get("type") == "TOOL"
        ],
        "api_calls": [
            {"name": o.get("name"), "input": o.get("input"), "output": o.get("output")}
            for o in obs
            if _api_call(o)
        ],
    }


def load_run(lf: Langfuse, dataset: str, run: str) -> list[dict]:
    """Every turn of the run, ordered by session then time, with prior turns attached."""
    items = lf.dataset_items(dataset)
    turns = [build_turn(lf.trace(ri["traceId"]), items.get(ri["datasetItemId"], {})) for ri in lf.run_items(dataset, run)]
    turns.sort(key=lambda t: (t["session_id"] or "", t["timestamp"] or ""))
    by_session: dict[str, list[dict]] = {}
    for turn in turns:
        history = by_session.setdefault(turn["session_id"], [])
        turn["turn_index"] = len(history) + 1
        turn["history"] = [
            {"question_gu": h["question_gu"], "answer_gu": h["answer_gu"], "tools": [c["name"] for c in h["tool_calls"]]}
            for h in history
        ]
        history.append(turn)
    for history in by_session.values():
        history[-1]["is_last_turn"] = True
    return turns
