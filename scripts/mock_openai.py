#!/usr/bin/env python3
"""
Tiny OpenAI-compatible mock used ONLY to verify the LLM nodes locally
(request shape, model name, structured-output parsing) without spending
real API credits.

Not part of the demo pipeline. Point the "OpenAI API" credential's Base URL
at http://host.docker.internal:8766/v1 to use it.

Every request is appended to ./mock_openai.log so you can inspect exactly
what n8n sent.
"""

import json
import os
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = int(os.environ.get("MOCK_LLM_PORT", "8766"))
LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "mock_openai.log")


def first_user_message(body):
    """Handles both the Chat Completions and the Responses API shapes."""
    inp = body.get("input")
    if isinstance(inp, str):
        return inp
    if isinstance(inp, list):
        for m in inp:
            if isinstance(m, dict) and m.get("role") in ("user", "system"):
                content = m.get("content")
                if isinstance(content, str) and content.strip():
                    return content
    for m in body.get("messages", []):
        if m.get("role") in ("user", "system"):
            content = m.get("content")
            if isinstance(content, str) and content.strip():
                return content
    return ""


def extract_url(text):
    m = re.search(r"https?://[^\s\"'<>]+", text)
    return m.group(0).rstrip(").,;") if m else ""


UI_JUNK = re.compile(
    r"(select |request |share this|book a|copy link|schedule a|timezone|"
    r"midway island|(europe|america|asia|africa|australia)/|add to cart|"
    r"virtual consultation|video call|patient login|free quote|"
    r"^reviews?\b|\d+\s*reviews?\b)",
    re.I,
)


def _title_case_ratio(s):
    words = [w for w in re.findall(r"[A-Za-z][a-z]+", s) if len(w) > 2]
    if len(words) < 3:
        return 1.0
    return sum(1 for w in words if w[0].isupper()) / len(words)


def sentences(text, limit=3):
    """Pick informative fragments from the source body (prefer fact-dense ones)."""
    parts = re.split(r"(?<=[.!?])\s+|;\s*|,\s+(?=[A-Z0-9])", text)
    out = []
    for s in parts:
        s = " ".join(s.split()).strip(" ,;")
        if not (35 < len(s) < 170):
            continue
        if UI_JUNK.search(s):
            continue
        # Navigation and widget labels are almost entirely title-case.
        if _title_case_ratio(s) > 0.6:
            continue
        score = len(re.findall(r"\d", s)) + (2 if "$" in s else 0)
        out.append((score, s))
    out.sort(key=lambda x: -x[0])
    return [s for _, s in out[:limit]]


def derive_tags(title):
    tags = ["#MedicalTourism"]
    for word, tag in [
        ("dental", "#DentalTourism"), ("hair", "#HairTransplant"),
        ("rhinoplasty", "#Rhinoplasty"), ("gastric", "#BariatricSurgery"),
        ("breast", "#BreastSurgery"), ("tummy", "#TummyTuck"),
        ("istanbul", "#Istanbul"), ("turkey", "#Turkey"), ("mexico", "#Mexico"),
        ("tijuana", "#Tijuana"), ("india", "#India"), ("singapore", "#Singapore"),
    ]:
        if word in title.lower():
            tags.append(tag)
    return tags[:5]


def draft_reply(body):
    prompt = first_user_message(body)
    url = extract_url(prompt)
    m = re.search(r"PAGE TITLE:\s*(.+)", prompt)
    title = (m.group(1).strip().split("\n")[0] if m else "").strip()
    body_m = re.search(r"PAGE BODY:\n([\s\S]*?)(?:\nYou must format your output|\Z)", prompt)
    source = body_m.group(1) if body_m else ""

    topic = re.split(r"\s[|–—-]\s", title)[0].strip(" :|")
    hook = (topic[:70] + "?") if not topic.endswith("?") else topic[:70]
    if len(hook) > 90:
        hook = hook[:87].rstrip() + "?"

    lines = sentences(source)
    if not lines:
        lines = ["PlacidWay lists verified clinics and treatment packages for this procedure."]
    lines = [l if l.endswith(".") else l + "." for l in lines[:4]]

    tags = derive_tags(title)
    for fallback in ("#PlacidWay", "#HealthTravel", "#MedicalTravel"):
        if len(tags) >= 3:
            break
        if fallback not in tags:
            tags.append(fallback)

    return {
        "output": {
            "hook": hook,
            "value_lines": lines,
            "cta": "Read more:",
            "hashtags": tags[:5],
            "source_url": url,
        }
    }


def generic_reply(body):
    return {"output": {"greeting": "hello"}}


def review_reply(body):
    if os.environ.get("MOCK_FORCE_REVIEW"):
        return {
            "output": {
                "decision": "FAIL",
                "reasons": ["Mock-forced failure for testing the quarantine path."],
                "medical_claims": ["Mock claim"],
                "invented_prices": [],
                "unsupported_facts": [],
            }
        }
    return {
        "output": {
            "decision": "PASS",
            "reasons": [],
            "medical_claims": [],
            "invented_prices": [],
            "unsupported_facts": [],
        }
    }


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, payload):
        raw = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        if self.path.startswith("/v1/models"):
            self._send(200, {"object": "list", "data": [{"id": "mock-1", "object": "model"}]})
        else:
            self._send(404, {"error": {"message": "not found"}})

    def do_POST(self):
        length = int(self.headers.get("content-length", 0) or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            body = {}
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"path": self.path, "body": body}, ensure_ascii=False) + "\n")
        if not (self.path.startswith("/v1/chat/completions") or self.path.startswith("/v1/responses")):
            self._send(404, {"error": {"message": "not found"}})
            return
        prompt = first_user_message(body)
        low = prompt.lower()
        if "compliance reviewer" in low:
            reply = review_reply(body)
        elif "facebook posts for placidway" in low:
            reply = draft_reply(body)
        else:
            reply = generic_reply(body)
        text = json.dumps(reply)
        if self.path.startswith("/v1/responses"):
            self._send(
                200,
                {
                    "id": "resp_mock",
                    "object": "response",
                    "created_at": 0,
                    "status": "completed",
                    "model": body.get("model", "mock-1"),
                    "output": [
                        {
                            "id": "msg_mock",
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": text, "annotations": []}],
                        }
                    ],
                    "parallel_tool_calls": False,
                    "tool_choice": "none",
                    "tools": [],
                    "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                },
            )
            return
        self._send(
            200,
            {
                "id": "chatcmpl-mock",
                "object": "chat.completion",
                "model": body.get("model", "mock-1"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": text},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
            },
        )


if __name__ == "__main__":
    print(f"mock OpenAI server on http://127.0.0.1:{PORT} (logging to {LOG})")
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
