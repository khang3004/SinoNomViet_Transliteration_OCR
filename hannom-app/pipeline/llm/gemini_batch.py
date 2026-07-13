"""Gemini Batch Mode for bulk full-page auto-scan (Gemini-only).

Batch is asynchronous and ~50% cheaper with far higher throughput than interactive
calls — the right tool for scanning a whole job's pages at once. The API key is
supplied PER CALL (the reviewer's own key), never stored or logged.

File-based batch: we upload one JSONL (one line per page, image inline as base64)
via the Files API, create a batch job, later poll it, and download the result file.
All google-genai specifics live here; the rest of the app deals in plain dicts.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import tempfile

logger = logging.getLogger("hannom.llm.gemini_batch")


def _client(api_key: str):
    from google import genai

    return genai.Client(api_key=api_key)


def _request_line(key: str, image_png: bytes, prompt: str, system: str) -> dict:
    """One JSONL request line: a GenerateContentRequest wrapping page image + prompt."""
    b64 = base64.b64encode(image_png).decode("ascii")
    return {
        "key": key,
        "request": {
            "contents": [{
                "parts": [
                    {"text": prompt},
                    {"inline_data": {"mime_type": "image/png", "data": b64}},
                ],
            }],
            "system_instruction": {"parts": [{"text": system}]},
            "generation_config": {"response_mime_type": "application/json", "temperature": 0.2},
        },
    }


def submit_page_batch(api_key: str, model: str, items: list[dict], display_name: str) -> str:
    """Upload one JSONL of page requests and create a batch job. Returns its name.

    ``items`` = ``[{key, image_png, prompt, system}]`` — one per page.
    """
    from google.genai import types

    client = _client(api_key)
    tmp = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False, encoding="utf-8")
    try:
        for it in items:
            line = _request_line(it["key"], it["image_png"], it["prompt"], it["system"])
            tmp.write(json.dumps(line, ensure_ascii=False) + "\n")
        tmp.close()
        uploaded = client.files.upload(
            file=tmp.name,
            config=types.UploadFileConfig(display_name=display_name, mime_type="jsonl"),
        )
        batch = client.batches.create(
            model=model,
            src=uploaded.name,
            config=types.CreateBatchJobConfig(display_name=display_name),
        )
        logger.info("Submitted Gemini batch %s (%d pages, model=%s).", batch.name, len(items), model)
        return batch.name
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# Map the SDK's JOB_STATE_* to a small vocabulary the app/UI uses.
def norm_state(raw) -> str:
    s = (getattr(raw, "name", None) or str(raw) or "").upper()
    for tag in ("SUCCEEDED", "FAILED", "CANCELLED", "EXPIRED", "RUNNING", "PENDING"):
        if tag in s:
            return tag.lower()
    return "unknown"


def poll(api_key: str, batch_name: str) -> dict:
    """Return ``{state, error}`` for a batch (state is a norm_state() value)."""
    batch = _client(api_key).batches.get(name=batch_name)
    err = getattr(batch, "error", None)
    return {"state": norm_state(getattr(batch, "state", "")), "error": str(err) if err else ""}


def fetch_results(api_key: str, batch_name: str) -> dict[str, str]:
    """Download a SUCCEEDED batch's output and return ``{key: response_text}``."""
    client = _client(api_key)
    batch = client.batches.get(name=batch_name)
    dest = getattr(batch, "dest", None)
    file_name = getattr(dest, "file_name", None) if dest else None
    if not file_name:
        # Some SDKs return inlined responses instead of a file.
        inlined = getattr(dest, "inlined_responses", None) if dest else None
        if inlined:
            out: dict[str, str] = {}
            for i, r in enumerate(inlined):
                out[str(getattr(r, "key", i))] = _resp_text(getattr(r, "response", None))
            return out
        raise RuntimeError("batch has no result file or inlined responses")
    data = client.files.download(file=file_name)
    if not isinstance(data, (bytes, bytearray)):
        data = getattr(data, "read", lambda: bytes(data))()
    return parse_batch_results(bytes(data))


def _resp_text(response) -> str:
    """Pull concatenated text out of a GenerateContentResponse (obj or dict)."""
    if response is None:
        return ""
    if isinstance(response, dict):
        try:
            parts = response["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError, TypeError):
            return response.get("text", "") if isinstance(response, dict) else ""
    return getattr(response, "text", "") or ""


def parse_batch_results(jsonl_bytes: bytes) -> dict[str, str]:
    """Pure: parse a batch result JSONL into ``{key: response_text}``.

    Each line is ``{"key": ..., "response": {...}}`` (or an error line, which is
    skipped). Malformed lines are skipped.
    """
    out: dict[str, str] = {}
    for line in jsonl_bytes.decode("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        key = str(obj.get("key", ""))
        if not key or "error" in obj or "response" not in obj:
            continue
        text = _resp_text(obj["response"])
        if text:
            out[key] = text
    return out
