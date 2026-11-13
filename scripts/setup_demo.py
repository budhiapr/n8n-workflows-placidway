#!/usr/bin/env python3
"""
One-time setup for the PlacidWay -> Facebook daily post demo.

Run this against a fresh n8n instance AFTER importing the workflows
(see README). It performs the instance-specific wiring that cannot be
baked into the exported JSON:

  1. Signs in as the owner and grabs a session cookie (+ browser-id header).
  2. Creates (or updates) the "OpenAI API" credential used by the LLM nodes.
     Points at an OpenAI-compatible endpoint (kenari.id by default).
  3. Patches every workflow in ./workflows so its LLM nodes reference that
     credential by id (n8n credential ids are instance-specific).
  4. Points the main workflow's `settings.errorWorkflow` at the error handler.
  5. Activates the error handler workflow (required for Error Trigger to fire).

Usage:
    python3 scripts/setup_demo.py

Environment (defaults shown):
    N8N_URL=http://localhost:5678
    N8N_EMAIL=admin@example.com
    N8N_PASSWORD=Test1234
    LLM_BASE_URL=https://kenari.id/v1
    OPENAI_API_KEY=            # optional; edit the credential in the UI later
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


def load_dotenv(path=None):
    """Read the project .env into os.environ (without overwriting real env)."""
    path = path or os.path.join(ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip())


# Must run before the settings below are read.
load_dotenv()

N8N_URL = os.environ.get("N8N_URL", "http://localhost:5678").rstrip("/")
N8N_EMAIL = os.environ.get("N8N_EMAIL", "admin@example.com")
N8N_PASSWORD = os.environ.get("N8N_PASSWORD", "Test1234")
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://kenari.id/v1").rstrip("/")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
CRED_NAME = os.environ.get("N8N_CRED_NAME", "OpenAI API")
CRED_TYPE = "openAiApi"
LLM_NODE_TYPE = "@n8n/n8n-nodes-langchain.lmChatOpenAi"
MAIN_WORKFLOW = "PlacidWay Daily Social Post"
ERROR_WORKFLOW = "PlacidWay - Error Handler"


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
            with urllib.request.urlopen(req, timeout=30) as res:
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
        sc = ""
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
        print(f"  logged in as {N8N_EMAIL}")


def credential_payload():
    return {
        "name": CRED_NAME,
        "type": CRED_TYPE,
        "nodesAccess": [{"nodeType": LLM_NODE_TYPE}],
        "data": {
            "apiKey": OPENAI_API_KEY or "PLACEHOLDER-REPLACE-ME",
            "url": LLM_BASE_URL,
        },
    }


def upsert_credential(api):
    existing = api.request(
        "GET", "/rest/credentials?" + urllib.parse.urlencode({"filter": json.dumps({"name": CRED_NAME})})
    )
    items = existing.get("data", [])
    if items:
        cred_id = items[0]["id"]
        print(f"  credential '{CRED_NAME}' already exists ({cred_id})")
        if OPENAI_API_KEY:
            api.request("PATCH", f"/rest/credentials/{cred_id}", credential_payload(), ok=(200,))
            print("  api key stored")
    else:
        created = api.request("POST", "/rest/credentials", credential_payload())
        cred_id = created["data"]["id"]
        print(f"  created credential '{CRED_NAME}' ({cred_id})")
    return cred_id


def list_workflows(api):
    listed = api.request("GET", "/rest/workflows?limit=200")
    return {w["name"]: w for w in listed.get("data", [])}


def get_workflow(api, wf_id):
    return api.request("GET", f"/rest/workflows/{wf_id}")["data"]


def patch_credential_refs(api, cred_id, by_name):
    patched = 0
    for fname in sorted(os.listdir(WORKFLOWS)):
        if not fname.endswith(".json"):
            continue
        with open(os.path.join(WORKFLOWS, fname), encoding="utf-8") as f:
            wf = json.load(f)
        if not any(
            (n.get("credentials") or {}).get(CRED_TYPE) for n in wf.get("nodes", [])
        ):
            continue
        changed = False
        for node in wf.get("nodes", []):
            ref = (node.get("credentials") or {}).get(CRED_TYPE)
            if ref and ref.get("id") != cred_id:
                ref["id"] = cred_id
                ref["name"] = CRED_NAME
                changed = True
        target = by_name.get(wf["name"])
        if not target:
            print(f"  {fname}: not found in n8n (import it first) - skipped")
            continue
        if not changed:
            print(f"  {fname}: credential already wired")
            continue
        api.request(
            "PATCH",
            f"/rest/workflows/{target['id']}",
            {"name": wf["name"], "nodes": wf["nodes"], "connections": wf["connections"]},
            ok=(200,),
        )
        patched += 1
        print(f"  {fname}: wired credential -> {target['id']}")
    return patched


def wire_error_workflow(api, by_name):
    main = by_name.get(MAIN_WORKFLOW)
    err = by_name.get(ERROR_WORKFLOW)
    if not main or not err:
        print(f"  error workflow wiring skipped (main={bool(main)}, handler={bool(err)})")
        return
    if not err.get("active"):
        api.request(
            "POST",
            f"/rest/workflows/{err['id']}/activate",
            {"versionId": err["versionId"]},
            ok=(200, 201),
        )
        print(f"  activated '{ERROR_WORKFLOW}'")
    else:
        print(f"  '{ERROR_WORKFLOW}' already active")
    current = (main.get("settings") or {}).get("errorWorkflow")
    if current == err["id"]:
        print(f"  error workflow already wired ({err['id']})")
        return
    full = get_workflow(api, main["id"])
    settings = dict(full.get("settings") or {})
    settings["errorWorkflow"] = err["id"]
    api.request(
        "PATCH",
        f"/rest/workflows/{main['id']}",
        {"name": full["name"], "nodes": full["nodes"], "connections": full["connections"], "settings": settings},
        ok=(200,),
    )
    print(f"  error workflow wired: {MAIN_WORKFLOW} -> {err['id']}")


def main():
    load_dotenv()
    if not os.path.isdir(WORKFLOWS):
        sys.exit(f"workflows directory not found: {WORKFLOWS}")
    api = Api(N8N_URL)
    print(f"n8n: {N8N_URL}")
    api.login()
    cred_id = upsert_credential(api)
    by_name = list_workflows(api)
    n = patch_credential_refs(api, cred_id, by_name)
    wire_error_workflow(api, by_name)
    print(f"\nDone. {n} workflow(s) wired to credential '{CRED_NAME}'.")
    if not OPENAI_API_KEY:
        print(
            "\nNOTE: no OPENAI_API_KEY set. The credential stores a placeholder.\n"
            "      Paste your key at: n8n UI -> Credentials -> 'OpenAI API' -> API Key,\n"
            "      or re-run with OPENAI_API_KEY=... set."
        )


if __name__ == "__main__":
    main()
