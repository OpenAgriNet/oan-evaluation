"""Reference content from other Amul Agent Dev conversations, for eval turns whose own content tools failed.

Only knowledge-base tools are used (document search, scheme lookups): their results are the same for every farmer,
so another conversation's successful result is a fair reference. Farmer-specific tools (milk, booking) are not.
"""
from __future__ import annotations

import json
import re

from rapidfuzz import fuzz

from evaluation.amul.langfuse_io import Langfuse

REFERENCE_TOOLS = {
    "search_documents": "query",
    "get_union_scheme_data": "scheme_name",
    "get_vistaar_scheme_info": "scheme_code",
}
FAILED = re.compile(r"unavailable|error|failed", re.I)
MIN_SIMILARITY = 70
MAX_REFS_PER_CALL = 2
MAX_REF_CHARS = 5000


def _text(output) -> str:
    return output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)


def _key(tool: str, call_input) -> str:
    if not isinstance(call_input, dict):
        return ""
    return str(call_input.get(REFERENCE_TOOLS[tool]) or "").strip()


def load_library(lf: Langfuse, pages: int = 10) -> dict[str, list[dict]]:
    library: dict[str, list[dict]] = {}
    for tool in REFERENCE_TOOLS:
        seen: set[str] = set()
        entries = []
        for page in range(1, pages + 1):
            data = lf.get("/api/public/observations", {"name": tool, "type": "TOOL", "limit": 100, "page": page}).get("data") or []
            for obs in data:
                output = _text(obs.get("output"))
                if not output or FAILED.search(output[:300]) or output in seen:
                    continue
                seen.add(output)
                entries.append({"input": obs.get("input") or {}, "key": _key(tool, obs.get("input")), "output": output})
            if len(data) < 100:
                break
        library[tool] = entries
    return library


def _score(tool: str, wanted: str, entry: dict, union: str | None) -> int:
    if tool == "get_vistaar_scheme_info":
        return 100 if wanted and wanted.lower() == entry["key"].lower() else 0
    if not wanted and not entry["key"]:
        base = 100
    elif not wanted or not entry["key"]:
        return 0
    else:
        base = int(fuzz.token_set_ratio(wanted.lower(), entry["key"].lower()))
    if tool == "get_union_scheme_data" and union and union.lower() not in entry["output"].lower():
        base -= 15
    return base


def attach_references(turns: list[dict], library: dict[str, list[dict]], union: str | None = None) -> int:
    """Adds turn['prod_reference'] for failed knowledge-base calls. Returns how many turns got a reference."""
    attached = 0
    for turn in turns:
        refs, used = [], set()
        for call in turn["tool_calls"]:
            tool = call.get("name")
            if tool not in REFERENCE_TOOLS or not FAILED.search(_text(call.get("output"))[:300]):
                continue
            wanted = _key(tool, call.get("input"))
            ranked = sorted(
                ((_score(tool, wanted, entry, union), entry) for entry in library.get(tool, [])),
                key=lambda pair: pair[0],
                reverse=True,
            )
            for similarity, entry in ranked[:MAX_REFS_PER_CALL]:
                if similarity < MIN_SIMILARITY or entry["output"] in used:
                    continue
                used.add(entry["output"])
                output = entry["output"]
                if len(output) > MAX_REF_CHARS:
                    output = output[:MAX_REF_CHARS] + " ...[truncated]"
                refs.append({"tool": tool, "input": entry["input"], "similarity": similarity, "output": output})
        if refs:
            turn["prod_reference"] = refs
            attached += 1
        else:
            turn.pop("prod_reference", None)
    return attached
