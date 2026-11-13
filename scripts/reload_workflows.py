#!/usr/bin/env python3
"""
Re-import the workflows in ./workflows into a running n8n instance.

Existing workflows whose names start with "PlacidWay" are archived and
deleted first, so this is safe to run repeatedly while iterating on
workflows/placidway-daily-post.json.

Usage:
    python3 scripts/reload_workflows.py

Environment (defaults shown):
    N8N_URL=http://localhost:5678
    N8N_EMAIL=admin@example.com
    N8N_PASSWORD=Test1234
"""

import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOWS = os.path.join(ROOT, "workflows")

N8N_URL = os.environ.get("N8N_URL", "http://localhost:5678").rstrip("/")
N8N_EMAIL = os.environ.get("N8N_EMAIL", "admin@example.com")
N8N_PASSWORD = os.environ.get("N8N_PASSWORD", "Test1234")
PREFIX = "PlacidWay"


class Api:
    def __init__(self, base):
        self.base = base
        self.cookie = None
        self.browser_id = str(uuid.uuid4())

    def request(self, method, path, body=None, ok=(200, 201)):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("accept", "application/json")
        req.add_header("browser-id", self.browser_id)
        if data:
            req.add_header("content-type", "application/json")
        if self.cookie:
            req.add_header("cookie", "n8n-auth=" + self.cookie)
        try:
            with urllib.request.urlopen(req, timeout=60) as res:
                raw = res.read().decode()
                if res.status not in ok:
                    raise RuntimeError(f"{method} {path} -> {res.status}: {raw[:400]}")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            if e.code in ok:
                return json.loads(raw) if raw else {}
            raise RuntimeError(f"{method} {path} -> {e.code}: {raw[:400]}") from None

    def login(self):
        req = urllib.request.Request(
            self.base + "/rest/login",
            data=json.dumps({"emailOrLdapLoginId": N8N_EMAIL, "password": N8N_PASSWORD}).encode(),
            method="POST",
        )
        req.add_header("content-type", "application/json")
        req.add_header("browser-id", self.browser_id)
        last = None
        for attempt in range(6):
            try:
                with urllib.request.urlopen(req, timeout=30) as r:
                    sc = r.headers.get("set-cookie", "")
                break
            except urllib.error.HTTPError as e:
                last = e
                if e.code != 429 or attempt == 5:
                    raise RuntimeError(f"login failed -> {e.code}: {e.read().decode()[:300]}") from None
                time.sleep(5)
        else:
            raise RuntimeError(f"login failed -> {last.code if last else '?'}")
        if "n8n-auth=" not in sc:
            raise RuntimeError("login did not return an auth cookie")
        self.cookie = sc.split("n8n-auth=", 1)[1].split(";", 1)[0]


def main():
    if not os.path.isdir(WORKFLOWS):
        sys.exit(f"workflows directory not found: {WORKFLOWS}")
    api = Api(N8N_URL)
    api.login()
    print(f"logged in to {N8N_URL}")

    listed = api.request("GET", "/rest/workflows?" + urllib.parse.urlencode({"limit": 200}))
    stale = [w for w in listed.get("data", []) if w["name"].startswith(PREFIX)]
    for w in stale:
        if w.get("active"):
            api.request("POST", f"/rest/workflows/{w['id']}/deactivate")
            time.sleep(1)
        api.request("POST", f"/rest/workflows/{w['id']}/archive", ok=(200, 400))
        for attempt in range(5):
            try:
                api.request("DELETE", f"/rest/workflows/{w['id']}")
                break
            except RuntimeError as e:
                if "409" not in str(e) or attempt == 4:
                    raise
                time.sleep(2)
    if stale:
        print(f"removed {len(stale)} existing workflow(s)")

    for fname in sorted(os.listdir(WORKFLOWS)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(WORKFLOWS, fname), encoding="utf-8") as f:
            wf = json.load(f)
        created = api.request("POST", "/rest/workflows", wf)
        d = created.get("data", {})
        print(f"  {fname} -> {d.get('id')} ({d.get('name')})")

    print("\nNext: python3 scripts/setup_demo.py")


if __name__ == "__main__":
    main()
