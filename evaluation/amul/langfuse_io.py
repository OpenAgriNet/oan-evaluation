"""Read Amul dataset-run traces from Langfuse and write scores back."""
from __future__ import annotations

import base64
import hashlib
import json
import os
from datetime import datetime
from math import isfinite
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


def _is_tool(obs: dict) -> bool:
    if obs.get("type") == "TOOL":
        return True
    # Current pydantic-ai instrumentation exports tool steps as SPAN. The
    # app's reporting wrapper stamps this explicit marker on the tool itself.
    # Names, nonempty output and cache metadata alone are not tool evidence.
    status = (obs.get("metadata") or {}).get("amul.tool_status")
    return obs.get("type") == "SPAN" and isinstance(status, str) and bool(status.strip())


def _api_call(obs: dict, by_id: dict[str, dict]) -> bool:
    name = obs.get("name") or ""
    if obs.get("type") != "SPAN" or _is_tool(obs):
        return False
    if name.endswith("_api") or name.startswith("fetch_") or name == "ai_call_booking":
        return True
    # record_api_result emits a boolean `ok`. Require tool ancestry for other
    # names so agent, translation and background-job spans cannot become calls.
    output = obs.get("output")
    if not isinstance(output, dict) or not isinstance(output.get("ok"), bool):
        return False
    parent = obs.get("parentObservationId")
    seen = set()
    while parent in by_id and parent not in seen:
        seen.add(parent)
        ancestor = by_id[parent]
        if _is_tool(ancestor):
            return True
        parent = ancestor.get("parentObservationId")
    return False


def _call_evidence(obs: dict) -> dict:
    call = {"name": obs.get("name"), "input": obs.get("input"), "output": obs.get("output")}
    for source, target in [("id", "observation_id"), ("parentObservationId", "parent_observation_id"),
                           ("level", "level"), ("statusMessage", "status_message")]:
        if obs.get(source) is not None:
            call[target] = obs[source]
    metadata = {k: v for k, v in (obs.get("metadata") or {}).items()
                if k in ("amul.tool_status", "amul.cache")}
    if metadata:
        call["metadata"] = metadata
    return call


def _named_output(obs: list[dict], names: tuple[str, ...], expected_type: type) -> Any:
    """Prefer the current stage name, retaining the earlier trace format."""
    return next(
        (o["output"] for name in names for o in obs
         if o.get("name") == name and isinstance(o.get("output"), expected_type)),
        None,
    )


def _reply_latency(trace: dict, obs: list[dict]) -> float | None:
    """The root turn ends with the answer; later suggestions extend the trace."""
    root = next(
        (o for o in obs if o.get("name") == "turn" and o.get("type") == "SPAN"
         and o.get("parentObservationId") is None),
        None,
    )
    if root is not None:
        try:
            start = datetime.fromisoformat(root["startTime"].replace("Z", "+00:00"))
            end = datetime.fromisoformat(root["endTime"].replace("Z", "+00:00"))
            duration = (end - start).total_seconds()
            if duration >= 0:
                return duration
        except (KeyError, AttributeError, TypeError, ValueError, OverflowError):
            pass
        # Some exports provide only the observation's duration (in seconds).
        duration = root.get("latency")
        if isinstance(duration, (int, float)) and not isinstance(duration, bool):
            if isfinite(duration) and duration >= 0:
                return duration
        return None
    if (trace.get("metadata") or {}).get("amul.schema_version") == "turn.v1" or trace.get("name") == "turn":
        # An incomplete new trace must not silently acquire suggestions time.
        return None
    return trace.get("latency")


def build_turn(trace: dict, item: dict) -> dict:
    obs = _sorted_obs(trace)
    by_id = {o["id"]: o for o in obs if o.get("id")}
    item_input = item.get("input") if isinstance(item.get("input"), dict) else {}
    trace_input = trace.get("input") if isinstance(trace.get("input"), dict) else {}

    question_en = _named_output(obs, ("pre_translation", "query_pretranslation"), str)
    metadata = trace.get("metadata") or {}
    if question_en is None and metadata.get("amul.source_lang") == "en" and isinstance(trace.get("input"), str):
        question_en = trace["input"]
    answer_en_parts = [
        o["input"]["text"]
        for o in obs
        if o.get("name") == "stream_translation" and isinstance(o.get("input"), dict) and isinstance(o["input"].get("text"), str)
    ]
    answer_en = "".join(answer_en_parts) or None
    if answer_en is None and metadata.get("amul.target_lang") == "en" and isinstance(trace.get("output"), str):
        answer_en = trace["output"]
    moderation = _named_output(obs, ("moderation", "Moderation"), dict) or {}
    return {
        "trace_id": trace["id"],
        "session_id": trace.get("sessionId"),
        "timestamp": trace.get("timestamp"),
        "latency_s": _reply_latency(trace, obs),
        "question_id": item_input.get("question_id"),
        "category": (item.get("metadata") or {}).get("category"),
        "tool_hint": (item.get("metadata") or {}).get("tool_hint"),
        "expected_output": item.get("expectedOutput"),
        "question_gu": trace["input"] if isinstance(trace.get("input"), str) else trace_input.get("query"),
        "question_en": question_en,
        "answer_en": answer_en,
        "answer_gu": trace.get("output") if isinstance(trace.get("output"), str) else json.dumps(trace.get("output"), ensure_ascii=False),
        "moderation": moderation.get("category"),
        "tool_calls": [
            _call_evidence(o)
            for o in obs
            if _is_tool(o)
        ],
        "api_calls": [
            _call_evidence(o)
            for o in obs
            if _api_call(o, by_id)
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
    return turns
