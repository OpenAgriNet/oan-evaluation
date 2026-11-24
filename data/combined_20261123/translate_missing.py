"""Fill missing question translations in text.csv with Gemma (open model, through bounded gateway 18869).

The mined prod-failure questions exist only in English and Hindi, and the full benchmark has no Punjabi or
Maithili. Each missing (question, language) is translated from the final English text (after date edits) and
cached in translations.json keyed by "<language>\t<english>". build_combined.py fills empty cells from the cache
and lists the machine-translated languages per row in the `translated` column.

Usage: translate_missing.py   (re-run safe: only missing cache entries are requested)
"""
import asyncio
import csv
import json
from pathlib import Path

import httpx

HERE = Path(__file__).resolve().parent
CACHE = HERE / 'translations.json'
URL = 'http://127.0.0.1:18869/translate/v1/chat/completions'
MODEL = 'google/gemma-4-31b-it'
CONCURRENCY = 16
SCRIPT = {'hindi': 'Hindi (Devanagari)', 'marathi': 'Marathi (Devanagari)', 'gujarati': 'Gujarati',
          'malayalam': 'Malayalam', 'kannada': 'Kannada', 'tamil': 'Tamil', 'telugu': 'Telugu',
          'bengali': 'Bengali', 'assamese': 'Assamese', 'odia': 'Odia', 'punjabi': 'Punjabi (Gurmukhi)',
          'maithili': 'Maithili (Devanagari)'}
PROMPT = """Translate this message from an Indian farmer to an agriculture help chatbot from English into {lang}.
Write it the way a farmer would naturally type it in {lang} script. Keep the meaning, every detail and the
question form exactly. Keep these unchanged, in Latin script and digits: registration numbers, application and
ticket numbers, OTPs, phone numbers, Farmer IDs, coordinates, amounts and dates written with digits, and scheme
acronyms such as PM-KISAN, PMFBY, KCC, AIF, GFR, SHC, SATHI, OTP. Place names and crop names go in {lang} script.
Reply with the translation only, no quotes and no notes.

Message: {text}"""


async def translate(client, sem, lang, text):
    async with sem:
        for attempt in range(4):
            try:
                r = await client.post(URL, json=dict(model=MODEL, temperature=0, max_tokens=800, messages=[
                    dict(role='user', content=PROMPT.format(lang=SCRIPT[lang], text=text))]), timeout=120)
                r.raise_for_status()
                out = r.json()['choices'][0]['message']['content'].strip()
                if out:
                    return out
            except Exception as exc:  # noqa: BLE001 - retried, then reported
                err = exc
            await asyncio.sleep(2 * (attempt + 1))
        raise RuntimeError(f'{lang}: {text[:60]}: {err}')


async def main():
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    rows = list(csv.DictReader((HERE / 'text.csv').open()))
    todo = sorted({(lang, r['question_english']) for r in rows for lang in SCRIPT
                   if not r['question_' + lang].strip() and f"{lang}\t{r['question_english']}" not in cache})
    print('to translate:', len(todo))
    sem = asyncio.Semaphore(CONCURRENCY)
    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(*(translate(client, sem, lang, text) for lang, text in todo),
                                       return_exceptions=True)
    failed = 0
    for (lang, text), out in zip(todo, results):
        if isinstance(out, Exception):
            failed += 1
            print('FAILED', out)
        else:
            cache[f'{lang}\t{text}'] = out
    CACHE.write_text(json.dumps(cache, ensure_ascii=False, indent=1))
    print('cached', len(cache), 'failed', failed)


if __name__ == '__main__':
    asyncio.run(main())
