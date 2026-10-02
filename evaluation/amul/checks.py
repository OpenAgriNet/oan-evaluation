"""Rule-based Amul metrics. No LLM calls; each returns (score, reason) or None when not applicable."""
from __future__ import annotations

import json
import re
from datetime import date
from typing import Callable

from helpers.language_utils import (
    KNOWN_TECHNICAL,
    LANGUAGE_CONFIGS,
    build_reason,
    detect_english_mixing,
    detect_foreign_scripts,
)
from evaluation.amul.glossary import glossary_adherence
from mock_server.amul_pashu_mock.server import FARMER, TECHNICIANS

Result = tuple[float, str] | None

TICKET_IN_TOOL = re.compile(r"[Tt]icket\s*(?:[Nn]umber|[Nn]o\.?|[Ii][Dd])?\s*[:#]?\s*\"?([A-Z0-9][A-Z0-9-]{3,})")
TICKET_IN_ANSWER = re.compile(r"\b[A-Z][A-Z0-9]*(?:-[A-Z0-9]+)*-\d{2,}\b|\b[A-Z]{2,4}\d{6,}\b")
TICKET_WORDS = ("ટિકિટ", "ticket", "Ticket")
LONG_NUMBER = re.compile(r"\b\d{6,}\b")
SNAKE_CASE = re.compile(r"\b[a-z]+(?:_[a-z0-9]+)+\b")
CAMEL_CASE = re.compile(r"\b[a-z]+[A-Z][a-zA-Z]+\b")
BRAND_NAMES = {"iKhedut", "eKYC", "eNAM", "mKisan", "iPhone"}
JSON_LIKE = re.compile(r"[{\[]\s*\"|\"\s*:\s*[\"{\[\d]")
INTERNAL_HOST = re.compile(r"127\.0\.0\.1|localhost|https?://[^\s]*(?:internal|:\d{4})", re.I)
FILE_NAME = re.compile(r"[^\s()*]+\.(?:docx?|pdf|xlsx?|csv|json|txt|pptx?)\b", re.I)
# "provided documents" is excluded: the agent prompt mandates "I don't know based on the provided documents".
REASONING_LEAK = re.compile(
    r"\b(?:the user|user's|system prompt|farmer profile|tool calls?|function calls?|query variants?|planning block"
    r"|slots?|I will (?:call|search|use)|I'll (?:call|search|use)|let me (?:search|check|call|look)|calling the"
    r"|context provided|according to the tool)\b",
    re.I,
)
# Dairy abbreviations farmers use; a bracketed one such as "કૃત્રિમ બીજદાન (AI)" counts as an abbreviation, not bracketed English.
DAIRY_ABBREV = re.compile(r"^(?:AI|PD|SNF|FAT|CVCC|BIS|TMR|LSD|HS|BQ|IVF|ET|MSP|CLR)$", re.I)

BUFFALO_WORDS = re.compile(r"\bbuffalo(?:es|s)?\b|ભેંસ|ભેસ|પાડી", re.I)
COW_WORDS = re.compile(r"\bcows?\b|\bheifers?\b|ગાય|વાછરડી", re.I)
URGENT_WORDS = re.compile(r"\b(?:urgent|emergency|immediately|serious|critical)\b|તાત્કાલિક|ઇમરજન્સી|ગંભીર", re.I)
MAX_MILK_RANGE_DAYS = 31


def _session_tickets(turn: dict, session: list[dict]) -> set[str]:
    found: set[str] = set()
    for t in session:
        if t["timestamp"] and turn["timestamp"] and t["timestamp"] > turn["timestamp"]:
            continue
        for call in t["tool_calls"]:
            found.update(TICKET_IN_TOOL.findall(json.dumps(call.get("output"), ensure_ascii=False)))
    return found


def ticket_consistency(turn: dict, session: list[dict]) -> Result:
    answer = turn["answer_gu"] or ""
    returned_now: set[str] = set()
    for call in turn["tool_calls"]:
        returned_now.update(TICKET_IN_TOOL.findall(json.dumps(call.get("output"), ensure_ascii=False)))
    in_answer = set(TICKET_IN_ANSWER.findall(answer))
    if any(word in answer for word in TICKET_WORDS):
        in_answer.update(LONG_NUMBER.findall(answer))
    if not returned_now and not in_answer:
        return None
    known = _session_tickets(turn, session)
    invented = sorted(in_answer - known)
    if invented:
        return 0.0, f"Reply gives ticket {', '.join(invented)}, but no booking tool returned it."
    untold = sorted(returned_now - in_answer)
    if untold:
        english = turn.get("answer_en") or ""
        in_english = [t for t in untold if t in english]
        altered = [t for t in untold if re.search(r"\d+$", t) and re.search(r"\d+$", t).group() in answer]
        if in_english and altered:
            return 0.0, (
                f"Booking tool returned ticket {', '.join(untold)} and the English answer has it, "
                "but the Gujarati translation changed it (e.g. transliterated letters), so the farmer gets a wrong ticket."
            )
        if in_english:
            return 0.0, f"English answer has ticket {', '.join(untold)}, but the Gujarati translation dropped it."
        return 0.0, f"Booking tool returned ticket {', '.join(untold)}, but the reply does not tell the farmer."
    return 1.0, f"Reply's ticket {', '.join(sorted(in_answer))} matches the booking tool's result."


def expected_tool_called(turn: dict, session: list[dict]) -> Result:
    if not turn.get("is_last_turn") or not turn.get("tool_hint"):
        return None
    expected = turn["tool_hint"]
    called = [c["name"] for t in session for c in t["tool_calls"]]
    if expected in called:
        return 1.0, f"Session called {expected} (tools used: {', '.join(dict.fromkeys(called))})."
    used = ", ".join(dict.fromkeys(called)) or "none"
    return 0.0, f"Session never called {expected}; tools used: {used}. Scored on the last turn for the whole session."


def output_hygiene(turn: dict, session: list[dict]) -> Result:
    answer = turn["answer_gu"] or ""
    if not answer:
        return None
    tool_names = {c["name"] for t in session for c in t["tool_calls"] + t["api_calls"] if c.get("name")}
    problems = []
    leaked_tools = sorted(name for name in tool_names if name in answer)
    if leaked_tools:
        problems.append(f"tool names {leaked_tools}")
    identifiers = sorted((set(SNAKE_CASE.findall(answer)) | set(CAMEL_CASE.findall(answer))) - set(leaked_tools) - BRAND_NAMES)
    if identifiers:
        problems.append(f"code identifiers {identifiers[:5]}")
    if JSON_LIKE.search(answer):
        problems.append("raw JSON")
    if INTERNAL_HOST.search(answer):
        problems.append("internal URL")
    english = turn.get("answer_en") or ""
    files = sorted(set(FILE_NAME.findall(answer)) | set(FILE_NAME.findall(english)))
    if files:
        problems.append(f"file names {files[:3]}")
    tech_ids = {
        str((c.get("input") or {}).get("user_id"))
        for t in session
        for c in t["tool_calls"]
        if isinstance(c.get("input"), dict) and (c.get("input") or {}).get("user_id")
    } | {tech["userId"] for tech in TECHNICIANS}
    leaked_ids = sorted(i for i in tech_ids if i in answer or i in english)
    if leaked_ids:
        problems.append(f"technician user IDs {leaked_ids}")
    leaks = sorted({m.group(0) for m in REASONING_LEAK.finditer(english)})
    if leaks:
        problems.append(f"reasoning/instruction words {leaks[:3]}")
    if problems:
        return 0.0, "Reply exposes " + "; ".join(problems) + "."
    return 1.0, "No tool names, code identifiers, JSON, internal URLs, file names, technician IDs or leaked reasoning in the reply."


def language_mixing(turn: dict, session: list[dict]) -> Result:
    answer = turn["answer_gu"] or ""
    if not answer:
        return None
    answer = TICKET_IN_ANSWER.sub(" ", answer)
    config = LANGUAGE_CONFIGS["gujarati"]
    foreign = detect_foreign_scripts(answer, config)
    english = detect_english_mixing(answer)
    bracketed = english.get("parens_english") or []
    abbrevs = [b for b in bracketed if all(DAIRY_ABBREV.match(w) or KNOWN_TECHNICAL.match(w) for w in re.split(r"[\s/,-]+", b) if w)]
    english["parens_english"] = [b for b in bracketed if b not in abbrevs]
    english["technical_abbrevs"] = (english.get("technical_abbrevs") or []) + abbrevs
    if foreign:
        score = 1.0
    elif english.get("inline_english"):
        score = 2.0
    elif english.get("parens_english"):
        score = 3.0
    elif english.get("technical_abbrevs"):
        score = 4.0
    else:
        score = 5.0
    return score, build_reason(config, foreign, english) + " (1 = other script, 2 = English words inline, 3 = English in brackets, 4 = only abbreviations like AI/PD, 5 = clean)"


def _species_said(turn: dict, session: list[dict]) -> str | None:
    """Species in the farmer's latest message (this turn or earlier) that names exactly one."""
    for t in sorted((t for t in session if t["turn_index"] <= turn["turn_index"]), key=lambda t: -t["turn_index"]):
        text = f"{t.get('question_en') or ''} {t.get('question_gu') or ''}"
        buffalo, cow = bool(BUFFALO_WORDS.search(text)), bool(COW_WORDS.search(text))
        if buffalo != cow:
            return "buffalo" if buffalo else "cow"
    return None


def _farmer_text(turn: dict, session: list[dict]) -> str:
    return " ".join(
        f"{t.get('question_en') or ''} {t.get('question_gu') or ''}" for t in session if t["turn_index"] <= turn["turn_index"]
    )


def tool_arguments(turn: dict, session: list[dict]) -> Result:
    """Booking and milk tool arguments against the farmer profile, technician list and the farmer's own words."""
    problems, checked = [], []
    profile = {"union_code": FARMER["unionCode"], "society_code": FARMER["societyCode"], "farmer_code": FARMER["farmerCode"]}
    for call in turn["tool_calls"]:
        name, args = call.get("name"), call.get("input") if isinstance(call.get("input"), dict) else {}
        if name in ("create_ai_call", "create_health_call"):
            checked.append(name)
            for key, expected in profile.items():
                if str(args.get(key) or "") != expected:
                    problems.append(f"{name} {key}={args.get(key)!r}, profile has {expected!r}")
            said = _species_said(turn, session)
            if said and str(args.get("species") or "").lower() != said:
                problems.append(f"{name} species={args.get('species')!r}, farmer said {said}")
            if name == "create_ai_call":
                tech_ids = {tech["userId"] for tech in TECHNICIANS}
                if args.get("user_id") not in tech_ids:
                    problems.append(f"create_ai_call user_id={args.get('user_id')!r} is not a listed technician")
            if name == "create_health_call":
                urgent = bool(URGENT_WORDS.search(_farmer_text(turn, session)))
                case_type = str(args.get("case_type") or "").lower()
                if urgent and case_type != "emergency":
                    problems.append(f"create_health_call case_type={case_type!r}, farmer described an emergency")
        elif name == "get_farmer_milk_collection_details":
            checked.append(name)
            try:
                start, end = date.fromisoformat(args.get("fromdate", "")), date.fromisoformat(args.get("todate", ""))
            except ValueError:
                problems.append(f"milk dates not YYYY-MM-DD: {args.get('fromdate')!r}..{args.get('todate')!r}")
                continue
            asked_on = date.fromisoformat((turn.get("timestamp") or "")[:10]) if turn.get("timestamp") else None
            if end < start:
                problems.append(f"milk range ends before it starts ({start}..{end})")
            elif (end - start).days > MAX_MILK_RANGE_DAYS:
                problems.append(f"milk range {start}..{end} exceeds the tool's {MAX_MILK_RANGE_DAYS}-day limit; it should be split")
            if asked_on and end > asked_on:
                problems.append(f"milk range ends {end}, after the question date {asked_on}")
    if not checked:
        return None
    if problems:
        return 0.0, "; ".join(problems) + "."
    return 1.0, f"Arguments of {', '.join(dict.fromkeys(checked))} match the farmer profile, technician list and the farmer's words."


def latency_s(turn: dict, session: list[dict]) -> Result:
    if turn.get("latency_s") is None:
        return None
    return round(float(turn["latency_s"]), 2), "End-to-end trace latency in seconds."


CHECKS: dict[str, Callable[[dict, list[dict]], Result]] = {
    "expected_tool_called": expected_tool_called,
    "ticket_consistency": ticket_consistency,
    "tool_arguments": tool_arguments,
    "output_hygiene": output_hygiene,
    "language_mixing": language_mixing,
    "glossary_adherence": glossary_adherence,
    "latency_s": latency_s,
}
