"""Milk totals from recorded tool output, preserving multi-account row counts."""
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from evaluation.amul.tool_evidence import outcome_evidence


class MilkEvidenceError(ValueError):
    """A requested numerical check needs evidence the trace cannot resolve."""


def _number(value: str) -> Decimal | None:
    try:
        result = Decimal(value.strip().replace(',', ''))
        return result if result.is_finite() else None
    except InvalidOperation:
        return None


def summary_totals(text: str) -> dict[int, Decimal | None]:
    """Explicit total columns in current English or common Gujarati summaries."""
    aliases = {
        0: {'total milk (l)', 'કુલ દૂધ (લિટર)', 'કુલ દૂધ (લીટર)'},
        1: {'total amount (rs)', 'કુલ રકમ (રૂ.)', 'કુલ રકમ (રૂપિયા)'},
    }
    headers, totals = {}, {}
    for line in text.splitlines():
        if not line.strip().startswith('|'):
            headers = {}
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        columns = {column: i for i, cell in enumerate(cells) for column, names in aliases.items() if cell.lower() in names}
        if columns:
            headers = columns
            continue
        if not headers or all(re.fullmatch(r':?-+:?', c) for c in cells):
            continue
        if totals:
            raise MilkEvidenceError('multiple milk summary rows without separate account identities')
        totals = {column: _number(cells[i].translate(str.maketrans('૦૧૨૩૪૫૬૭૮૯', '0123456789')))
                  if i < len(cells) else None for column, i in headers.items()}
    return totals


@dataclass
class Lookup:
    rows: list[tuple]
    summary: dict[int, Decimal | None]
    bounds: tuple[date, date] | None
    failed: bool


def _lookup(call: dict) -> Lookup:
    rows = []
    output = call.get('output')
    if not isinstance(output, str):
        raise MilkEvidenceError('milk output is not a supported text table')
    summary = summary_totals(output)
    detail_table = False
    for line in output.splitlines():
        if not line.strip().startswith('|'):
            detail_table = False
            continue
        cells = [c.strip() for c in line.strip().strip('|').split('|')]
        if cells[:3] == ['Date', 'Shift', 'Qty (L)']:
            detail_table = True
            continue
        if all(re.fullmatch(r':?-+:?', c) for c in cells):
            continue
        # The summary's period is not a detail-row date. Deductions have three
        # columns and must not become milk earnings. FAT/SNF may legitimately be -.
        if len(cells) != 6 or not re.fullmatch(r'\d{4}-\d{2}-\d{2}|\d{2}-\d{2}-\d{4}', cells[0]):
            if detail_table:
                raise MilkEvidenceError('unparsed milk detail row; cannot silently omit it')
            continue
        try:
            day = (date.fromisoformat(cells[0]) if len(cells[0].split('-')[0]) == 4
                   else datetime.strptime(cells[0], '%d-%m-%Y').date())
        except ValueError as exc:
            raise MilkEvidenceError('invalid milk detail date') from exc
        rows.append((day, cells[1], _number(cells[2]), _number(cells[5])))
    args = call.get('input') or {}
    bounds = None
    if isinstance(args, dict):
        try:
            start, end = date.fromisoformat(args['fromdate']), date.fromisoformat(args['todate'])
            if start <= end:
                bounds = (start, end)
        except (KeyError, TypeError, ValueError):
            pass
    outcome = outcome_evidence(call)
    failed = outcome.get('tool_status') in ('unavailable', 'error') or outcome.get('level') == 'ERROR'
    return Lookup(rows, summary, bounds, failed)


def _one_total(lookup: Lookup, column: int) -> Decimal:
    values = [r[column + 2] for r in lookup.rows]
    if any(v is None for v in values):
        raise MilkEvidenceError('requested quantity/amount is missing from milk records')
    if column in lookup.summary:
        value = lookup.summary[column]
        if value is None:
            raise MilkEvidenceError('requested summary total is missing or nonnumeric')
        if values and abs(sum(values, Decimal(0)) - value) > Decimal('0.005') * (len(values) + 1):
            raise MilkEvidenceError('milk summary conflicts with its detail rows beyond display rounding')
        return value
    if not values:
        raise MilkEvidenceError('requested quantity/amount is missing from milk records')
    return sum(values, Decimal(0))


def recorded_milk_totals(turn: dict, session: list[dict], columns: set[int]) -> tuple[dict[int, Decimal], str] | None:
    """Latest retrieval turn; disjoint ranges sum, overlapping records use multisets.

    Within one output, two identical rows may be different owned accounts. Across
    repeated lookups, retain the maximum occurrence count rather than summing the
    same returned records twice. Conflicting same-date/shift groups are ambiguous
    without account IDs, so no numerical score is certified for those overlaps.
    """
    calls = []
    for earlier in sorted(session, key=lambda t: t.get('turn_index', 0)):
        if earlier.get('turn_index', 0) > turn.get('turn_index', 0):
            continue
        current = [c for c in earlier.get('tool_calls') or [] if c.get('name') == 'get_farmer_milk_collection_details']
        if current:
            calls = current
    if not calls:
        return None
    lookups = [_lookup(c) for c in calls]
    if any(lookup.failed for lookup in lookups):
        raise MilkEvidenceError('latest milk retrieval includes a failed lookup; older records cannot certify this total')
    if not any(lookup.rows or lookup.summary for lookup in lookups):
        raise MilkEvidenceError('latest milk retrieval returned no usable records for the claimed total')
    if len(lookups) == 1:
        lookup = lookups[0]
        return {c: _one_total(lookup, c) for c in columns}, ('tool summary' if lookup.summary else f'{len(lookup.rows)} collection records')

    disjoint = all(a.bounds and b.bounds and (a.bounds[1] < b.bounds[0] or b.bounds[1] < a.bounds[0])
                   for i, a in enumerate(lookups) for b in lookups[i + 1:])
    if disjoint:
        return {c: sum((_one_total(lookup, c) for lookup in lookups), Decimal(0)) for c in columns}, 'tool totals for disjoint requested periods'

    merged, groups = Counter(), {}
    for lookup in lookups:
        if not lookup.rows:
            raise MilkEvidenceError('overlapping/unknown periods cannot be combined from summaries alone')
        current_groups = defaultdict(Counter)
        for row in lookup.rows:
            current_groups[row[:2]][row[2:]] += 1
        for key, records in current_groups.items():
            if key in groups and groups[key] != records:
                raise MilkEvidenceError('overlapping date/shift records differ; account identity or retrieval revision is unresolved')
            groups[key] = records
        # A summary may include records omitted from a partial detail export.
        for column in columns:
            values = [row[column + 2] for row in lookup.rows]
            if any(value is None for value in values):
                raise MilkEvidenceError('requested quantity/amount is missing from overlapping records')
            if column in lookup.summary:
                total = lookup.summary[column]
                rounding = Decimal('0.005') * (len(values) + 1)
                if total is None or abs(sum(values, Decimal(0)) - total) > rounding:
                    raise MilkEvidenceError('overlapping summary does not reconcile with its detail rows')
        merged |= Counter(lookup.rows)
    totals = {c: sum((row[c + 2] * count for row, count in merged.items()), Decimal(0)) for c in columns}
    return totals, f'{sum(merged.values())} collection records after overlapping-lookup deduplication'
