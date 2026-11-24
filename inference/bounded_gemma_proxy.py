"""Local request gate: at most BENCHMARK_LIMIT (default seven) in-flight inference/judge calls to Gemma.

For this explicitly authorized benchmark only. Never changes upstream serving.
Start with uvicorn inference.bounded_gemma_proxy:app --host 127.0.0.1 --port 18867.
"""
import asyncio
import json
import os
import time
import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, StreamingResponse

app = FastAPI()
LIMIT = int(os.environ.get('BENCHMARK_LIMIT', '7'))
slots = asyncio.Semaphore(LIMIT)
active = 0
peak = 0
completed = 0
upstream_url = os.environ.get('BENCHMARK_GEMMA_UPSTREAM', 'http://127.0.0.1:8867').rstrip('/')
allowed = {'http://127.0.0.1:8867'}
# Explicit opt-in for an authorized, bounded run; never an automatic fallback.
if os.environ.get('BENCHMARK_ALLOW_PRODUCTION') == '1':
    allowed.add('http://127.0.0.1:8864')
if upstream_url not in allowed:
    raise ValueError('Unsupported Gemma endpoint; production requires explicit BENCHMARK_ALLOW_PRODUCTION=1')
upstream = httpx.AsyncClient(base_url=upstream_url, timeout=600,
                            limits=httpx.Limits(max_connections=LIMIT + 2))


@app.get('/health')
async def health():
    return dict(limit=LIMIT, active=active, peak=peak, completed=completed)


USAGE_LOG = os.environ.get('BENCHMARK_USAGE_LOG')  # Optional JSONL of per-request token usage.


def record_usage(arm, status, payload, tail):
    if not USAGE_LOG:
        return
    usage, start = {}, tail.rfind(b'"usage":')
    if start >= 0:
        try:
            usage = json.JSONDecoder().raw_decode(tail[start + 8:].decode('utf-8', 'ignore').lstrip())[0] or {}
        except ValueError:
            pass
    with open(USAGE_LOG, 'a') as log:
        log.write(json.dumps(dict(ts=time.time(), arm=arm, status=status, request_bytes=len(payload), agent=b'"tools"' in payload,
                                  prompt_tokens=usage.get('prompt_tokens'),
                                  completion_tokens=usage.get('completion_tokens'))) + '\n')


@app.api_route('/{arm}/v1/{path:path}', methods=['GET', 'POST'])
async def tagged_proxy(arm: str, path: str, request: Request):
    """Same as /v1, with the caller's label recorded in the usage log."""
    return await proxy(path, request, arm)


@app.api_route('/v1/{path:path}', methods=['GET', 'POST'])
async def proxy(path: str, request: Request, arm: str = ''):
    global active, peak, completed
    counted = request.method == 'POST' and path == 'chat/completions'
    if not counted and not (request.method == 'GET' and path == 'models'):
        return JSONResponse({'error': 'Unsupported benchmark route'}, status_code=404)
    if counted:
        await slots.acquire()
        active += 1
        peak = max(peak, active)
    try:
        payload = await request.body()
        req = upstream.build_request(request.method, '/v1/' + path, content=payload,
                                     headers={'Content-Type': 'application/json', 'Accept-Encoding': 'identity'})
        response = await upstream.send(req, stream=True)
    except BaseException:
        if counted:
            active -= 1
            slots.release()
        raise

    async def body():
        global active, completed
        tail = b''
        try:
            async for chunk in response.aiter_raw():
                tail = (tail + chunk)[-8192:]
                yield chunk
        finally:
            await response.aclose()
            if counted:
                record_usage(arm, response.status_code, payload, tail)
                active -= 1
                completed += 1
                slots.release()
    return StreamingResponse(body(), status_code=response.status_code,
                             media_type=response.headers.get('content-type', 'application/json'))
