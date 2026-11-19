#!/usr/bin/env python3
"""One-time setup for the SWI Lead Generation Agent (WF-1).

  1. Sign in to n8n (cookie + browser-id).
  2. Create the `leads` Data Table with the PRD section 7 schema.
  3. Create/update the "OpenAI API" credential pointing at Kenari.id
     (LongCat model). Reuses the existing credential if present.
  4. Import workflows/swi-lead-gen.json (replaces an existing SWI WF-1).
  5. Patch the LLM node to reference the real credential id.

Usage:
    python3 scripts/setup_swi.py
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
WORKFLOW = os.path.join(ROOT, "workflows", "swi-lead-gen.json")
WF_NAME = "SWI - Lead Gen: Discovery & Drafting (WF-1)"
CRED_NAME = "OpenAI API"
CRED_TYPE = "openAiApi"
LLM_NODE_TYPE = "@n8n/n8n-nodes-langchain.lmChatOpenAi"


def load_dotenv(path=None):
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


load_dotenv()
BASE = os.environ.get("N8N_URL", "http://localhost:5678").rstrip("/")
EMAIL = os.environ.get("N8N_EMAIL", "admin@example.com")
PASSWORD = os.environ.get("N8N_PASSWORD", "Test1234")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://kenari.id/v1").rstrip("/")
API_KEY = os.environ.get("OPENAI_API_KEY", "")
PID = "IfDqnb4HEywxZfTY"  # personal project id in this instance


class Api:
    def __init__(self):
        self.cookie = None
        self.bid = str(uuid.uuid4())

    def req(self, method, path, body=None, ok=(200, 201)):
        data = json.dumps(body).encode() if body is not None else None
        r = urllib.request.Request(BASE + path, data=data, method=method)
        r.add_header("accept", "application/json")
        r.add_header("browser-id", self.bid)
        if data:
            r.add_header("content-type", "application/json")
        if self.cookie:
            r.add_header("cookie", "n8n-auth=" + self.cookie)
        try:
            with urllib.request.urlopen(r, timeout=60) as resp:
                raw = resp.read().decode()
                if resp.status not in ok:
                    raise RuntimeError(f"{method} {path} -> {resp.status}: {raw[:300]}")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as e:
            raw = e.read().decode()
            if e.code in ok:
                return json.loads(raw) if raw else {}
            raise RuntimeError(f"{method} {path} -> {e.code}: {raw[:300]}") from None

    def login(self):
        body = json.dumps({"emailOrLdapLoginId": EMAIL, "password": PASSWORD}).encode()
        r = urllib.request.Request(BASE + "/rest/login", data=body, method="POST")
        r.add_header("content-type", "application/json")
        r.add_header("browser-id", self.bid)
        with urllib.request.urlopen(r, timeout=30) as resp:
            sc = resp.headers.get("set-cookie", "")
        if "n8n-auth=" not in sc:
            raise RuntimeError("login did not return n8n-auth cookie")
        self.cookie = sc.split("n8n-auth=", 1)[1].split(";", 1)[0]
        print(f"  logged in as {EMAIL}")


def ensure_credential(api):
    existing = api.req("GET", "/rest/credentials?" + urllib.parse.urlencode({"filter": json.dumps({"name": CRED_NAME})}))
    items = existing.get("data", [])
    payload = {
        "name": CRED_NAME,
        "type": CRED_TYPE,
        "nodesAccess": [{"nodeType": LLM_NODE_TYPE}],
        "data": {"apiKey": API_KEY or "PLACEHOLDER-REPLACE-ME", "url": LLM_BASE_URL},
    }
    if items:
        cid = items[0]["id"]
        print(f"  credential '{CRED_NAME}' exists ({cid})")
        if API_KEY:
            api.req("PATCH", f"/rest/credentials/{cid}", payload, ok=(200,))
            print("  api key + url updated")
        return cid
    created = api.req("POST", "/rest/credentials", payload)
    cid = created["data"]["id"]
    print(f"  created credential '{CRED_NAME}' ({cid})")
    return cid


LEAD_COLUMNS = [
    ("company", "string"), ("website", "string"), ("email", "string"),
    ("contact_name", "string"), ("source", "string"), ("fit_score", "number"),
    ("need_summary", "string"), ("service_match", "string"),
    ("email_subject", "string"), ("email_draft", "string"),
    ("status", "string"), ("created_at", "string"), ("sent_at", "string"),
    ("notes", "string"),
]


def ensure_table(api):
    existing = api.req("GET", f"/rest/projects/{PID}/data-tables")
    for t in existing.get("data", {}).get("data", []):
        if t.get("name") == "leads":
            print(f"  data table 'leads' exists ({t['id']}) with {len(t.get('columns', []))} columns")
            return t["id"]
    cols_payload = [{"name": name, "type": ctype} for name, ctype in LEAD_COLUMNS]
    created = api.req("POST", f"/rest/projects/{PID}/data-tables",
                      {"name": "leads", "columns": cols_payload})
    tid = created["data"]["id"]
    print(f"  created data table 'leads' ({tid}) with {len(cols_payload)} columns")
    return tid


def import_workflow(api, cred_id):
    wf = json.load(open(WORKFLOW, encoding="utf-8"))
    # wire credential
    for n in wf["nodes"]:
        ref = (n.get("credentials") or {}).get(CRED_TYPE)
        if ref:
            ref["id"] = cred_id
            ref["name"] = CRED_NAME
    # remove stale same-named workflow
    listed = api.req("GET", "/rest/workflows?limit=200")
    for w in listed.get("data", []):
        if w["name"] == WF_NAME:
            if w.get("active"):
                api.req("POST", f"/rest/workflows/{w['id']}/deactivate")
                time.sleep(1)
            api.req("POST", f"/rest/workflows/{w['id']}/archive", ok=(200, 400))
            for attempt in range(5):
                try:
                    api.req("DELETE", f"/rest/workflows/{w['id']}")
                    break
                except RuntimeError as e:
                    if "409" not in str(e) or attempt == 4:
                        raise
                    time.sleep(2)
            print(f"  removed stale workflow {w['id']}")
            break
    created = api.req("POST", "/rest/workflows", wf)
    wid = created["data"]["id"]
    print(f"  imported '{WF_NAME}' -> {wid} ({len(wf['nodes'])} nodes)")
    return wid


def main():
    if not os.path.exists(WORKFLOW):
        sys.exit(f"missing {WORKFLOW} (run scripts/build_swi_workflow.py first)")
    api = Api()
    print(f"n8n: {BASE}")
    api.login()
    cred_id = ensure_credential(api)
    ensure_table(api)
    wid = import_workflow(api, cred_id)
    print(f"\nDone. WF-1 id={wid}. Open it at {BASE}/workflow/{wid} and hit Execute.")
    if not API_KEY:
        print("\nNOTE: no OPENAI_API_KEY in .env — credential has a placeholder.")
        print("      Edit 'OpenAI API' in the n8n UI before running the LLM node.")


if __name__ == "__main__":
    main()
