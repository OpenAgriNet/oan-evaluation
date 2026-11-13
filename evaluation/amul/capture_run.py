"""Plan or sequentially capture #510's seed turns and link their exact trace IDs.

Planning is read-only. Capture creates app traces and dataset-run items, never
scores. It requires a fresh, separately verified runtime isolation report.
"""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from evaluation.amul.langfuse_io import Langfuse, build_turn, load_env_file

RUNTIME_CHECKS = (
    'turn_v1', 'suggestions_under_turn', 'trace_id_header',
    'private_reads_mocked', 'booking_writes_mocked', 'isolated_history',
    'loan_issuance_disabled', 'loan_sms_disabled', 'pm_kisan_otp_disabled_or_mocked',
)
ARTIFACT_START, ARTIFACT_END = '\n__CHAT_ARTIFACTS__', '__END_CHAT_ARTIFACTS__\n'


class CaptureError(RuntimeError):
    pass


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def snapshot(items: list[dict]) -> str:
    return digest(sorted(({k: item.get(k) for k in ('id', 'input', 'metadata', 'expectedOutput', 'status')}
                          for item in items), key=lambda item: item['id']))


def _url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise CaptureError('Use a complete chat URL without embedded credentials, query parameters or fragments')
    if not parsed.path.endswith('/api/chat/'):
        raise CaptureError('Chat URL must end in /api/chat/; redirects are not followed')
    return url


def build_plan(items: list[dict], *, dataset: str, run: str, chat_url: str,
               langfuse_host: str, release: str, capture_id: str | None = None,
               source_lang: str = 'gu', target_lang: str = 'gu') -> dict:
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,100}', run):
        raise CaptureError('Run name must contain only letters, numbers, dot, underscore or hyphen')
    if not re.fullmatch(r'[0-9a-f]{40}', release):
        raise CaptureError('Pin the expected app release to a full Git commit SHA')
    capture_id = capture_id or uuid.uuid4().hex
    if not re.fullmatch(r'[0-9a-f]{32}', capture_id):
        raise CaptureError('Capture ID must be 32 lowercase hex characters')
    if source_lang not in ('gu', 'en') or target_lang not in ('gu', 'en'):
        raise CaptureError('Seed capture supports gu/en language codes')
    sessions, seen_ids, seen_questions = {}, set(), set()
    for item in items:
        if item.get('status', 'ACTIVE') != 'ACTIVE':
            raise CaptureError('Dataset contains inactive items; resolve the capture scope explicitly')
        value = item.get('input')
        if not isinstance(value, dict):
            raise CaptureError('Seed item input must be an object')
        sid, qid, iid = value.get('session_id'), value.get('question_id'), item.get('id')
        if not all(isinstance(v, str) and v.strip() for v in (sid, qid, iid)):
            raise CaptureError('Every seed item needs nonempty session_id, question_id and dataset item ID')
        if len(f'amul-eval-{capture_id}-{sid}') > 200:
            raise CaptureError('Seed session ID is too long for a distinct Langfuse capture session')
        if iid in seen_ids or qid in seen_questions:
            raise CaptureError('Duplicate dataset item ID or question_id')
        seen_ids.add(iid)
        seen_questions.add(qid)
        match = re.fullmatch(re.escape(sid) + r'-(\d+)', qid)
        if not match or int(match[1]) < 1:
            raise CaptureError(f'Cannot establish numeric turn order for {qid}')
        question = value.get('question_gujarati' if source_lang == 'gu' else 'question_english')
        if not isinstance(question, str) or not question.strip():
            raise CaptureError(f'Missing capture-language question for {qid}')
        metadata = item.get('metadata') or {}
        sessions.setdefault(sid, []).append({
            'dataset_item_id': iid, 'question_id': qid, 'turn_index': int(match[1]),
            'question': question, 'category': metadata.get('category'), 'tool_hint': metadata.get('tool_hint'),
        })
    eligible, excluded = [], []
    for sid, turns in sorted(sessions.items()):
        turns.sort(key=lambda turn: turn['turn_index'])
        if [t['turn_index'] for t in turns] != list(range(1, len(turns) + 1)):
            raise CaptureError(f'Session {sid} has missing or duplicate turn numbers')
        ai = sid.startswith('ai-') or any(t['category'] == 'AI_technician_booking' or t['tool_hint'] == 'create_ai_call' for t in turns)
        if ai:
            excluded.append({'seed_session_id': sid, 'reason': 'AI booking excluded by #510 pending current mocks (#513)',
                             'question_ids': [t['question_id'] for t in turns]})
        else:
            eligible.append({'seed_session_id': sid, 'session_id': f'amul-eval-{capture_id}-{sid}', 'items': turns})
    if not eligible:
        raise CaptureError('No eligible turns')
    plan = {
        'schema_version': 'amul.capture-plan.v1', 'dataset': dataset, 'run_name': run,
        'capture_id': capture_id, 'chat_url': _url(chat_url), 'langfuse_host': langfuse_host.rstrip('/'),
        'expected_release': release, 'source_lang': source_lang, 'target_lang': target_lang,
        'dataset_snapshot_sha256': snapshot(items), 'sessions': eligible, 'excluded_sessions': excluded,
        'counts': {'total_items': len(items), 'eligible_turns': sum(len(s['items']) for s in eligible),
                   'eligible_sessions': len(eligible), 'excluded_turns': sum(len(s['question_ids']) for s in excluded),
                   'excluded_sessions': len(excluded)},
        'eligible_categories': dict(Counter(t['category'] or 'uncategorized' for s in eligible for t in s['items'])),
    }
    plan['plan_sha256'] = digest(plan)
    return plan


def check_plan(plan: dict) -> None:
    if plan.get('schema_version') != 'amul.capture-plan.v1' or digest({k: v for k, v in plan.items() if k != 'plan_sha256'}) != plan.get('plan_sha256'):
        raise CaptureError('Plan schema or digest is invalid; generate a fresh plan')
    _url(plan['chat_url'])


def check_runtime(plan: dict, report: dict, *, now: datetime | None = None) -> None:
    check_plan(plan)
    if report.get('schema_version') != 'amul.capture-runtime-check.v1':
        raise CaptureError('A verified capture-runtime-check.v1 report is required')
    for key, value in [('chat_url', plan['chat_url']), ('langfuse_host', plan['langfuse_host']), ('release', plan['expected_release'])]:
        if report.get(key) != value:
            raise CaptureError(f'Runtime check does not match plan {key}')
    try:
        verified = datetime.fromisoformat(report['verified_at_utc'].replace('Z', '+00:00'))
        age = ((now or datetime.now(timezone.utc)) - verified).total_seconds()
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise CaptureError('Runtime verification time is missing or invalid') from exc
    if not -60 <= age <= 3600:
        raise CaptureError('Runtime check must be verified within the last hour')
    missing = [name for name in RUNTIME_CHECKS if report.get('checks', {}).get(name) is not True]
    if missing or report.get('capture_allowed') is not True:
        raise CaptureError('Runtime not ready: ' + ', '.join(missing or report.get('blockers') or ['capture not allowed']))


def write_json(path: Path, value: dict, *, new: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if new:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
        return
    temporary = path.with_name(path.name + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
    temporary.replace(path)


@contextmanager
def journal_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + '.lock')
    try:
        fd = os.open(lock, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError as exc:
        raise CaptureError('Capture journal is locked by another process; inspect it before removing a stale lock') from exc
    os.close(fd)
    try:
        yield
    finally:
        lock.unlink()


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def chat_headers() -> dict:
    api_key = os.environ.get('SCENARIO_CHAT_API_KEY', '').strip()
    if api_key:
        phone = os.environ.get('SCENARIO_USER_PHONE', '').strip()
        if not phone:
            raise CaptureError('SCENARIO_USER_PHONE is required for API-key capture')
        return {'X-API-Key': api_key, 'X-User-Phone': phone}
    token = os.environ.get('TOKEN', '').strip()
    if not token:
        raise CaptureError('Set TOKEN or the eval-only SCENARIO_CHAT_API_KEY and SCENARIO_USER_PHONE')
    return {'Authorization': 'Bearer ' + token}


def send_chat(plan: dict, session: dict, item: dict, headers: dict) -> dict:
    params = {'query': item['question'], 'session_id': session['session_id'],
              'source_lang': plan['source_lang'], 'target_lang': plan['target_lang'],
              'user_id': 'anonymous', 'channel': 'web', 'stream': 'true', 'input_method': 'typed'}
    request = Request(plan['chat_url'] + '?' + urlencode(params), headers=headers, method='GET')
    start, first, chunks = time.perf_counter(), None, []
    try:
        with build_opener(NoRedirect()).open(request, timeout=180) as response:
            trace_id = response.headers.get('X-Trace-Id', '')
            if not re.fullmatch(r'[0-9a-f]{32}', trace_id):
                raise CaptureError('App did not return a valid X-Trace-Id; do not retry the question automatically')
            while chunk := response.read1(4096):
                if first is None:
                    first = time.perf_counter()
                chunks.append(chunk)
    except HTTPError as exc:
        raise CaptureError(f'Chat HTTP {exc.code}; no automatic question retry') from None
    except CaptureError:
        raise
    except Exception as exc:
        raise CaptureError(f'Chat transport failed ({type(exc).__name__}); outcome unknown, no automatic question retry') from None
    end = time.perf_counter()
    wire_text = b''.join(chunks).decode('utf-8')
    answer, marker, tail = wire_text.partition(ARTIFACT_START)
    if marker and (not tail.endswith(ARTIFACT_END) or ARTIFACT_START in tail):
        raise CaptureError('Malformed terminal artifact frame; inspect the turn before continuing')
    return {'trace_id': trace_id, 'answer': answer.strip(), 'artifact_frame_present': bool(marker),
            'client_first_body_byte_s': first - start if first is not None else None,
            'client_response_wall_s': end - start}


def verify_trace(raw: dict, plan: dict, session: dict, item: dict, response: dict) -> dict:
    metadata = raw.get('metadata') or {}
    if raw.get('id') != response['trace_id'] or raw.get('sessionId') != session['session_id']:
        raise CaptureError('Trace ID/session does not match the completed request')
    if raw.get('name') != 'turn' or metadata.get('amul.schema_version') != 'turn.v1' or metadata.get('amul.pipeline') != 'webapp':
        raise CaptureError('Trace is not a turn.v1 webapp turn; verify #445/#499 and the route')
    if metadata.get('amul.release') != plan['expected_release']:
        raise CaptureError('Recorded app release differs from the pinned capture build')
    if metadata.get('amul.source_lang') != plan['source_lang'] or metadata.get('amul.target_lang') != plan['target_lang']:
        raise CaptureError('Recorded languages differ from the capture plan')
    roots = [o for o in raw.get('observations') or [] if o.get('type') == 'SPAN' and o.get('name') == 'turn' and o.get('parentObservationId') is None]
    if len(roots) != 1:
        raise CaptureError('Expected one root turn span')
    if any(o.get('name') == 'suggestions' and o.get('parentObservationId') != roots[0].get('id') for o in raw.get('observations') or []):
        raise CaptureError('Suggestions is not a child of this turn; verify #499')
    if raw.get('input') != item['question'] or raw.get('output', '').strip() != response['answer']:
        raise CaptureError('Recorded question/answer differs from the request/response')
    if not response['answer'] or metadata.get('amul.outcome') in ('error', 'cancelled'):
        raise CaptureError('Turn failed or produced no answer; inspect before continuing the session')
    normalized = build_turn(raw, {'input': {'question_id': item['question_id']}})
    if normalized['latency_s'] is None:
        raise CaptureError('Root turn timing is incomplete')
    return {'reply_latency_s': normalized['latency_s'], 'release': metadata['amul.release'],
            'tool_names': [call['name'] for call in normalized['tool_calls']]}


def wait_for_trace(lf: Langfuse, plan: dict, session: dict, item: dict, response: dict,
                   *, timeout: float = 90) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        try:
            raw = lf.trace(response['trace_id'])
            return verify_trace(raw, plan, session, item, response)
        except HTTPError as exc:
            if exc.code != 404:
                raise CaptureError(f'Langfuse trace read failed: HTTP {exc.code}') from None
        except CaptureError:
            # Trace fields and root end-time can arrive in separate ingestion batches.
            if time.monotonic() >= deadline:
                raise
        if time.monotonic() >= deadline:
            raise CaptureError('Exact trace is not available in Langfuse; response saved, do not resend')
        time.sleep(2)


def _run(lf: Langfuse, plan: dict) -> dict | None:
    from urllib.parse import quote
    try:
        return lf.get(f"/api/public/datasets/{quote(plan['dataset'], safe='')}/runs/{quote(plan['run_name'], safe='')}")
    except HTTPError as exc:
        if exc.code == 404:
            return None
        raise CaptureError(f'Cannot read dataset run: HTTP {exc.code}') from None


def _reconcile_links(plan: dict, entries: dict, remote: list[dict], *, complete: bool = False) -> set[str]:
    expected = {t['dataset_item_id'] for s in plan['sessions'] for t in s['items']}
    linked = set()
    for row in remote:
        iid = row['datasetItemId']
        recorded = entries.get(iid, {}).get('response', {}).get('trace_id')
        if iid not in expected or iid in linked or not recorded or row.get('traceId') != recorded:
            raise CaptureError('Dataset run contains an unexpected, duplicate or mismatched trace link')
        linked.add(iid)
    if complete and linked != expected:
        raise CaptureError('Dataset run links do not cover every planned item')
    return linked


def capture(plan: dict, report: dict, lf: Langfuse, journal_path: Path, *, resume: bool = False,
            sender=send_chat, waiter=wait_for_trace) -> dict:
    check_runtime(plan, report)
    if lf.host != plan['langfuse_host']:
        raise CaptureError('Langfuse host differs from the capture plan')
    if snapshot(list(lf.dataset_items(plan['dataset']).values())) != plan['dataset_snapshot_sha256']:
        raise CaptureError('Dataset changed after planning; generate and review a fresh plan')
    headers = chat_headers()
    with journal_lock(journal_path):
        if journal_path.exists():
            if not resume:
                raise CaptureError('Journal exists; use --resume to reconcile without replaying completed questions')
            journal = json.loads(journal_path.read_text())
            if journal.get('plan_sha256') != plan['plan_sha256']:
                raise CaptureError('Journal belongs to a different plan')
        else:
            if resume:
                raise CaptureError('No journal to resume')
            journal = {'plan_sha256': plan['plan_sha256'], 'entries': {}, 'complete': False}
        remote = _run(lf, plan)
        if remote is not None and not resume:
            raise CaptureError('Run name already exists; choose a new name')
        entries = journal['entries']
        linked = _reconcile_links(plan, entries, (remote or {}).get('datasetRunItems') or [])
        for session in plan['sessions']:
            for item in session['items']:
                iid = item['dataset_item_id']
                entry = entries.get(iid)
                if iid in linked:
                    entry['state'] = 'linked'
                    continue
                if entry and entry['state'] in ('request_started', 'link_started', 'linked'):
                    raise CaptureError(f"Uncertain request/link state for {item['question_id']}; reconcile it manually, do not replay")
                if entry is None:
                    entry = entries[iid] = {'question_id': item['question_id'], 'session_id': session['session_id'], 'state': 'request_started'}
                    write_json(journal_path, journal)
                    entry['response'] = sender(plan, session, item, headers)
                    entry['state'] = 'response_received'
                    write_json(journal_path, journal)
                entry['trace'] = waiter(lf, plan, session, item, entry['response'])
                entry['state'] = 'link_started'
                write_json(journal_path, journal)
                lf._request('POST', '/api/public/dataset-run-items', body={
                    'runName': plan['run_name'], 'datasetItemId': iid, 'traceId': entry['response']['trace_id'],
                    'runDescription': 'Sequential turn.v1 seed capture for API #510; AI-booking sessions excluded; no scores posted.',
                    'metadata': {'capture_id': plan['capture_id'], 'capture_plan_sha256': plan['plan_sha256'],
                                 'expected_release': plan['expected_release'], 'sequential': True},
                })
                entry['state'] = 'linked'
                write_json(journal_path, journal)
        # Re-read rather than trusting POST responses as proof of complete linkage.
        _reconcile_links(plan, entries, (_run(lf, plan) or {}).get('datasetRunItems') or [], complete=True)
        journal['complete'] = True
        write_json(journal_path, journal)
        return journal


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    planner = sub.add_parser('plan', help='Read dataset and write an offline manifest; never call the app')
    planner.add_argument('--dataset', default='amul-seed-questions')
    planner.add_argument('--run', required=True)
    planner.add_argument('--chat-url', required=True)
    planner.add_argument('--expected-release', required=True)
    planner.add_argument('--source-lang', choices=['gu', 'en'], default='gu')
    planner.add_argument('--target-lang', choices=['gu', 'en'], default='gu')
    planner.add_argument('--output', type=Path, required=True)
    planner.add_argument('--expected-total', type=int, default=75)
    planner.add_argument('--expected-eligible', type=int, default=58)
    runner = sub.add_parser('capture', help='Send sequential app requests and create Langfuse dataset-run links')
    runner.add_argument('--plan', type=Path, required=True)
    runner.add_argument('--runtime-check', type=Path, required=True)
    runner.add_argument('--journal', type=Path, required=True)
    runner.add_argument('--resume', action='store_true')
    for command in (planner, runner):
        command.add_argument('--env-file')
    args = parser.parse_args()
    load_env_file(args.env_file)
    lf = Langfuse()
    if args.command == 'plan':
        plan = build_plan(list(lf.dataset_items(args.dataset).values()), dataset=args.dataset, run=args.run,
                          chat_url=args.chat_url, langfuse_host=lf.host, release=args.expected_release,
                          source_lang=args.source_lang, target_lang=args.target_lang)
        if plan['counts']['total_items'] != args.expected_total or plan['counts']['eligible_turns'] != args.expected_eligible:
            raise CaptureError('Dataset counts differ from expected scope; review before writing the plan')
        write_json(args.output, plan, new=True)
        print(json.dumps({'mode': 'plan-only; no app requests or Langfuse writes', 'counts': plan['counts'],
                          'eligible_categories': plan['eligible_categories'], 'plan_sha256': plan['plan_sha256']}))
    else:
        plan = json.loads(args.plan.read_text())
        result = capture(plan, json.loads(args.runtime_check.read_text()), lf, args.journal, resume=args.resume)
        print(json.dumps({'complete': result['complete'], 'linked_turns': len(result['entries']), 'scores_posted': False}))


if __name__ == '__main__':
    try:
        main()
    except CaptureError as exc:
        raise SystemExit(str(exc)) from None
