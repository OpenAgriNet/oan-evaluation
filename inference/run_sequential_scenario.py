#!/usr/bin/env python3
"""
Sequential scenario benchmark for english.csv using langwatch/scenario.

- Groups CSV rows by session_id
- Within each session questions are sent IN ORDER (truly sequential turns)
- Each turn calls the same agricultural chatbot API as run_chat_benchmark.py
  (GET {BASE_URL}/api/chat/ with Bearer token)
- The server keeps conversation history via the shared session_id, so each
  question is answered in context of all previous turns in that session
- Sessions run concurrently (bounded by MAX_CONCURRENT_SESSIONS)
- No user simulator LLM, no judge — CSV questions only
- Writes answer, TTFT_answer, latency_answer, tool_calls, tool_outputs to CSV

Usage:
    TOKEN=xxx BASE_URL=http://... python3 scenario_chat_english.py
    # or pass them as env vars in inf_bh_dev/.env
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import httpx
import scenario
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent
DEFAULT_ENV = ROOT / ".env"
load_dotenv(DEFAULT_ENV)

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# No hardcoded default on purpose — this box's vLLM containers include shared
# production instances (e.g. gemma4-text) that must never be hit without the
# user explicitly confirming that specific use, right then. Set both env vars
# yourself, each time. See oan-brain's knowledge/environments.md.
GEMMA_BASE_URL = os.environ["SCENARIO_GEMMA_BASE_URL"]
GEMMA_MODEL = os.environ["SCENARIO_GEMMA_MODEL"]
# Virtual key for the LiteLLM proxy. Direct vLLM accepts "dummy".
GEMMA_API_KEY = os.environ.get("LITELLM_API_KEY", "dummy")
QUESTION_COLUMN = os.environ.get("SCENARIO_QUESTION_COLUMN", "question")
SOURCE_LANG = os.environ.get("SCENARIO_SOURCE_LANG", "en")
TARGET_LANG = os.environ.get("SCENARIO_TARGET_LANG", "en")

INPUT_CSV = Path(os.environ.get("SCENARIO_INPUT_CSV", ROOT / "per_lang_csvs" / "english.csv"))
OUTPUT_CSV = Path(os.environ.get("SCENARIO_OUTPUT_CSV", ROOT / "output" / "sequential_scenario" / "english_scenario_chat.csv"))

MAX_CONCURRENT_SESSIONS = 10
SAVE_INTERVAL_SEC = 30.0
LANGFUSE_WAIT_SEC = 5.0

os.environ.setdefault("LITELLM_LOG", "ERROR")

# Configure Gemma as the framework's default model (only used if UserSimulatorAgent
# is accidentally triggered — won't be called in normal flow)
scenario.configure(default_model=GEMMA_MODEL)


# ---------------------------------------------------------------------------
# Streaming helper (mirrors run_chat_benchmark.py)
# ---------------------------------------------------------------------------
class StreamResult(NamedTuple):
    text: str
    ttft_ms: float
    total_ms: float


def chat_auth_headers(token: str) -> dict[str, str]:
    """Auth for one capture run.

    SCENARIO_CHAT_API_KEY is an eval-copy-only key (X-API-Key + X-User-Phone).
    It is not a farmer JWT, so it cannot authenticate to the shared app when
    that app has CHAT_API_KEY unset. Bearer TOKEN remains the live-app path.
    """
    api_key = os.environ.get("SCENARIO_CHAT_API_KEY", "").strip()
    if api_key:
        phone = os.environ.get("SCENARIO_USER_PHONE", "").strip()
        if not phone:
            raise SystemExit(
                "SCENARIO_USER_PHONE is required when SCENARIO_CHAT_API_KEY is set"
            )
        return {"X-API-Key": api_key, "X-User-Phone": phone}
    if not token:
        raise SystemExit("TOKEN required — set TOKEN in .env or pass --token")
    return {"Authorization": f"Bearer {token}"}


async def stream_chat_answer(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    query: str,
    source_lang: str,
    target_lang: str,
    user_id: str,
    session_id: str,
) -> StreamResult:
    params = {
        "query": query,
        "source_lang": source_lang,
        "target_lang": target_lang,
        "user_id": user_id,
        "session_id": session_id,
    }
    chunks: list[str] = []
    t_start = time.perf_counter()
    t_first: float | None = None
    async with client.stream("GET", url, headers=headers, params=params, timeout=600.0) as resp:
        async for text in resp.aiter_text():
            if t_first is None:
                t_first = time.perf_counter()
            chunks.append(text)
        body = "".join(chunks)
        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError as e:
            raise RuntimeError(f"HTTP {e.response.status_code}: {body[:2000]}") from e
    t_end = time.perf_counter()
    total_ms = (t_end - t_start) * 1000.0
    ttft_ms = (t_first - t_start) * 1000.0 if t_first is not None else total_ms
    return StreamResult(body.strip(), ttft_ms, total_ms)


async def fetch_langfuse_tool_calls(
    client: httpx.AsyncClient,
    *,
    langfuse_base_url: str,
    langfuse_public_key: str,
    langfuse_secret_key: str,
    session_id: str,
) -> list[dict]:
    await asyncio.sleep(LANGFUSE_WAIT_SEC)
    auth = (langfuse_public_key, langfuse_secret_key)
    try:
        resp = await client.get(
            f"{langfuse_base_url}/api/public/traces",
            params={"sessionId": session_id, "limit": 1},
            auth=auth,
            timeout=30.0,
        )
        resp.raise_for_status()
        traces = resp.json().get("data", [])
        if not traces:
            return []
        trace_id = traces[0]["id"]

        resp2 = await client.get(
            f"{langfuse_base_url}/api/public/observations",
            params={"traceId": trace_id, "type": "TOOL"},
            auth=auth,
            timeout=30.0,
        )
        resp2.raise_for_status()
        observations = resp2.json().get("data", [])

        async def fetch_one(obs: dict) -> dict:
            obs_id = obs.get("id", "")
            try:
                r = await client.get(
                    f"{langfuse_base_url}/api/public/observations/{obs_id}",
                    auth=auth,
                    timeout=30.0,
                )
                r.raise_for_status()
                full = r.json()
                return {
                    "name": full.get("name", ""),
                    "latency_ms": round((full.get("latency") or 0) * 1000, 1),
                    "input": full.get("input"),
                    "output": full.get("output"),
                    "trace_id": trace_id,
                    "observation_id": obs_id,
                }
            except Exception as e:
                return {"name": obs.get("name", ""), "error": str(e), "trace_id": trace_id}

        return list(await asyncio.gather(*(fetch_one(o) for o in observations)))
    except Exception as e:
        return [{"error": str(e)}]


# ---------------------------------------------------------------------------
# Agent adapter — calls the agricultural chatbot API
# ---------------------------------------------------------------------------
class ChatBotAgent(scenario.AgentAdapter):
    """
    Each session gets its own instance with a fixed api_session_id.
    The server tracks conversation history server-side via that id,
    so we only need to send the latest question each turn.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        headers: dict[str, str],
        api_session_id: str,
        source_lang: str = "en",
        target_lang: str = "en",
        user_id: str = "benchmark-user",
        langfuse_base_url: str = "",
        langfuse_public_key: str = "",
        langfuse_secret_key: str = "",
        lf_enabled: bool = False,
    ) -> None:
        self.client = client
        self.url = url
        self.headers = headers
        self.api_session_id = api_session_id
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.user_id = user_id
        self.langfuse_base_url = langfuse_base_url
        self.langfuse_public_key = langfuse_public_key
        self.langfuse_secret_key = langfuse_secret_key
        self.lf_enabled = lf_enabled

        # Per-turn results collected for the caller to read back
        self.results: list[dict] = []  # {"text", "ttft_ms", "total_ms", "tool_calls"}

    async def call(self, input: scenario.AgentInput) -> scenario.AgentReturnTypes:
        # Get the question injected by scenario.message()
        query = input.last_new_user_message_str()

        res = await stream_chat_answer(
            self.client,
            url=self.url,
            headers=self.headers,
            query=query,
            source_lang=self.source_lang,
            target_lang=self.target_lang,
            user_id=self.user_id,
            session_id=self.api_session_id,
        )

        tool_calls: list[dict] = []
        if self.lf_enabled:
            tool_calls = await fetch_langfuse_tool_calls(
                self.client,
                langfuse_base_url=self.langfuse_base_url,
                langfuse_public_key=self.langfuse_public_key,
                langfuse_secret_key=self.langfuse_secret_key,
                session_id=self.api_session_id,
            )

        self.results.append({
            "text": res.text,
            "ttft_ms": res.ttft_ms,
            "total_ms": res.total_ms,
            "tool_calls": tool_calls,
        })

        return res.text


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------
async def run_session(
    session_id: str,
    questions: list[dict],
    *,
    client: httpx.AsyncClient,
    url: str,
    headers: dict[str, str],
    run_uuid: str,
    sem: asyncio.Semaphore,
    source_lang: str = "en",
    target_lang: str = "en",
    langfuse_base_url: str = "",
    langfuse_public_key: str = "",
    langfuse_secret_key: str = "",
    lf_enabled: bool = False,
) -> list[dict]:
    """Run one session: questions go sequentially, server keeps conversation context."""
    sorted_qs = sorted(questions, key=lambda r: r["question_id"])

    # Unique session id per run (mirrors run_chat_benchmark.py's run_uuid scheme)
    api_session_id = f"{run_uuid}_{source_lang}_{session_id}"

    agent = ChatBotAgent(
        client=client,
        url=url,
        headers=headers,
        api_session_id=api_session_id,
        source_lang=source_lang,
        target_lang=target_lang,
        langfuse_base_url=langfuse_base_url,
        langfuse_public_key=langfuse_public_key,
        langfuse_secret_key=langfuse_secret_key,
        lf_enabled=lf_enabled,
    )

    # UserSimulatorAgent must be registered (framework requirement) even though
    # it is never called — we always inject explicit messages via scenario.message()
    user_agent = scenario.UserSimulatorAgent(
        model=GEMMA_MODEL,
        extra_litellm_params={"api_base": GEMMA_BASE_URL, "api_key": GEMMA_API_KEY},
    )

    script: list = []
    for row in sorted_qs:
        q = (row.get("question") or "").strip()
        if q:
            script.append(scenario.message({"role": "user", "content": q}))
            script.append(scenario.agent())
    script.append(scenario.succeed())

    async with sem:
        t0 = time.perf_counter()
        await scenario.arun(
            name=f"session_{session_id}",
            description=f"Agricultural Q&A session {session_id}",
            agents=[agent, user_agent],
            script=script,
            verbose=False,
        )
        elapsed = time.perf_counter() - t0

    # Pair each question row with the collected turn result
    output_rows = []
    for i, row in enumerate(sorted_qs):
        if i < len(agent.results):
            r = agent.results[i]
            tool_calls_json = json.dumps(r["tool_calls"], ensure_ascii=False) if r["tool_calls"] else ""
            tool_outputs_text = "\n\n".join(
                f"[{tc.get('name', '')}]\n{tc.get('output', '')}"
                for tc in r["tool_calls"]
                if isinstance(tc.get("output"), str)
            ) if r["tool_calls"] else ""
            output_rows.append({
                **row,
                "answer": r["text"],
                "tool_calls": tool_calls_json,
                "tool_outputs": tool_outputs_text,
                "TTFT_answer": f"{r['ttft_ms']:.2f}",
                "latency_answer": f"{r['total_ms']:.2f}",
            })
        else:
            output_rows.append({**row, "answer": "", "tool_calls": "", "tool_outputs": "",
                                 "TTFT_answer": "", "latency_answer": ""})

    n = len(sorted_qs)
    print(f"  session {session_id:>4}: {n} q(s) done in {elapsed:.1f}s [{api_session_id}]")
    return output_rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> None:
    load_dotenv(DEFAULT_ENV)

    token = (args.token or os.environ.get("TOKEN", "")).strip()
    headers = chat_auth_headers(token)

    base_url = (args.base_url or os.environ.get("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    # The shared Amul app publishes port 8000. An eval capture must not use it.
    if os.environ.get("SCENARIO_CHAT_API_KEY", "").strip() and base_url.endswith(":8000"):
        raise SystemExit("refusing capture: BASE_URL port 8000 is the shared app")
    url = f"{base_url}/api/chat/"

    langfuse_base_url = (os.environ.get("LANGFUSE_BASE_URL") or os.environ.get("LANGFUSE_HOST") or "").rstrip("/")
    langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    lf_enabled = bool(langfuse_base_url and langfuse_public_key and langfuse_secret_key)

    with INPUT_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [dict(r) for r in reader]

    # Add output columns if absent. `question` is filled from SCENARIO_QUESTION_COLUMN
    # when the source CSV only has language columns (question_gujarati, etc.).
    for col in ("question", "answer", "tool_calls", "tool_outputs", "TTFT_answer", "latency_answer"):
        if col not in fieldnames:
            fieldnames.append(col)

    sessions: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        text = (row.get("question") or row.get(QUESTION_COLUMN) or "").strip()
        row["question"] = text
        sessions[row["session_id"]].append(row)

    run_uuid = uuid.uuid4().hex[:8]
    print(f"Bot API: {url}")
    print(f"lang={SOURCE_LANG}->{TARGET_LANG}  question_column={QUESTION_COLUMN}")
    print(f"run_uuid={run_uuid}  rows={len(rows)}  sessions={len(sessions)}  "
          f"multi-turn={sum(1 for v in sessions.values() if len(v) > 1)}")
    print(f"Langfuse: {'enabled' if lf_enabled else 'disabled'}")
    print(f"Output: {OUTPUT_CSV}\n")

    sem = asyncio.Semaphore(args.concurrency)
    all_rows: list[dict] = []
    lock = asyncio.Lock()

    async def run_and_collect(sid: str, qs: list[dict]) -> None:
        try:
            result_rows = await run_session(
                sid, qs,
                client=client,
                url=url,
                headers=headers,
                run_uuid=run_uuid,
                sem=sem,
                source_lang=SOURCE_LANG,
                target_lang=TARGET_LANG,
                langfuse_base_url=langfuse_base_url,
                langfuse_public_key=langfuse_public_key,
                langfuse_secret_key=langfuse_secret_key,
                lf_enabled=lf_enabled,
            )
            async with lock:
                all_rows.extend(result_rows)
        except Exception as e:
            print(f"  session {sid}: FAILED — {e}")
            async with lock:
                for row in qs:
                    all_rows.append({**row, "answer": f"ERROR: {e}",
                                     "tool_calls": "", "tool_outputs": "",
                                     "TTFT_answer": "", "latency_answer": ""})

    async def periodic_save() -> None:
        while True:
            await asyncio.sleep(SAVE_INTERVAL_SEC)
            async with lock:
                snapshot = list(all_rows)
            _write_csv(OUTPUT_CSV, fieldnames, snapshot)
            print(f"  [checkpoint] saved {len(snapshot)}/{len(rows)} rows")

    limits = httpx.Limits(
        max_keepalive_connections=args.concurrency + 4,
        max_connections=args.concurrency + 4,
    )
    t_start = time.perf_counter()
    async with httpx.AsyncClient(limits=limits) as client:
        save_task = asyncio.create_task(periodic_save())
        await asyncio.gather(*(run_and_collect(sid, qs) for sid, qs in sessions.items()))
        save_task.cancel()
        try:
            await save_task
        except asyncio.CancelledError:
            pass

    order = {r["question_id"]: i for i, r in enumerate(rows)}
    all_rows.sort(key=lambda r: order.get(r["question_id"], 9999))
    _write_csv(OUTPUT_CSV, fieldnames, all_rows)
    elapsed = time.perf_counter() - t_start
    print(f"\nDone. {len(all_rows)} rows written to {OUTPUT_CSV} in {elapsed:.1f}s")

    print("\n--- Sample outputs ---")
    good = [r for r in all_rows if r.get("answer") and not r["answer"].startswith("ERROR")]
    for row in good[:5]:
        print(f"\nQ [{row['session_id']}] {row['question'][:80]}")
        print(f"A: {row['answer'][:300]}")
        if row.get("TTFT_answer"):
            print(f"   TTFT={row['TTFT_answer']}ms  latency={row['latency_answer']}ms")
        print("-" * 60)


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def main() -> None:
    ap = argparse.ArgumentParser(description="Scenario-based sequential chat benchmark (English)")
    ap.add_argument("--token", default="", help="Bearer token (default: env TOKEN)")
    ap.add_argument("--base-url", default="", help="API base URL (default: env BASE_URL)")
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENT_SESSIONS,
                    help="max concurrent sessions")
    args = ap.parse_args()
    asyncio.run(async_main(args))


if __name__ == "__main__":
    main()
