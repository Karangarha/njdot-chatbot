"""Mock OpenAI + Anthropic API for the NJDOT sandbox.

Stands in for api.openai.com and api.anthropic.com so the backend can run
every code path (RAG chat, compliance review, session Q&A, graph agent)
with no real API keys and deterministic answers.

Endpoints
---------
POST /v1/embeddings         OpenAI embeddings (strings or token-id lists,
                            float or base64 encoding), 1536 dims.
POST /v1/chat/completions   OpenAI chat: plain text, json_schema structured
                            output, tool calling (agent loop).
POST /v1/messages           Anthropic messages: text, tool_use (LangChain's
                            structured-output path), output_format json.
GET  /v1/models             Model list.
GET  /health                Liveness probe.
GET  /__stats               Per-endpoint/per-kind request counters (used by
                            the E2E suite to assert the LLM was really hit).
POST /__fail                {"openai": true|false, "anthropic": true|false}
                            toggles forced HTTP 500s — lets the suite exercise
                            the OpenAI -> Anthropic fallback path.

Every request is logged as one JSON line on stdout (endpoint, model, kind,
latency) so `docker compose logs mock-llm` shows exactly what the backend
asked for.
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import struct
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import tiktoken  # decode token-id inputs sent by langchain OpenAIEmbeddings

    _ENC = tiktoken.get_encoding("cl100k_base")
except Exception:  # pragma: no cover - mock still works on string input
    _ENC = None

DIMS = 1536
PORT = int(os.getenv("PORT", "4010"))

_lock = threading.Lock()
_stats: dict[str, int] = {}
_fail = {
    "openai": os.getenv("MOCK_FAIL_OPENAI", "").lower() in ("1", "true"),
    "anthropic": os.getenv("MOCK_FAIL_ANTHROPIC", "").lower() in ("1", "true"),
}


def _count(key: str) -> None:
    with _lock:
        _stats[key] = _stats.get(key, 0) + 1


def _log(**fields) -> None:
    fields["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    sys.stdout.write(json.dumps(fields) + "\n")
    sys.stdout.flush()


# ── Embeddings ────────────────────────────────────────────────────────────────
_WORD = re.compile(r"[a-z0-9]+")
_STOP = set(
    "the a an of to and or in on for is are be by with as at from that this it "
    "what which how does do shall must any all".split()
)


def embed(text: str) -> list[float]:
    """Hashed bag-of-words vector, L2-normalised.

    Texts sharing vocabulary get real cosine overlap, and a constant component
    lifts the baseline so short questions still clear the 0.2/0.3 similarity
    floors the retrievers apply against long chunks.
    """
    vec = [0.0] * DIMS
    words = [w for w in _WORD.findall(text.lower()) if w not in _STOP]
    for w in words:
        h = int.from_bytes(hashlib.md5(w.encode()).digest()[:8], "little")
        vec[h % (DIMS - 1)] += 1.0 if (h >> 63) == 0 else 0.8
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    vec = [v / norm for v in vec]
    vec[DIMS - 1] = 0.75  # shared component -> baseline cosine ~0.36
    norm = math.sqrt(sum(v * v for v in vec))
    return [v / norm for v in vec]


def _as_text(item) -> str:
    if isinstance(item, str):
        return item
    if isinstance(item, list) and _ENC is not None:
        try:
            return _ENC.decode(item)
        except Exception:
            pass
    return json.dumps(item)


# ── JSON-schema instance generator (structured output) ────────────────────────
def _resolve(schema: dict, root: dict) -> dict:
    while isinstance(schema, dict) and "$ref" in schema:
        ref = schema["$ref"].split("/")[-1]
        schema = (root.get("$defs") or root.get("definitions") or {}).get(ref, {})
    return schema


def instance(schema: dict, root: dict, name: str = "") -> object:
    schema = _resolve(schema, root)
    if "anyOf" in schema:
        options = [_resolve(s, root) for s in schema["anyOf"]]
        if any(o.get("type") == "null" for o in options):
            return None
        return instance(options[0], root, name)
    if "default" in schema and schema["default"] is not None:
        return schema["default"]
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if isinstance(t, list):
        if "null" in t:
            return None
        t = t[0]
    if t == "object" or "properties" in schema:
        return {k: instance(v, root, k) for k, v in (schema.get("properties") or {}).items()}
    if t == "array":
        return []
    if t == "string":
        return f"Mock {name.replace('_', ' ')}".strip()
    if t == "integer":
        return 0
    if t == "number":
        return 0.0
    if t == "boolean":
        return False
    return None


def _stable(text: str) -> int:
    return int(hashlib.sha1(text.encode()).hexdigest()[:8], 16)


def structured(name: str, schema: dict, prompt: str) -> dict:
    """Schema-valid answer, tuned per schema so the app's validators accept it."""
    obj = instance(schema, schema, name)
    if not isinstance(obj, dict):
        obj = {}
    if name == "EvaluationSchema":
        tags = re.findall(r"\[cite:([^\]]+)\]", prompt)
        missing = _stable(prompt) % 5 == 0  # some checks come back MISSING
        obj.update(
            considered_items=[],
            breaching_items=[],
            insufficient_evidence=missing,
            evidence=(
                "Mock evaluation: the provided excerpts were reviewed and no "
                "breach of this requirement was found."
                if not missing
                else "Mock evaluation: the documents do not contain enough evidence."
            ),
            source="Mock LLM (sandbox)",
            cited_chunk_ids=tags[:1],
        )
    elif name == "GroundingJudgment":
        obj.update(grounded=True, reason="Mock judge: evidence supports the verdict.")
    elif name == "EstimateExtraction":
        obj.update(
            engineers_estimate_raw="$12,500,000.00",
            engineers_estimate_usd=12500000.0,
            cost_basis_label="Engineer's Estimate",
            project_name="Route 49 (Sandbox mock)",
            page_transcription="Mock transcription of the DBE goal memo first page.",
        )
    elif name == "KeyMapExtraction":
        obj.update(
            route="Route 49",
            latitude_decimal=39.43,
            longitude_decimal=-75.23,
            municipalities=["Mock Township"],
            counties=["Cumberland"],
            utility_owners=["Mock Electric Co."],
        )
    elif name == "_EdqSectionLocateResult":
        obj.update(pages=[])
    # Strict json_schema outputs reject keys the schema doesn't declare.
    props = _resolve(schema, schema).get("properties")
    if props:
        obj = {k: v for k, v in obj.items() if k in props}
    return obj


# ── Chat helpers ──────────────────────────────────────────────────────────────
def _content_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
                elif block.get("type") == "tool_result":
                    parts.append(_content_text(block.get("content")))
        return "\n".join(parts)
    return ""


def _classify(system: str, user: str) -> str:
    both = system + "\n" + user
    if "spec_style" in both:
        return "query_expansion"
    if '"coverage"' in both and '"citations"' in both:
        return "rag_answer"
    if "Cypher" in both or "cypher" in both:
        return "cypher"
    if '"entities"' in both and '"relations"' in both:
        return "entities"
    if re.search(r"numbered list|exactly \d+ lines", both, re.I):
        return "contextualize"
    return "text"


def text_answer(kind: str, system: str, user: str) -> str:
    if kind == "query_expansion":
        q = user.strip().splitlines()[-1][:200] if user.strip() else "query"
        return json.dumps({"spec_style": q, "keywords": " ".join(_WORD.findall(q.lower())[:8])})
    if kind == "rag_answer":
        heads = re.findall(r"\[Chunk (\d+): ([^,\]]+), Section ([^,\]]+), Page ([^\]]+)\]", user)
        citations = []
        for _, doc, section, page in heads[:3]:
            page_num = int(page.strip()) if page.strip().isdigit() else None
            citations.append(
                {"document": doc.strip(), "section": section.strip(),
                 "page_printed": page_num, "page_pdf": page_num, "chunk_id": ""}
            )
        if not heads:
            return json.dumps({
                "answer": "Insufficient evidence in the provided manuals to answer this question.",
                "coverage": "insufficient", "citations": []})
        q = user.rsplit("Question:", 1)[-1].strip()
        answer = (
            f"**Sandbox mock answer** to: _{q[:160]}_\n\n"
            f"Based on {len(heads)} retrieved excerpt(s), the most relevant is "
            f"{heads[0][1]} Section {heads[0][2]}."
        )
        return json.dumps({"answer": answer, "coverage": "full", "citations": citations})
    if kind == "cypher":
        m = re.search(r"projectId:\s*'([^']+)'", user + system) or re.search(
            r"project[_ ]?id[^A-Za-z0-9]+([0-9a-f-]{36})", user + system, re.I)
        pid = m.group(1) if m else "unknown"
        return (
            f"MATCH (a:Activity {{projectId: '{pid}'}}) "
            "RETURN a.taskId AS taskId, a.name AS name LIMIT 10"
        )
    if kind == "entities":
        return json.dumps({"entities": [], "relations": []})
    if kind == "contextualize":
        m = re.search(r"exactly (\d+)", system + user)
        n = int(m.group(1)) if m else 1
        return "\n".join(f"{i}. Mock context summary for chunk {i}." for i in range(1, n + 1))
    q = user.strip()[-300:]
    return f"Sandbox mock response. You asked about: {q}"


def _usage_openai(prompt: str, completion: str) -> dict:
    p, c = max(1, len(prompt) // 4), max(1, len(completion) // 4)
    return {"prompt_tokens": p, "completion_tokens": c, "total_tokens": p + c,
            "prompt_tokens_details": {"cached_tokens": 0},
            "completion_tokens_details": {"reasoning_tokens": 0}}


# ── OpenAI chat ───────────────────────────────────────────────────────────────
def openai_chat(req: dict) -> tuple[dict, str]:
    msgs = req.get("messages", [])
    system = "\n".join(_content_text(m.get("content")) for m in msgs if m.get("role") in ("system", "developer"))
    user = "\n".join(_content_text(m.get("content")) for m in msgs if m.get("role") == "user")
    model = req.get("model", "gpt-4o")
    rf = req.get("response_format") or {}
    tools = req.get("tools") or []
    message: dict = {"role": "assistant", "content": None, "refusal": None}
    finish = "stop"

    if rf.get("type") == "json_schema":
        js = rf.get("json_schema", {})
        kind = f"structured:{js.get('name')}"
        message["content"] = json.dumps(structured(js.get("name", ""), js.get("schema", {}), system + user))
    elif rf.get("type") == "json_object":
        kind = "json_object"
        message["content"] = text_answer(_classify(system, user), system, user)
    elif tools and msgs and msgs[-1].get("role") != "tool":
        forced = req.get("tool_choice")
        fn = tools[0]["function"]
        if isinstance(forced, dict) and forced.get("function"):
            fn = next((t["function"] for t in tools if t["function"]["name"] == forced["function"]["name"]), fn)
            kind = f"tool_forced:{fn['name']}"
            args = structured(fn["name"], fn.get("parameters", {}), system + user)
        else:
            # Agent loop: prefer the schedule-graph tool, else the first tool.
            fn = next((t["function"] for t in tools if t["function"]["name"] == "query_schedule_graph"), fn)
            kind = f"tool_call:{fn['name']}"
            props = (fn.get("parameters") or {}).get("properties") or {"__arg1": {}}
            args = {next(iter(props)): user.strip()[-400:] or "activities"}
        message["tool_calls"] = [{
            "id": f"call_{uuid.uuid4().hex[:12]}", "type": "function",
            "function": {"name": fn["name"], "arguments": json.dumps(args)}}]
        finish = "tool_calls"
    elif tools:
        kind = "tool_final"
        tool_out = _content_text(msgs[-1].get("content"))
        message["content"] = (
            "**Sandbox mock answer** (graph agent). Tool output excerpt:\n\n"
            + (tool_out[:600] or "(empty)")
        )
    else:
        kind = _classify(system, user)
        message["content"] = text_answer(kind, system, user)

    out_text = message.get("content") or json.dumps(message.get("tool_calls"))
    body = {
        "id": f"chatcmpl-{uuid.uuid4().hex}", "object": "chat.completion",
        "created": int(time.time()), "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": _usage_openai(system + user, out_text), "system_fingerprint": "fp_sandbox",
    }
    return body, kind


# ── Anthropic messages ────────────────────────────────────────────────────────
def anthropic_messages(req: dict) -> tuple[dict, str]:
    sys_field = req.get("system") or ""
    system = sys_field if isinstance(sys_field, str) else _content_text(sys_field)
    msgs = req.get("messages", [])
    user = "\n".join(_content_text(m.get("content")) for m in msgs if m.get("role") == "user")
    tools = req.get("tools") or []
    choice = req.get("tool_choice") or {}
    last = msgs[-1] if msgs else {}
    last_is_tool_result = isinstance(last.get("content"), list) and any(
        isinstance(b, dict) and b.get("type") == "tool_result" for b in last["content"])
    fmt = req.get("output_format") or (req.get("output_config") or {}).get("format")
    content: list[dict]
    stop = "end_turn"

    if fmt and fmt.get("type") == "json_schema":
        kind = "structured:output_format"
        content = [{"type": "text", "text": json.dumps(structured("", fmt.get("schema", {}), system + user))}]
    elif tools and choice.get("type") == "tool":
        tool = next((t for t in tools if t.get("name") == choice.get("name")), tools[0])
        kind = f"structured:{tool['name']}"
        content = [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}", "name": tool["name"],
                    "input": structured(tool["name"], tool.get("input_schema", {}), system + user)}]
        stop = "tool_use"
    elif tools and not last_is_tool_result:
        tool = next((t for t in tools if t.get("name") == "query_schedule_graph"), tools[0])
        props = (tool.get("input_schema") or {}).get("properties") or {"__arg1": {}}
        kind = f"tool_call:{tool['name']}"
        content = [{"type": "tool_use", "id": f"toolu_{uuid.uuid4().hex[:20]}", "name": tool["name"],
                    "input": {next(iter(props)): user.strip()[-400:] or "activities"}}]
        stop = "tool_use"
    else:
        kind = "tool_final" if last_is_tool_result else _classify(system, user)
        text = (
            "**Sandbox mock answer** (Anthropic). " + _content_text(last.get("content"))[:600]
            if last_is_tool_result else text_answer(kind, system, user)
        )
        content = [{"type": "text", "text": text}]

    body = {
        "id": f"msg_{uuid.uuid4().hex[:24]}", "type": "message", "role": "assistant",
        "model": req.get("model", "claude-sonnet-5"), "content": content,
        "stop_reason": stop, "stop_sequence": None,
        "usage": {"input_tokens": max(1, len(system + user) // 4), "output_tokens": 50,
                  "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0},
    }
    return body, kind


# ── HTTP plumbing ─────────────────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # silence default access log; we log JSON
        pass

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _read(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0].rstrip("/")
        if path in ("/health", ""):
            return self._send(200, {"status": "ok"})
        if path == "/__stats":
            with _lock:
                return self._send(200, {"counts": dict(_stats), "fail": dict(_fail)})
        if path.endswith("/models"):
            return self._send(200, {"object": "list", "data": [
                {"id": m, "object": "model", "owned_by": "sandbox"}
                for m in ("gpt-4o", "text-embedding-3-small", "claude-sonnet-5")]})
        return self._send(404, {"error": {"message": f"unknown path {path}"}})

    def do_POST(self):
        path = self.path.split("?")[0].rstrip("/")
        started = time.time()
        try:
            req = self._read()
        except Exception as exc:
            return self._send(400, {"error": {"message": f"bad json: {exc}"}})

        if path == "/__fail":
            _fail.update({k: bool(v) for k, v in req.items() if k in _fail})
            _log(endpoint="__fail", state=_fail)
            return self._send(200, {"fail": dict(_fail)})
        if path == "/__reset":
            with _lock:
                _stats.clear()
            return self._send(200, {"ok": True})

        try:
            if path.endswith("/embeddings"):
                if _fail["openai"]:
                    raise _Forced("openai")
                inputs = req.get("input")
                if isinstance(inputs, str) or (isinstance(inputs, list) and inputs and isinstance(inputs[0], int)):
                    inputs = [inputs]
                fmt = req.get("encoding_format") or "float"
                data = []
                for i, item in enumerate(inputs or []):
                    vec = embed(_as_text(item))
                    emb = base64.b64encode(struct.pack(f"<{DIMS}f", *vec)).decode() if fmt == "base64" else vec
                    data.append({"object": "embedding", "index": i, "embedding": emb})
                kind = f"embeddings:{len(data)}"
                body = {"object": "list", "data": data, "model": req.get("model"),
                        "usage": {"prompt_tokens": 1, "total_tokens": 1}}
                provider = "openai"
            elif path.endswith("/chat/completions"):
                if _fail["openai"]:
                    raise _Forced("openai")
                if req.get("stream"):
                    return self._send(400, {"error": {"message": "streaming not supported by mock"}})
                body, kind = openai_chat(req)
                provider = "openai"
            elif path.endswith("/messages"):
                if _fail["anthropic"]:
                    raise _Forced("anthropic")
                body, kind = anthropic_messages(req)
                provider = "anthropic"
            else:
                return self._send(404, {"error": {"message": f"unknown path {path}"}})
        except _Forced as f:
            _count(f"{f.provider}:forced_500")
            _log(endpoint=path, provider=f.provider, kind="forced_500")
            return self._send(500, {"error": {"type": "server_error", "message": "mock forced failure"}})
        except Exception as exc:  # surface mock bugs loudly
            _count("mock_error")
            _log(endpoint=path, kind="mock_error", error=repr(exc))
            return self._send(500, {"error": {"type": "mock_error", "message": repr(exc)}})

        _count(f"{provider}:{kind.split(':')[0]}")
        _log(endpoint=path, provider=provider, model=req.get("model"), kind=kind,
             ms=int((time.time() - started) * 1000))
        return self._send(200, body)


class _Forced(Exception):
    def __init__(self, provider: str):
        self.provider = provider


if __name__ == "__main__":
    _log(event="mock-llm listening", port=PORT, tiktoken=_ENC is not None)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
