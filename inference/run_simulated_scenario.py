#!/usr/bin/env python3
"""
Multi-turn scenario benchmark: Gemma simulates the user, agricultural chatbot API answers.

Per session (grouped by session_id in the CSV):
  - Turn 1 : exact question from the CSV  → chatbot API  → answer
  - Turns 2+: Gemma generates realistic follow-ups based on the bot's last answer
              → chatbot API → answer  (repeated for N turns total, where N = rows in that session)

Single-question sessions: just Q1 → bot (no Gemma needed for user simulation).
Multi-turn sessions (N>1): Q1 (CSV) → bot A1 → Gemma Q2 → bot A2 → Gemma Q3 → …

The agricultural chatbot API keeps conversation context server-side via session_id.
Gemma is ONLY used to generate user-side follow-up messages; it never answers questions.

env / .env (inference/.env): TOKEN, BASE_URL
Voice: --api-kind voice --session 901 --languages English --concurrency 1
uses POST /api/v1/chat/completions and times first nonempty spoken-text snapshot.
"""
from __future__ import annotations

import argparse
import asyncio
import csv
import json
import re
import os
import time
import uuid
from collections import defaultdict
from pathlib import Path
from typing import NamedTuple

import httpx
import litellm
import scenario
from scenario.types import AgentRole
from dotenv import load_dotenv
from tenacity import retry, stop_after_attempt, wait_fixed, retry_if_exception_type

ROOT = Path(__file__).resolve().parent
DEFAULT_ENV = ROOT / ".env"

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------
# No hardcoded default on purpose — see oan-brain's knowledge/environments.md
# and feedback_vllm_docker_care.md: never point this at a shared GPU endpoint
# (e.g. gemma4-text, a production instance) without confirming that specific
# use, each time.
GEMMA_BASE_URL = os.environ["SCENARIO_GEMMA_BASE_URL"]
GEMMA_MODEL = os.environ["SCENARIO_GEMMA_MODEL"]

INPUT_CSV  = Path(os.environ.get("SCENARIO_INPUT_CSV", ROOT / "benchmark_questions.csv"))
OUTPUT_DIR = Path(os.environ.get("SCENARIO_OUTPUT_DIR", ROOT / "output" / "simulated_scenario"))

# ---------------------------------------------------------------------------
# Language passes — mirrors DEFAULT_PASSES in run_chat_benchmark.py.
# input_column : column in INPUT_CSV that holds questions for this language.
# output        : filename (in OUTPUT_DIR) for this language's result CSV.
# Comment out any passes you don't want to run.
# ---------------------------------------------------------------------------
LANGUAGE_PASSES = [
    {"input_column": "question_english",  "language": "English",   "source_lang": "en", "target_lang": "en", "output": "english_chatgemma_scenario.csv"},
    {"input_column": "question_hindi",    "language": "Hindi",     "source_lang": "hi", "target_lang": "hi", "output": "hindi_chatgemma_scenario.csv"},
    {"input_column": "question_marathi",  "language": "Marathi",   "source_lang": "mr", "target_lang": "mr", "output": "marathi_chatgemma_scenario.csv"},
    {"input_column": "question_gujarati", "language": "Gujarati",  "source_lang": "gu", "target_lang": "gu", "output": "gujarati_chatgemma_scenario.csv"},
    {"input_column": "question_malayalam","language": "Malayalam", "source_lang": "ml", "target_lang": "ml", "output": "malayalam_chatgemma_scenario.csv"},
    {"input_column": "question_kannada",  "language": "Kannada",   "source_lang": "kn", "target_lang": "kn", "output": "kannada_chatgemma_scenario.csv"},
    {"input_column": "question_tamil",    "language": "Tamil",     "source_lang": "ta", "target_lang": "ta", "output": "tamil_chatgemma_scenario.csv"},
    {"input_column": "question_telugu",   "language": "Telugu",    "source_lang": "te", "target_lang": "te", "output": "telugu_chatgemma_scenario.csv"},
    {"input_column": "question_bengali",  "language": "Bengali",   "source_lang": "bn", "target_lang": "bn", "output": "bengali_chatgemma_scenario.csv"},
    #{"input_column": "question_odia",     "language": "Odia",      "source_lang": "or", "target_lang": "or", "output": "odia_chatgemma_scenario.csv"},
    {"input_column": "question_assamese", "language": "Assamese",  "source_lang": "as", "target_lang": "as", "output": "assamese_chatgemma_scenario.csv"},
    {"input_column": "question_odia", "language": "Odia", "source_lang": "or", "target_lang": "or", "output": "odia_chatgemma_scenario.csv"},
    {"input_column": "question_punjabi", "language": "Punjabi", "source_lang": "pa", "target_lang": "pa", "output": "punjabi_chatgemma_scenario.csv"},
    {"input_column": "question_maithili", "language": "Maithili", "source_lang": "mai", "target_lang": "mai", "output": "maithili_chatgemma_scenario.csv"},
]


def language_passes(api_kind: str) -> list[dict]:
    if api_kind == 'text':
        return [dict(p) for p in LANGUAGE_PASSES]
    return [dict(p, source_lang='od' if p['source_lang'] == 'or' else p['source_lang'],
                 target_lang='od' if p['target_lang'] == 'or' else p['target_lang'],
                 output=f"{p['language'].lower()}_voice_scenario.csv")
            for p in LANGUAGE_PASSES if p['source_lang'] not in ('as', 'mai')]

MAX_CONCURRENT_SESSIONS = 20
SAVE_INTERVAL_SEC = 30.0
LANGFUSE_WAIT_SEC = 5.0

os.environ.setdefault("LITELLM_LOG", "ERROR")
os.environ.setdefault("OPENAI_API_KEY", "dummy")  # litellm requires this even for local endpoints
litellm.suppress_debug_info = True
litellm.drop_params = True  # vLLM rejects empty tools:[] — drop unsupported params silently

# Gemma is the default model — used by UserSimulatorAgent to generate follow-ups
scenario.configure(default_model=GEMMA_MODEL)


# ---------------------------------------------------------------------------
# Streaming helper (mirrors run_chat_benchmark.py)
# ---------------------------------------------------------------------------
class TurnResult(NamedTuple):
    text: str
    ttft_ms: float
    total_ms: float


async def stream_voice_answer(client, *, url, token, query, session_id, language, tenant_id):
    """Time first nonempty spoken text, not SSE metadata or the JSON envelope.

    Voice emits complete cumulative JSON snapshots, NOT append-only text deltas.
    This is first user-facing audio text, not guaranteed post-tool final-answer TTFT.
    Session/tool history is maintained by the voice API in Redis.
    """
    headers = {"X-Tenant-ID": tenant_id, "X-User-ID": "benchmark-user",
               "X-Session-ID": session_id, "X-Language": language}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    start = time.perf_counter()
    first = None
    answer = ""
    done = False
    async with client.stream("POST", url, headers=headers,
            json={"messages": [{"role": "user", "content": query}], "stream": True},
            timeout=600.0) as response:
        if response.is_error:
            await response.aread()
            response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                done = True
                break
            event = json.loads(payload)
            if "error" in event:
                raise RuntimeError(f"Voice stream error: {event['error']}")
            for choice in event.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if not content:
                    continue
                snapshot = json.loads(content)
                audio = snapshot.get("audio")
                if not isinstance(audio, str):
                    raise ValueError("Voice snapshot must contain an audio string")
                answer = audio
                if first is None and audio.strip():
                    first = time.perf_counter()
    end = time.perf_counter()
    if not done or first is None or not answer.strip():
        raise RuntimeError("Voice stream ended without DONE and a nonempty spoken answer")
    return TurnResult(answer.strip(), (first-start)*1000, (end-start)*1000)


@retry(
    stop=stop_after_attempt(3),
    wait=wait_fixed(2),
    retry=retry_if_exception_type((httpx.RemoteProtocolError, httpx.ReadError, httpx.ConnectError)),
)
async def stream_chat_answer(
    client: httpx.AsyncClient,
    *,
    url: str,
    token: str,
    query: str,
    session_id: str,
    source_lang: str = "en",
    target_lang: str = "en",
    user_id: str = "benchmark-user",
) -> TurnResult:
    headers = {"Authorization": f"Bearer {token}"}
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
    return TurnResult(
        text=body.strip(),
        ttft_ms=(t_first - t_start) * 1000.0 if t_first else (t_end - t_start) * 1000.0,
        total_ms=(t_end - t_start) * 1000.0,
    )


# ---------------------------------------------------------------------------
# Langfuse — fetch ALL tool calls for the full session (all turns combined)
# ---------------------------------------------------------------------------
async def fetch_session_tool_calls(
    client: httpx.AsyncClient,
    *,
    langfuse_base_url: str,
    langfuse_public_key: str,
    langfuse_secret_key: str,
    api_session_id: str,
    wait_sec: float = LANGFUSE_WAIT_SEC,
) -> tuple[list[dict], str]:
    """Return (tool_calls_list, tool_outputs_text) for every tool used in the session."""
    await asyncio.sleep(wait_sec)
    auth = (langfuse_public_key, langfuse_secret_key)
    try:
        # Get ALL traces for this session (one per turn)
        resp = await client.get(
            f"{langfuse_base_url}/api/public/traces",
            params={"sessionId": api_session_id, "limit": 100},
            auth=auth,
            timeout=30.0,
        )
        resp.raise_for_status()
        traces = resp.json().get("data", [])
        if not traces:
            return [], ""

        # Collect tool observations across all traces
        all_observations: list[dict] = []
        for trace in traces:
            trace_id = trace["id"]
            r2 = await client.get(
                f"{langfuse_base_url}/api/public/observations",
                params={"traceId": trace_id, "type": "TOOL"},
                auth=auth,
                timeout=30.0,
            )
            r2.raise_for_status()
            obs_data = r2.json().get("data", [])
            for obs in obs_data:
                obs["_trace_id"] = trace_id
            all_observations.extend(obs_data)

        async def fetch_one(obs: dict) -> dict:
            obs_id = obs.get("id", "")
            trace_id = obs.get("_trace_id", "")
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
                return {"name": obs.get("name", ""), "error": str(e),
                        "trace_id": trace_id, "observation_id": obs_id}

        tool_calls = list(await asyncio.gather(*(fetch_one(o) for o in all_observations)))

        tool_outputs = "\n\n".join(
            f"[{tc.get('name', '')}]\n{tc.get('output', '')}"
            for tc in tool_calls
            if isinstance(tc.get("output"), str)
        )
        return tool_calls, tool_outputs

    except Exception as e:
        return [{"error": str(e)}], ""


# ---------------------------------------------------------------------------
# Custom user simulator — calls Gemma with only conversation history, no tools
# ---------------------------------------------------------------------------
class GemmaUserSimulator(scenario.AgentAdapter):
    """
    Custom user simulator. Calls Gemma with only conversation history — no tools field.
    Generates follow-up questions strictly grounded in the original CSV question facts.
    """
    role = AgentRole.USER

    def __init__(self, description: str, all_questions: list[str], language: str) -> None:
        self.description = description
        self.all_questions = all_questions
        self.language = language

    async def call(self, input: scenario.AgentInput) -> scenario.AgentReturnTypes:
        facts_block = "\n".join(f"- {q}" for q in self.all_questions)
        system = f"""You are simulating an Indian farmer talking to an agricultural assistant.
You must reply ONLY in {self.language}.

GOAL: {self.description}

FACTS FROM THE ORIGINAL QUESTIONS (you must stay strictly within these):
{facts_block}

STRICT RULES:
- Use ONLY the crop names, locations, scheme names, numbers, and details that appear in the facts above. Do NOT invent or substitute any new facts.
- Cover the facts progressively across turns — do not repeat what you already asked.
- Base your next message on what the assistant just said, responding naturally.
- Keep it short: a few words to one sentence, lowercase, conversational.
- Reply ONLY with the next farmer message. No explanation, no quotes, nothing else.
- If the assistant asks for an OTP, reply with the canonical test OTP:
  PM-KISAN uses 4 digits -> reply 1111; PMFBY uses 6 digits -> reply 111111.
  (Both are accepted by mock_server; prod OTP lengths confirmed from Langfuse.)"""

        history = [
            {"role": m["role"], "content": m.get("content") or ""}
            for m in input.messages
            if m.get("content")
        ]
        response = await litellm.acompletion(
            model=GEMMA_MODEL,
            messages=[{"role": "system", "content": system}] + history,
            api_base=GEMMA_BASE_URL,
            api_key="dummy",
            temperature=0.5,
            max_tokens=150,
        )
        message = (response.choices[0].message.content or "").strip()
        if not message:
            raise RuntimeError("User simulator returned an empty message; refusing an invalid bot turn")
        return message


# ---------------------------------------------------------------------------
# Agent adapter — calls the agricultural chatbot API
# Bot answers every turn; Gemma generates the user's follow-up messages.
# ---------------------------------------------------------------------------
class ChatBotAgent(scenario.AgentAdapter):
    """
    Wraps the agricultural chatbot API. One instance per session, fixed api_session_id
    so the server maintains conversation context across all turns.
    Per-turn results (text, latency) are appended to self.turns for the caller to read.
    """

    def __init__(
        self,
        *,
        client: httpx.AsyncClient,
        url: str,
        token: str,
        api_session_id: str,
        source_lang: str = "en",
        target_lang: str = "en",
        api_kind: str = "text",
        tenant_id: str = "eval",
    ) -> None:
        self.client = client
        self.url = url
        self.token = token
        self.api_session_id = api_session_id
        self.source_lang = source_lang
        self.target_lang = target_lang
        self.api_kind = api_kind
        self.tenant_id = tenant_id
        self.turns: list[TurnResult] = []
        self.answered_queries: list[str] = []
        self.last_query: str = ""

    async def call(self, input: scenario.AgentInput) -> scenario.AgentReturnTypes:
        query = input.last_new_user_message_str()
        self.last_query = query
        if self.api_kind == "voice":
            result = await stream_voice_answer(
                self.client, url=self.url, token=self.token, query=query,
                session_id=self.api_session_id, language=self.target_lang,
                tenant_id=self.tenant_id,
            )
        else:
            result = await stream_chat_answer(
                self.client,
                url=self.url,
                token=self.token,
                query=query,
                session_id=self.api_session_id,
                source_lang=self.source_lang,
                target_lang=self.target_lang,
            )
        self.turns.append(result)
        self.answered_queries.append(query)
        return result.text


# ---------------------------------------------------------------------------
# Session runner
# ---------------------------------------------------------------------------
FARMER_SYSTEM = """You play an Indian farmer chatting with an agricultural assistant. Write ONLY in {language}.
Today is {today} (India time). Use it if a date matters; never pick a different "today".

WHAT YOU KNOW (facts; never invent anything else):
{facts}

YOUR NEXT MESSAGE TO START (only when the assistant is not waiting for an answer from you): {next_message}

Look at the assistant's LAST reply and choose ONE action:
- "answer": the assistant asks you something (a detail, a choice, a yes/no confirmation). Write a short, natural
  answer from your facts. When a fact says "give exactly", use that exact value this time. If your facts do not
  have it, say you do not know or that you only want what you asked. To a confirmation of what you asked for, say
  yes. List in "used" the fact names your answer gives.
- "next": the assistant is not waiting for anything from you (it answered, or only offers more help). Your next
  message will be sent.
- "stop": you have no next message and the assistant needs nothing more from you to finish what you asked.

Reply with JSON only, one of: {{"action": "answer", "message": "...", "used": ["fact_name", ...]}}
{{"action": "next"}} {{"action": "stop"}}"""

MAX_WRITTEN_REPLIES_PER_MESSAGE = 15  # loop guard only: a full FFS booking needs ~9 replies after one message
MAX_EXTRA_TURNS = 15
SIM_SPECS = json.loads(Path(os.environ["SIM_SPECS"]).read_text()) if os.environ.get("SIM_SPECS") else {}


def render_facts(facts, progress):
    """A plain fact is always the answer. {"value": real, "in_order": [...]} is the real value plus what to give
    each time the bot asks (e.g. a partial OTP first); once the sequence is used up, always the real value."""
    lines = []
    for key, value in facts.items():
        if isinstance(value, dict) and "in_order" in value:
            seq, real = value["in_order"], value.get("value", value["in_order"][-1])
            i = progress.get(key, 0)
            give = seq[i] if i < len(seq) else real
            lines.append(f"- {key}: {real} (your real {key}). When asked for it, answer in this order: "
                         f"{' then '.join(repr(v) for v in seq)}; after that always {real!r}. "
                         f"Next time you are asked, give exactly {give!r}.")
        else:
            lines.append(f"- {key}: {value}")
    return "\n".join(lines) or "(none)"


async def decide_farmer_turn(language, facts, progress, next_message, history):
    from datetime import datetime, timedelta, timezone
    today = datetime.now(timezone(timedelta(hours=5, minutes=30))).strftime('%A, %d %B %Y')
    system = FARMER_SYSTEM.format(language=language, today=today, facts=render_facts(facts, progress),
                                  next_message=next_message or "(none: you have sent all your messages)")
    response = await litellm.acompletion(
        model=GEMMA_MODEL, api_base=GEMMA_BASE_URL, api_key="dummy", temperature=0, max_tokens=250,
        messages=[{"role": "system", "content": system}] + history[-8:])
    raw = (response.choices[0].message.content or "").strip()
    match = re.search(r"\{.*\}", raw, re.S)
    try:
        d = json.loads(match.group(0)) if match else {}
    except json.JSONDecodeError:
        d = {}
    if d.get("action") == "answer" and (d.get("message") or "").strip():
        used = [k for k in (d.get("used") or []) if isinstance(k, str) and k in facts]
        return {"action": "answer", "message": d["message"].strip(), "used": used}
    if d.get("action") == "stop" and not next_message:
        return {"action": "stop"}
    return {"action": "next"} if next_message else {"action": "stop"}


async def run_goal_session(session_id, sorted_rows, *, bot_agent, language, sem):
    """Simulated farmer from a two-key profile (sim_specs.json; user design 2026-11-24).

    user_messages: what the farmer starts by itself, in order (question ids; sent verbatim in the session language).
    facts: what the farmer knows. A plain value is always the answer; {"in_order": [...]} gives the values one per
    time the bot asks (e.g. a partial OTP first, then the right one). Python tracks that progress.
    Everything between user messages is written by the farmer from the facts. Written replies get question_id
    "<current user message id>.r<k>" and are judged with that message's unit.
    """
    spec = SIM_SPECS[session_id]
    by_id = {r["question_id"]: r for r in sorted_rows}
    text = lambda qid: by_id[qid]["question"].strip()
    pending = list(spec["user_messages"])
    facts = spec.get("facts", {})
    progress = {}
    history, turns = [], []
    failure, replies, current = "", 0, None
    budget = len(sorted_rows) + MAX_EXTRA_TURNS

    async def ask(message, row, qid, kind):
        try:
            result = await bot_agent.call(_StaticInput(message))
        except Exception as exc:
            # A failed written reply has its own id. Retain it even if the
            # originating planned question already has a successful answer.
            turns.append(dict(row=row, question_id=qid, kind=kind, question=message,
                              turn=len(turns) + 1,
                              result=TurnResult(f"ERROR: {type(exc).__name__}: {exc}", 0, 0)))
            raise
        history.extend([{"role": "user", "content": message}, {"role": "assistant", "content": result}])
        turns.append(dict(row=row, question_id=qid, kind=kind, question=message, turn=len(turns) + 1,
                          result=bot_agent.turns[-1]))

    async with sem:
        try:
            current = pending.pop(0)
            await ask(text(current), by_id[current], current, "planned")
            while len(turns) < budget:
                d = await decide_farmer_turn(language, facts, progress, text(pending[0]) if pending else "", history)
                if d["action"] == "answer" and replies < MAX_WRITTEN_REPLIES_PER_MESSAGE:
                    replies += 1
                    for key in d["used"]:
                        if isinstance(facts[key], dict) and "in_order" in facts[key]:
                            progress[key] = progress.get(key, 0) + 1
                    await ask(d["message"], by_id[current], f"{current}.r{replies}", "reply")
                    continue
                if not pending:
                    break
                current = pending.pop(0)
                replies = 0
                await ask(text(current), by_id[current], current, "planned")
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            print(f"  session {session_id}: stopped after {len(turns)} turns — {failure}")
    output = [{"session_id": t["row"]["session_id"], "question_id": t["question_id"],
               "planned_question_id": t["row"]["question_id"], "turn": t["turn"], "kind": t["kind"],
               "category": t["row"].get("category", ""), "question": t["question"], "answer": t["result"].text,
               "tool_calls": "", "tool_outputs": "", "TTFT_answer": f"{t['result'].ttft_ms:.2f}",
               "latency_answer": f"{t['result'].total_ms:.2f}"} for t in turns]
    for qid in pending + ([current] if failure and current not in {t["question_id"] for t in turns} else []):
        row = by_id[qid]  # user messages never sent after a failure
        output.append({"session_id": row["session_id"], "question_id": qid, "planned_question_id": qid,
                       "turn": len(output) + 1, "kind": "planned", "category": row.get("category", ""),
                       "question": row["question"], "answer": f"ERROR: {failure or 'not reached'}",
                       "tool_calls": "", "tool_outputs": "", "TTFT_answer": "", "latency_answer": ""})
    unused = {k: v["in_order"][progress.get(k, 0):] for k, v in facts.items()
              if isinstance(v, dict) and progress.get(k, 0) < len(v["in_order"])}
    print(f"  session {session_id:>4}: {len(turns)} turns ({sum(t['kind'] == 'reply' for t in turns)} written "
          f"replies; in-order facts not fully used: {unused or 'none'})")
    return output


def current_of(turn):
    """Written replies belong to the request they were answering (their row is that request's row)."""
    return turn["row"]["question_id"]


class _StaticInput:
    """Minimal stand-in for scenario.AgentInput: ChatBotAgent only reads the last user message."""
    def __init__(self, text):
        self.text = text

    def last_new_user_message_str(self):
        return self.text


async def run_session(
    session_id: str,
    rows: list[dict],
    *,
    client: httpx.AsyncClient,
    url: str,
    token: str,
    run_uuid: str,
    sem: asyncio.Semaphore,
    language: str = "English",
    source_lang: str = "en",
    target_lang: str = "en",
    langfuse_base_url: str = "",
    langfuse_public_key: str = "",
    langfuse_secret_key: str = "",
    lf_enabled: bool = False,
    api_kind: str = "text",
    tenant_id: str = "eval",
    fixed_questions: bool = False,
    goal_driven: bool = False,
) -> list[dict]:
    """
    Run one session. Questions are sequential (scenario enforces this).
    Turn 1 uses the exact CSV question; turns 2+ are generated by Gemma
    based on the bot's previous answer and the session description.
    """
    sorted_rows = sorted(rows, key=lambda r: int(r["question_id"].rsplit("-", 1)[-1]))
    n_turns = len(sorted_rows)
    if goal_driven and n_turns > 1:
        bot = ChatBotAgent(client=client, url=url, token=token, api_session_id=f"{run_uuid}_{target_lang}_{session_id}",
                           source_lang=source_lang, target_lang=target_lang, api_kind=api_kind, tenant_id=tenant_id)
        consent = sorted_rows[0].get('agristack_consent', '')
        if api_kind == 'text' and consent in ('true', 'false'):
            response = await client.post(url.rsplit('/chat/', 1)[0] + '/callback', json={
                'callbackSessionId': bot.api_session_id, 'stateLgdCode': '10',
                'consentName': 'CONTINUE WITH CONSENT' if consent == 'true' else 'CONTINUE WITHOUT CONSENT',
                'data': {'farmerData': {'centralId': 'SYNTHETIC-BENCHMARK-FARMER'}},
            })
            response.raise_for_status()
        return await run_goal_session(session_id, sorted_rows, bot_agent=bot, language=language, sem=sem)
    all_questions = [r["question"].strip() for r in sorted_rows]
    q1 = all_questions[0]
    category = sorted_rows[0].get("category", "")

    description = (
        f"Goal from turn 1: {q1}. Category: {category}. "
        f"Continue the conversation naturally, covering all the facts in the questions list."
    )

    api_session_id = f"{run_uuid}_{target_lang}_{session_id}"

    bot_agent = ChatBotAgent(
        client=client,
        url=url,
        token=token,
        api_session_id=api_session_id,
        source_lang=source_lang,
        target_lang=target_lang,
        api_kind=api_kind,
        tenant_id=tenant_id,
    )

    # Gemma simulates the user for follow-up turns (turn 2 onwards)
    gemma_user = GemmaUserSimulator(
        description=description,
        all_questions=all_questions,
        language=language,
    )

    # Script:
    # - Turn 1: inject Q1 from CSV exactly, then bot answers
    # - Turns 2..N: Gemma generates follow-up, then bot answers
    script: list = [
        scenario.message({"role": "user", "content": q1}),
        scenario.agent(),
    ]
    for question in all_questions[1:]:
        script.append(scenario.message({"role": "user", "content": question})
                      if fixed_questions else scenario.user())
        script.append(scenario.agent())  # bot answers
    script.append(scenario.succeed())

    async with sem:
        consent = sorted_rows[0].get('agristack_consent', '')
        if api_kind == 'text' and consent in ('true', 'false'):
            response = await client.post(url.rsplit('/chat/', 1)[0] + '/callback', json={
                'callbackSessionId': api_session_id, 'stateLgdCode': '10',
                'consentName': 'CONTINUE WITH CONSENT' if consent == 'true' else 'CONTINUE WITHOUT CONSENT',
                'data': {'farmerData': {'centralId': 'SYNTHETIC-BENCHMARK-FARMER'}},
            })
            response.raise_for_status()
        t0 = time.perf_counter()
        failure = ""
        result = None
        try:
            result = await scenario.arun(
                name=f"session_{session_id}",
                description=description,
                agents=[bot_agent, gemma_user],
                script=script,
                verbose=False,
            )
        except Exception as exc:
            failure = f"{type(exc).__name__}: {exc}"
            print(f"  session {session_id}: stopped after {len(bot_agent.turns)}/{n_turns} answers — {failure}")
        elapsed = time.perf_counter() - t0

    # Fetch all tool calls for the full session from Langfuse (assigned to last row)
    session_tool_calls: list[dict] = []
    session_tool_outputs: str = ""
    if lf_enabled:
        session_tool_calls, session_tool_outputs = await fetch_session_tool_calls(
            client,
            langfuse_base_url=langfuse_base_url,
            langfuse_public_key=langfuse_public_key,
            langfuse_secret_key=langfuse_secret_key,
            api_session_id=api_session_id,
        )

    # Extract the actual user messages Gemma sent (turn 2+) from result.messages
    # Conversation order: user, assistant, user, assistant, ...
    all_msgs = list(result.messages) if result is not None else []
    user_msgs = [m for m in all_msgs if m.get("role") == "user"]
    # user_msgs[0] = Q1 (from CSV), user_msgs[1+] = Gemma-generated

    output_rows = []
    for i, row in enumerate(sorted_rows):
        turn_res = bot_agent.turns[i] if i < len(bot_agent.turns) else None

        # For turns 2+, replace the CSV question with what Gemma actually asked
        actual_question = row["question"]
        if i < len(user_msgs):
            content = user_msgs[i].get("content", "")
            actual_question = content if isinstance(content, str) else json.dumps(content)
        elif i < len(bot_agent.answered_queries):
            actual_question = bot_agent.answered_queries[i]
        elif failure and i == len(bot_agent.turns) and bot_agent.last_query:
            actual_question = bot_agent.last_query

        answer = turn_res.text if turn_res else ""
        if failure and turn_res is None:
            answer = (f"ERROR: {failure}" if i == len(bot_agent.turns) else
                      f"ERROR: Not attempted after session failure at turn {len(bot_agent.turns) + 1}")

        output_rows.append({
            "session_id": row["session_id"],
            "question_id": row["question_id"],
            "category": row.get("category", ""),
            "question": actual_question,
            "answer": answer,
            "tool_calls": "",
            "tool_outputs": "",
            "TTFT_answer": f"{turn_res.ttft_ms:.2f}" if turn_res else "",
            "latency_answer": f"{turn_res.total_ms:.2f}" if turn_res else "",
        })

    # Put all session tool calls into the last row
    if output_rows and session_tool_calls:
        output_rows[-1]["tool_calls"] = json.dumps(session_tool_calls, ensure_ascii=False)
        output_rows[-1]["tool_outputs"] = session_tool_outputs

    print(
        f"  session {session_id:>4}: {n_turns} turn(s) in {elapsed:.1f}s "
        f"[{api_session_id}]"
    )
    return output_rows


# ---------------------------------------------------------------------------
# Per-language runner
# ---------------------------------------------------------------------------
async def run_language_pass(
    lpass: dict,
    *,
    client: httpx.AsyncClient,
    url: str,
    token: str,
    run_uuid: str,
    concurrency: int,
    langfuse_base_url: str,
    langfuse_public_key: str,
    langfuse_secret_key: str,
    lf_enabled: bool,
    api_kind: str = "text",
    tenant_id: str = "eval",
    session_filter: str = "",
    fixed_questions: bool = False,
    goal_driven: bool = False,
) -> None:
    in_col = lpass["input_column"]
    output_csv = OUTPUT_DIR / lpass["output"]
    language = lpass["language"]
    source_lang = lpass["source_lang"]
    target_lang = lpass["target_lang"]

    if not INPUT_CSV.exists():
        print(f"\n[{language}] Skipping — {INPUT_CSV} not found\n")
        return

    with INPUT_CSV.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        raw_rows = [dict(r) for r in reader]

    # Build per-language rows: map input_column → "question", skip blanks
    fieldnames = ["session_id", "question_id", "planned_question_id", "turn", "kind", "category", "question",
                  "answer", "tool_calls", "tool_outputs", "TTFT_answer", "latency_answer"]
    rows = []
    for r in raw_rows:
        if session_filter and r.get("session_id") != session_filter:
            continue
        q = (r.get(in_col) or "").strip()
        if not q:
            continue
        rows.append({
            "session_id":  r.get("session_id", ""),
            "question_id": r.get("question_id", ""),
            "category":    r.get("category", ""),
            "agristack_consent": r.get("agristack_consent", ""),
            "question":    q,
            "answer": "", "tool_calls": "", "tool_outputs": "",
            "TTFT_answer": "", "latency_answer": "",
        })

    if not rows:
        if session_filter:
            raise ValueError(f"No {language} questions for session {session_filter}")
        print(f"\n[{language}] Skipping — no non-empty rows in column '{in_col}'\n")
        return

    sessions: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        sessions[row["session_id"]].append(row)

    multi = sum(1 for v in sessions.values() if len(v) > 1)
    print(f"\n{'='*60}")
    print(f"Language: {language} ({source_lang}/{target_lang})")
    print(f"Rows    : {len(rows)}  Sessions: {len(sessions)}  Multi-turn: {multi}")
    print(f"Output  : {output_csv}")
    print(f"{'='*60}")

    sem = asyncio.Semaphore(concurrency)
    all_rows: list[dict] = []
    lock = asyncio.Lock()

    async def run_and_collect(sid: str, qs: list[dict]) -> None:
        try:
            result_rows = await run_session(
                sid, qs,
                client=client,
                url=url,
                token=token,
                run_uuid=run_uuid,
                sem=sem,
                language=language,
                source_lang=source_lang,
                target_lang=target_lang,
                langfuse_base_url=langfuse_base_url,
                langfuse_public_key=langfuse_public_key,
                langfuse_secret_key=langfuse_secret_key,
                lf_enabled=lf_enabled,
                api_kind=api_kind,
                tenant_id=tenant_id,
                fixed_questions=fixed_questions,
                goal_driven=goal_driven,
            )
            async with lock:
                all_rows.extend(result_rows)
        except Exception as e:
            print(f"  session {sid}: FAILED — {e}")
            async with lock:
                for row in qs:
                    all_rows.append({
                        "session_id": row["session_id"],
                        "question_id": row["question_id"],
                        "category": row.get("category", ""),
                        "question": row["question"],
                        "answer": f"ERROR: {e}",
                        "tool_calls": "", "tool_outputs": "",
                        "TTFT_answer": "", "latency_answer": "",
                    })

    async def periodic_save() -> None:
        while True:
            await asyncio.sleep(SAVE_INTERVAL_SEC)
            async with lock:
                snapshot = list(all_rows)
            _write_csv(output_csv, fieldnames, snapshot)
            print(f"  [checkpoint] {language}: saved {len(snapshot)}/{len(rows)} rows")

    t_start = time.perf_counter()
    save_task = asyncio.create_task(periodic_save())
    await asyncio.gather(*(run_and_collect(sid, qs) for sid, qs in sessions.items()))
    save_task.cancel()
    try:
        await save_task
    except asyncio.CancelledError:
        pass

    order = {r["question_id"]: i for i, r in enumerate(rows)}
    all_rows.sort(key=lambda r: order.get(r["question_id"], 9999))
    _write_csv(output_csv, fieldnames, all_rows)
    elapsed = time.perf_counter() - t_start
    print(f"\n[{language}] Done. {len(all_rows)} rows → {output_csv} in {elapsed:.1f}s")

    good = [r for r in all_rows if r.get("answer") and not r["answer"].startswith("ERROR")]
    for row in good[:3]:
        print(f"\n  Q [{row['session_id']}] {row['question'][:80]}")
        print(f"  A: {row['answer'][:200]}")
        if row.get("latency_answer"):
            print(f"     latency={row['latency_answer']}ms")
        print("  " + "-" * 58)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def async_main(args: argparse.Namespace) -> None:
    load_dotenv(DEFAULT_ENV)

    token = (args.token or os.environ.get("TOKEN", "")).strip()
    if not token and args.api_kind == "text":
        raise SystemExit("TOKEN required — set TOKEN in .env or pass --token")

    base_url = (args.base_url or os.environ.get("BASE_URL", "http://127.0.0.1:8000")).rstrip("/")
    url = base_url + ("/api/v1/chat/completions" if args.api_kind == "voice" else "/api/chat/")

    langfuse_base_url = (os.environ.get("LANGFUSE_BASE_URL") or "").rstrip("/")
    langfuse_public_key = os.environ.get("LANGFUSE_PUBLIC_KEY", "")
    langfuse_secret_key = os.environ.get("LANGFUSE_SECRET_KEY", "")
    lf_enabled = bool(langfuse_base_url and langfuse_public_key and langfuse_secret_key)

    run_uuid = uuid.uuid4().hex[:8]
    passes = language_passes(args.api_kind)
    if args.languages:
        langs = {l.strip().lower() for l in args.languages.split(",")}
        passes = [p for p in passes if p["language"].lower() in langs]
        if langs != {p["language"].lower() for p in passes}:
            raise ValueError("Unknown or unsupported requested language")

    print(f"Bot API : {url}")
    print(f"run_uuid: {run_uuid}")
    print(f"Langfuse: {'enabled' if lf_enabled else 'disabled'}")
    print(f"Passes  : {', '.join(p['language'] for p in passes)}")

    limits = httpx.Limits(
        max_keepalive_connections=args.concurrency + 4,
        max_connections=args.concurrency + 4,
    )
    async with httpx.AsyncClient(limits=limits) as client:
        for lpass in passes:
            await run_language_pass(
                lpass,
                client=client,
                url=url,
                token=token,
                run_uuid=run_uuid,
                concurrency=args.concurrency,
                langfuse_base_url=langfuse_base_url,
                langfuse_public_key=langfuse_public_key,
                langfuse_secret_key=langfuse_secret_key,
                lf_enabled=lf_enabled,
                api_kind=args.api_kind,
                tenant_id=args.tenant_id,
                session_filter=args.session,
                fixed_questions=args.fixed_questions,
                goal_driven=args.goal_driven,
            )

    print("\nAll passes complete.")


def _write_csv(path: Path, fieldnames: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({k: row.get(k, "") for k in fieldnames})


def main() -> None:
    ap = argparse.ArgumentParser(description="Multi-turn scenario benchmark for all languages")
    ap.add_argument("--api-kind", choices=["text", "voice"], default="text")
    ap.add_argument("--tenant-id", default="eval", help="Voice API tenant header")
    ap.add_argument("--session", default="", help="Run only this CSV session_id")
    ap.add_argument("--fixed-questions", action="store_true",
                    help="Replay every CSV question in order instead of simulating follow-ups")
    ap.add_argument("--goal-driven", action="store_true",
                    help="Multi-question sessions: send planned questions verbatim and let a simulated farmer "
                         "answer whatever the bot asks in between (the benchmark default)")
    ap.add_argument("--token", default="", help="Bearer token (default: env TOKEN)")
    ap.add_argument("--base-url", default="", help="API base URL (default: env BASE_URL)")
    ap.add_argument("--concurrency", type=int, default=MAX_CONCURRENT_SESSIONS,
                    help="max concurrent sessions per language pass")
    ap.add_argument("--languages", default="",
                    help="comma-separated language names to run (default: all in LANGUAGE_PASSES)")
    asyncio.run(async_main(ap.parse_args()))


if __name__ == "__main__":
    main()
