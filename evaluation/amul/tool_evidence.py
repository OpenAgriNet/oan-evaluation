"""Bounded operational evidence; never infer success from nonempty tool text."""


def outcome_evidence(call: dict) -> dict:
    metadata = call.get("metadata") or {}
    evidence = {}
    for key, field in [("amul.tool_status", "tool_status"), ("amul.cache", "cache")]:
        if key in metadata:
            evidence[field] = metadata[key]
    for field in ("level", "status_message"):
        if call.get(field) is not None:
            evidence[field] = call[field]
    return evidence


OUTCOME_RULES = (
    "Operational evidence: tool_status=success means the tool handled its request, "
    "including asking for missing information; it does not confirm a booking. "
    "Use the actual receipt/result and relevant backend ok/error evidence. "
    "empty/no_result mean no matching data was returned; unavailable/error mean a failed call. "
    "An honest failure explanation can still handle the farmer's request correctly. "
    "Missing status is unknown, not inferred success. Backend operations are nested evidence, "
    "not additional agent tool calls or duplicate bookings."
)
