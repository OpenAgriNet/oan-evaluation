"""Rule-based Amul metrics. No LLM calls; each returns (score, reason) or None when not applicable."""
from __future__ import annotations

import json
import re
from datetime import date
from decimal import Decimal
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
COW_WORDS = re.compile(r"\bcows?\b|ગાય|વાછરડી", re.I)
URGENT_WORDS = re.compile(r"\b(?:urgent|emergency|immediately|serious|critical)\b|તાત્કાલિક|ઇમરજન્સી|ગંભીર", re.I)
MAX_MILK_RANGE_DAYS = 31
_MILK_ROW = re.compile(
    r"\|\s*(\d{4}-\d{2}-\d{2})\s*\|\s*([^|]+)\|\s*([\d.]+)\s*\|\s*[\d.]+\s*\|\s*[\d.]+\s*\|\s*([\d.]+)\s*\|"
)
_LITRES = re.compile(r"(\d[\d,]*(?:\.\d+)?)\s*(?:lit(?:er|re)?s?\b|લીટર|લિટર)", re.I)
_RUPEES = re.compile(r"(?:₹|rs\.?|INR|રૂપિયા)\s*(\d[\d,]*(?:\.\d+)?)|(\d[\d,]*(?:\.\d+)?)\s*(?:રૂપિયા|રૂ\.)", re.I)
_TOTAL_WORD = re.compile(r"\btotal\b|કુલ", re.I)
_MONEY_ASK = re.compile(r"earning|money|rupee|payment|કમાણી|પૈસા", re.I)
_LITRE_ASK = re.compile(r"lit(?:er|re)|લીટર|લિટર", re.I)


def _session_tickets(turn: dict, session: list[dict]) -> set[str]:
    found: set[str] = set()
    for t in session:
        if t.get('turn_index', 0) > turn.get('turn_index', 0):
            continue
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


def output_hygiene(turn: dict, session: list[dict]) -> Result:
    answer = turn["answer_gu"] or ""
    if not answer:
        return None
    earlier = [t for t in session if t["turn_index"] <= turn["turn_index"]]
    tool_names = {c["name"] for t in earlier for c in t.get("tool_calls", []) + t.get("api_calls", []) if c.get("name")}
    tool_names.update({"search_documents", "create_ai_call", "create_health_call", "get_farmer_milk_collection_details", "get_union_scheme_data", "get_vistaar_scheme_info"})
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
    files = sorted(set(FILE_NAME.findall(answer)))
    if files:
        problems.append(f"file names {files[:3]}")
    tech_ids = {
        str((c.get("input") or {}).get("user_id"))
        for t in earlier
        for c in t["tool_calls"]
        if isinstance(c.get("input"), dict) and (c.get("input") or {}).get("user_id")
    } | {tech["userId"] for tech in TECHNICIANS}
    leaked_ids = sorted(i for i in tech_ids if i in answer)
    if leaked_ids:
        problems.append(f"technician user IDs {leaked_ids}")
    internal_codes = {FARMER['farmerCode'], FARMER['unionCode']}
    scheme_codes = set()
    for t in earlier:
        for call in t.get('tool_calls', []):
            args = call.get('input') or {}
            if not isinstance(args, dict):
                continue
            if call.get('name') in ('create_ai_call', 'create_health_call'):
                for key in ('farmer_code', 'union_code', 'society_code'):
                    value = args.get(key)
                    # The farmer's own society code is explicitly permitted by the final rubric.
                    if value and str(value) != FARMER['societyCode']:
                        internal_codes.add(str(value))
            if call.get('name') == 'get_vistaar_scheme_info':
                scheme_codes.update(str(v) for v in args.values() if str(v).lower() in ('kcc', 'pmkisan', 'pmfby'))
    leaked_codes = sorted(code for code in internal_codes if code in answer)
    if leaked_codes:
        problems.append(f"internal booking codes {leaked_codes}")
    for code in scheme_codes | {'kcc', 'pmkisan', 'pmfby'}:
        # Ordinary uppercase scheme abbreviations are farmer-facing names; lower-case raw codes or code formatting are not.
        if re.search(rf'(?<![\w]){re.escape(code.lower())}(?![\w])', answer) or re.search(rf'`{re.escape(code)}`', answer, re.I):
            problems.append(f"internal scheme code {code}")
    leaks = sorted({m.group(0) for m in REASONING_LEAK.finditer(answer)})
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
        score = 5.0
    else:
        score = 5.0
    return score, build_reason(config, foreign, english) + " (heuristic: 1 = other script, 2 = English words inline, 3 = English in brackets, 5 = clean or allowed abbreviations; judge checks names and meaning)"


def _species_said(turn: dict, session: list[dict]) -> str | None:
    """Species in the farmer's latest message (this turn or earlier) that names exactly one."""
    for t in sorted((t for t in session if t["turn_index"] <= turn["turn_index"]), key=lambda t: -t["turn_index"]):
        original=t.get('question_gu') or ''
        buffalo_gu,cow_gu=bool(BUFFALO_WORDS.search(original)),bool(COW_WORDS.search(original))
        if buffalo_gu != cow_gu:return 'buffalo' if buffalo_gu else 'cow'
        if buffalo_gu and cow_gu:return None  # Both named: do not silently reuse an older selection.
        text = f"{t.get('question_en') or ''} {t.get('question_gu') or ''}"
        if re.search(r'\bheifer\b',t.get('question_en') or '',re.I) and not re.search(r'\bcow\b|\bbuffalo\b|ગાય|ભેંસ|ભેસ',text,re.I):
            return None  # Generic heifer does not uniquely identify cow versus buffalo.
        buffalo, cow = bool(BUFFALO_WORDS.search(text)), bool(COW_WORDS.search(text))
        if buffalo != cow:
            return "buffalo" if buffalo else "cow"
        if buffalo and cow:return None
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
            if said is None:
                problems.append(f"{name} species is unconfirmed: fixture has cow and buffalo and farmer has not uniquely selected one")
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
            elif (end - start).days + 1 > MAX_MILK_RANGE_DAYS:
                problems.append(f"milk range {start}..{end} exceeds the tool's {MAX_MILK_RANGE_DAYS}-day limit; it should be split")
            if asked_on and end > asked_on:
                problems.append(f"milk range ends {end}, after the question date {asked_on}")
    if not checked:
        return None
    if problems:
        return 0.0, "; ".join(problems) + "."
    return 1.0, f"Checked profile codes, confirmed species, technician membership and date validity for {', '.join(dict.fromkeys(checked))}; relative date interpretation requires semantic review."


def _milk_rows_so_far(turn: dict, session: list[dict]) -> list[tuple[Decimal, Decimal]]:
    """Latest retrieval turn's rows; combine split lookups and deduplicate overlapping date/shift rows."""
    found = {}
    for earlier in sorted(session, key=lambda t: t.get('turn_index', 0)):
        if earlier.get("turn_index", 0) > turn.get("turn_index", 0):
            continue
        batch = {}
        retrieved = False
        for call in earlier.get("tool_calls") or []:
            if call.get("name") != "get_farmer_milk_collection_details":
                continue
            retrieved = True
            output = call.get("output")
            text = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            for day, shift, qty, amount in _MILK_ROW.findall(text):
                batch[(day, shift.strip())] = (Decimal(qty), Decimal(amount))
        if retrieved:
            found = batch
    return list(found.values())


def milk_total(turn: dict, session: list[dict]) -> Result:
    """Stated litre or earnings total against the sum of the milk records already fetched.

    Blank when this turn is not asking for a total, or no milk table is in the session yet.
    The grounding judge has both caught and missed the 72.7 L vs 68.4 L error.
    """
    question = f"{turn.get('question_en') or ''} {turn.get('question_gu') or ''}"
    answer = turn.get("answer_en") or ""
    if not _TOTAL_WORD.search(question) and not _TOTAL_WORD.search(answer + ' ' + (turn.get('answer_gu') or '')):
        return None
    rows = _milk_rows_so_far(turn, session)
    if not rows:
        return None
    wants_money = bool(_MONEY_ASK.search(question))
    wants_litres = bool(_LITRE_ASK.search(question))
    if not (wants_litres or wants_money):
        return None
    problems = []
    any_total = False
    for language, text in [('English', answer), ('Gujarati', turn.get('answer_gu') or '')]:
        if not text and language == 'English':
            continue
        normalized = text.translate(str.maketrans('૦૧૨૩૪૫૬૭૮૯', '0123456789'))
        for requested, column, tolerance, unit in [(wants_money, 1, Decimal('0.50'), 'rupees'), (wants_litres, 0, Decimal('0.05'), 'L')]:
            if not requested:
                continue
            actual = sum((row[column] for row in rows), Decimal(0))
            claimed = ([Decimal((a or b).replace(',', '')) for a, b in _RUPEES.findall(normalized)] if column else
                       [Decimal(n.replace(',', '')) for n in _LITRES.findall(normalized)])
            if not claimed:
                problems.append(f'{language} reply states no requested {unit} total')
            else:
                any_total = True
                if abs(claimed[0] - actual) > tolerance:
                    problems.append(f'{language} answer says {claimed[0]:g} {unit}, the {len(rows)} records sum to {actual:g} {unit}')
    if not any_total:
        return None
    if problems:
        return 0.0, "; ".join(problems) + "."
    kind = "earnings and litres" if wants_money and wants_litres else "earnings" if wants_money else "litres"
    return 1.0, f"Stated {kind} total matches the sum of the {len(rows)} collection records."


def latency_s(turn: dict, session: list[dict]) -> Result:
    if turn.get("latency_s") is None:
        return None
    return round(float(turn["latency_s"]), 2), "End-to-end trace latency in seconds."


CHECKS: dict[str, Callable[[dict, list[dict]], Result]] = {
    "output_hygiene": output_hygiene,
    "latency_s": latency_s,
}

# Supporting validators stay internal; only the final metric names are posted.
SOURCE_VALIDATORS = {'ticket_consistency': ticket_consistency, 'tool_arguments': tool_arguments, 'milk_total': milk_total}


def source_validation(turn: dict, session: list[dict]) -> dict[str, tuple[float, str]]:
    return {name: result for name, check in SOURCE_VALIDATORS.items() if (result := check(turn, session)) is not None}


def language_validation(turn: dict, session: list[dict]) -> str:
    findings = []
    for name, check in [('language_mixing', language_mixing), ('glossary_adherence', glossary_adherence)]:
        result = check(turn, session)
        if result is not None:
            findings.append(f'{name}: {result[1]}')
    return '\n'.join(findings)


def combine_source_alignment(judged: tuple[float | None, str], validators: dict[str, tuple[float, str]]) -> tuple[float | None, str]:
    score, reason = judged
    failures = [f'{name}: {evidence}' for name, (value, evidence) in validators.items() if value == 0]
    if failures:
        score = min(score, 2.0) if score is not None else 2.0
    evidence = [reason] if reason else []
    evidence.extend(f'{name}: {entry[1]}' for name, entry in validators.items())
    return score, ' '.join(evidence)


def ticket_translation(turn: dict, session: list[dict]) -> Result:
    """Exact returned ticket in English must survive into Gujarati; do not penalize untranslated source omissions."""
    english, gujarati = turn.get('answer_en') or '', turn.get('answer_gu') or ''
    changed = sorted(ticket for ticket in _session_tickets(turn, session) if ticket in english and ticket not in gujarati)
    if changed:
        return 0.0, f'English ticket {", ".join(changed)} is missing or changed in Gujarati.'
    return None
