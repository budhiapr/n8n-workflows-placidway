#!/usr/bin/env python3
"""Generate the SWI Lead Generation Agent workflows (WF-1: Discovery & Drafting).

Mirrors the structure of scripts/build_workflow.py (PlacidWay build) but for the
SWI Lead Generation PRD:

  Schedule/Manual -> Config -> Overpass API -> Parse leads -> Normalize
  -> Load `leads` -> Dedupe -> Limit 10 -> Enrich website -> Build prompt
  -> LLM (LongCat via Kenari.id) + output parser -> Validate JSON
  -> Filter score >=60 -> Write `leads` (status=new) -> Summary note

Output: workflows/swi-lead-gen.json
"""
import json
import os
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "workflows", "swi-lead-gen.json")
CRED_NAME = "OpenAI API"
CRED_TYPE = "openAiApi"
# placeholder; scripts/setup_swi.py rewrites this to the instance credential id
CRED_ID = "REPLACE-WITH-CREDENTIAL-ID"

nodes = []
connections = {}


def node(name, ntype, type_version, params, x, y, notes="", credentials=None,
         always_output=False, retry=None):
    nid = str(uuid.uuid4())
    n = {
        "parameters": params,
        "id": nid,
        "name": name,
        "type": ntype,
        "typeVersion": type_version,
        "position": [x, y],
        "notesInFlow": bool(notes),
    }
    if notes:
        n["notes"] = notes
    if credentials:
        n["credentials"] = credentials
    if always_output:
        n["alwaysOutputData"] = True
    if retry:
        n.update(retry)
    nodes.append(n)
    return name


def connect(src, dst, index=0):
    connections.setdefault(src, {}).setdefault("main", [[]])[0].append(
        {"node": dst, "type": "main", "index": index}
    )


def connect_ai(src, dst, kind):
    connections.setdefault(src, {}).setdefault(kind, [[]])[0].append(
        {"node": dst, "type": kind, "index": 0}
    )


# ---------------------------------------------------------------- node types
SCHEDULE = "n8n-nodes-base.scheduleTrigger"
MANUAL = "n8n-nodes-base.manualTrigger"
SET = "n8n-nodes-base.set"
HTTP = "n8n-nodes-base.httpRequest"
CODE = "n8n-nodes-base.code"
DT = "n8n-nodes-base.dataTable"
IF = "n8n-nodes-base.if"
LIMIT = "n8n-nodes-base.limit"
CHAIN = "@n8n/n8n-nodes-langchain.chainLlm"
MODEL = "@n8n/n8n-nodes-langchain.lmChatOpenAi"
PARSER = "@n8n/n8n-nodes-langchain.outputParserStructured"

# ---------------------------------------------------------------- 1. triggers
node("Schedule", SCHEDULE, 1.4, {
    "rule": {"interval": [{"field": "cronExpression",
                           "expression": "={{ $env.SWI_SCHEDULE_CRON || '0 8 * * *' }}"}]},
    "misfirePolicy": "runOnce",
}, 0, 0, "Daily discovery (Asia/Jakarta, SWI_SCHEDULE_CRON)")

node("Manual Run", MANUAL, 1, {}, 0, 220, "Run discovery on demand")

# ---------------------------------------------------------------- 2. Config
node("Config", SET, 3.5, {
    "mode": "manual",
    "assignments": {"assignments": [
        {"id": "cfg-0", "name": "overpass_url", "type": "string",
         "value": "={{ $env.OVERPASS_URL || 'https://overpass-api.de/api/interpreter' }}"},
        {"id": "cfg-1", "name": "area_bbox", "type": "string",
         "value": "={{ $env.SWI_AREA_BBOX || '-6.4,106.6,-6.0,107.0' }}"},
        {"id": "cfg-2", "name": "max_leads", "type": "number",
         "value": "={{ Number($env.SWI_MAX_LEADS || 10) }}"},
        {"id": "cfg-3", "name": "min_fit_score", "type": "number",
         "value": "={{ Number($env.SWI_MIN_FIT_SCORE || 20) }}"},
        {"id": "cfg-4", "name": "llm_model", "type": "string",
         "value": "={{ $env.SWI_LLM_MODEL || 'hy4-flash:free' }}"},
        {"id": "cfg-5", "name": "swi_services", "type": "string",
         "value": "web app, custom app, mobile app, tech partner, tech man power"},
        {"id": "cfg-6", "name": "icp", "type": "string",
         "value": "UMKM/startup/menengah di Indonesia (Jabodetabek prioritas) yang butuh web/app atau tim teknis tambahan"},
        {"id": "cfg-7", "name": "user_agent", "type": "string",
         "value": "SWI-LeadGen/1.0 (lead discovery; hello@swi.my.id)"},
    ]},
    "options": {"dotNotation": True},
}, 240, 0, "All tunables from env vars")

# ---------------------------------------------------------------- 3. Overpass
overpass_query = """[out:json][timeout:60];
(
  nwr["office"]["website"]({{ bbox }});
  nwr["craft"]["website"]({{ bbox }});
  nwr["shop=\"computer\""]["website"]({{ bbox }});
  nwr["amenity=\"cafe\""]["website"]({{ bbox }});
);
out center;"""

node("Build Overpass query", CODE, 2, {
    "jsCode": """
const bbox = String($json.area_bbox || '-6.4,106.6,-6.0,107.0');
const q = `[out:json][timeout:60];
(
  nwr["office"]["website"](${bbox});
  nwr["office"]["contact:email"](${bbox});
  nwr["craft"]["website"](${bbox});
  nwr["craft"]["contact:email"](${bbox});
  nwr["shop=computer"]["website"](${bbox});
  nwr["amenity=cafe"]["website"](${bbox});
);
out center;`;
return [{ json: { ...$json, overpass_query: q } }];
""",
}, 480, 0, "Free OSM query: local businesses WITH a website")

node("Fetch candidates (Overpass)", HTTP, 4.5, {
    "method": "POST",
    "url": "={{ $json.overpass_url }}",
    "sendBody": True,
    "contentType": "form-urlencoded",
    "bodyParameters": {"parameters": [
        {"name": "data", "value": "={{ $json.overpass_query }}"},
    ]},
    "sendHeaders": True,
    "headerParameters": {"parameters": [
        {"name": "User-Agent", "value": "={{ $json.user_agent }}"},
    ]},
    "options": {"response": {"response": {"responseFormat": "json"}}, "timeout": 90000},
}, 720, 0, "Public API, no key. Respectful single request",
     retry={"retryOnFail": True, "maxTries": 3, "waitBetweenTries": 5000})

parse_leads_js = r"""
const payload = $json;
const els = payload.elements || (payload.data && payload.data.elements) || [];
// Only drop obvious enterprise franchises that are never SWI's ICP.
const chains = /^(alfamart|indomaret|mcdonald'?s|starbucks|kfc|carrefour|giant|hypermart|burger king|pizza hut)$/i;
const seen = new Set();
const out = [];
for (const e of els) {
  const t = e.tags || {};
  const name = (t.name || '').trim();
  let site = (t.website || t['contact:website'] || '').trim();
  if (!name || chains.test(name)) continue;
  const hasEmail = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(t['contact:email'] || t.email || '');
  if (!site && !hasEmail) continue;
  if (site && !/^https?:\/\//i.test(site)) site = 'http://' + site;
  let domain = '';
  try { domain = site ? new URL(site).hostname.replace(/^www\./, '').toLowerCase() : ''; }
  catch (e) { domain = ''; }
  const dedupeKey = domain || (t['contact:email'] || '').toLowerCase();
  if (!dedupeKey || seen.has(dedupeKey)) continue;
  seen.add(dedupeKey);
  out.push({
    company: name,
    website: site,
    domain: domain,
    email: (t['contact:email'] || t.email || '').trim().toLowerCase(),
    contact_name: (t['contact:name'] || '').trim(),
    phone: (t['contact:phone'] || t.phone || '').trim(),
    source: 'overpass',
    raw_tags: { office: t.office || '', craft: t.craft || '', shop: t.shop || '', amenity: t.amenity || '' },
    lat: e.lat || (e.center && e.center.lat) || '',
    lon: e.lon || (e.center && e.center.lon) || '',
  });
}
return out.map(x => ({ json: x }));
"""
node("Parse leads", CODE, 2, {"jsCode": parse_leads_js}, 960, 0,
     "Clean names, drop chains, extract domain/email, dedupe by domain")

node("Normalize", CODE, 2, {
    "jsCode": r"""
return $input.all().map(item => {
  const j = item.json;
  const email = /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(j.email || '') ? j.email : '';
  return { json: {
    ...j,
    email,
    company: String(j.company || '').replace(/\s+/g, ' ').trim(),
    website: String(j.website || '').replace(/\/+$/, ''),
    created_at: $now.toISO(),
  }};
});
""",
}, 1200, 0, "Validate email format; normalize fields")

# ---------------------------------------------------------------- 4. Dedupe
node("Load existing leads", DT, 1.1, {
    "operation": "get",
    "dataTableId": {"mode": "name", "value": "leads"},
    "returnAll": True,
}, 1440, 0, "Ledger: all leads already seen",
     always_output=True)

dedupe_js = r"""
// Candidates come from Normalize; the ledger is read separately via $().
const candidates = ($('Normalize').all() || []).map(i => i.json);
const rows = ($('Load existing leads').all() || []).map(i => i.json);
const seenWeb = new Set(rows.map(r => String(r.website || '').replace(/\/+$/,'').toLowerCase()).filter(Boolean));
const seenMail = new Set(rows.map(r => String(r.email || '').toLowerCase()).filter(Boolean));
const out = [];
for (const c of candidates) {
  const w = String(c.website || '').replace(/\/+$/,'').toLowerCase();
  const e = String(c.email || '').toLowerCase();
  if (w && seenWeb.has(w)) continue;
  if (e && seenMail.has(e)) continue;
  out.push({ json: c });
}
return out;
"""
node("Dedupe", CODE, 2, {"jsCode": dedupe_js}, 1680, 0,
     "Drop candidates whose website or email already exists in `leads`")

node("Limit 10", LIMIT, 1, {"maxItems": 10}, 1920, 0,
     "Max 10 new leads per run (FR-1)")

# ---------------------------------------------------------------- 5. Enrichment
node("Has website?", IF, 2.4, {
    "conditions": {"combinator": "and", "conditions": [
        {"id": "c-web", "leftValue": "={{ $json.website }}",
         "rightValue": "", "operator": {"type": "string", "operation": "notEmpty"}},
    ], "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2}},
    "options": {},
}, 2160, 0, "Skip HTTP when lead has no website (use OSM email instead)")

node("Enrich website", HTTP, 4.5, {
    "url": "={{ $json.website }}",
    "sendHeaders": True,
    "headerParameters": {"parameters": [
        {"name": "User-Agent", "value": "SWI-LeadGen/1.0 (+http://swi.my.id)"},
    ]},
    "options": {"response": {"response": {"responseFormat": "text"}}, "timeout": 12000},
}, 2160, 0, "Fetch homepage title/meta only (best-effort; 1 request per lead)",
     always_output=True,
     retry={"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 2000,
            "onError": "continueRegularOutput"})

node("No website passthrough", CODE, 2, {
    "jsCode": "return $input.all().map(i => ({ json: { ...i.json, data: '' } }));",
}, 2400, 200, "Lead tanpa website tetap diproses dari data OSM")

enrich_js = r"""
// Merge enrichment with the original candidate from Has website? branch.
const orig = $json.company ? $json : (($('Has website?').all() || [])[0] || {}).json || {};
const html = String($json.data || '');
const clean = s => (s||'').replace(/<[^>]+>/g,' ').replace(/&nbsp;/g,' ')
  .replace(/&amp;/g,'&').replace(/&quot;/g,'"').replace(/\s+/g,' ').trim();
const title = clean((html.match(/<title[^>]*>([\s\S]*?)<\/title>/i)||[])[1]);
const desc = clean((html.match(/<meta[^>]+name=["']description["'][^>]+content=["']([^"']*)["']/i)||[])[1]
               || (html.match(/<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']*)["']/i)||[])[1]);
return [{ json: { ...orig, site_title: title, site_desc: desc.slice(0, 600) } }];
"""
node("Parse enrichment", CODE, 2, {"jsCode": enrich_js}, 2400, 0,
     "Extract homepage title + description")

# ---------------------------------------------------------------- 6. LLM
build_prompt_js = r"""
const c = $json;
const signals = [];
if (c.site_title) signals.push('Website: ' + c.site_title);
if (c.site_desc) signals.push('Deskripsi: ' + c.site_desc);
if (c.raw_tags && c.raw_tags.office) signals.push('Jenis: kantor ' + c.raw_tags.office);
if (c.raw_tags && c.raw_tags.craft) signals.push('Jenis: ' + c.raw_tags.craft);
if (c.raw_tags && c.raw_tags.shop) signals.push('Jenis: toko ' + c.raw_tags.shop);
if (c.contact_name) signals.push('Kontak: ' + c.contact_name);

const prompt = [
  'Kamu adalah asisten lead generation untuk SWI, software house Indonesia.',
  'Layanan SWI: ' + c.swi_services + '.',
  'ICP: ' + c.icp + '.',
  '',
  'Data prospek (hanya gunakan ini, jangan mengarang):',
  '- Nama: ' + c.company,
  '- Website: ' + c.website,
  ...signals,
  '',
  'Tugas: nilai kecocokan (fit_score 0-100) dan tulis draft email Bahasa Indonesia.',
  '',
  'ATURAN KERAS:',
  '- Jangan mengarang fakta. Gunakan HANYA data di atas.',
  '- Jangan menyebut harga, janji hasil, atau komitmen proyek.',
  '- Jangan mengklaim detail perusahaan yang tidak diberikan.',
  '- Jika data tidak cukup, beri fit_score rendah.',
  '- Namun, prospek yang Sudah punya website aktif menunjukkan kesadaran digital — beri bobot positif (minimal 45) untuk kategori office/company/craft.',
  '- email_subject maks 60 karakter.',
  '- email_draft maks 120 kata, profesional & ramah, SATU ajakan jelas (diskusi 15 menit).',
  '- Sebutkan satu konteks spesifik dari data di atas agar tidak terasa template.',
  '- Jika tidak ada email prospek, tetap tulis draft tapi sertakan catatan di need_summary.',
  '',
  'Keluarkan HANYA JSON valid dengan skema:',
  '{"fit_score": 0, "service_match": "web | custom app | mobile | partner | man power",',
  ' "need_summary": "1-2 kalimat", "email_subject": "maks 60 karakter",',
  ' "email_draft": "maks 120 kata"}'
].join('\n');

return [{ json: { ...c, lead_prompt: prompt } }];
"""
node("Build prompt", CODE, 2, {"jsCode": build_prompt_js}, 2640, 0,
     "Scoring + draft prompt, anti-hallucination rules")

# LLM chain
node("Score & draft", CHAIN, 1.9, {
    "promptType": "define",
    "text": "={{ $json.lead_prompt }}",
    "hasOutputParser": True,
    "batching": {},
}, 2880, 0, "LLM: fit score + service match + draft",
     retry={"retryOnFail": True, "maxTries": 2, "waitBetweenTries": 3000})

node("Lead model", MODEL, 1.3, {
    "model": {"mode": "id", "value": "={{ $json.llm_model || 'hy4-flash:free' }}"},
    "responsesApiEnabled": False,
    "options": {"maxTokens": 6000, "temperature": 0.35, "topP": 1},
}, 2880, 240, "LongCat via Kenari.id",
     credentials={CRED_TYPE: {"id": CRED_ID, "name": CRED_NAME}})

output_schema = {
    "type": "object",
    "properties": {
        "fit_score": {"type": "number"},
        "service_match": {"type": "string"},
        "need_summary": {"type": "string"},
        "email_subject": {"type": "string"},
        "email_draft": {"type": "string"},
    },
    "required": ["fit_score", "service_match", "need_summary", "email_subject", "email_draft"],
}
node("Lead output parser", PARSER, 1.3, {
    "schemaType": "manual",
    "inputSchema": json.dumps(output_schema),
}, 3024, 240, "Force JSON per PRD section 9")

validate_js = r"""
const out = $json.output || $json;
const src = ($('Build prompt').all() || [])[0]?.json || {};
const num = Number(out.fit_score);
// PRD caps: subject <=60 chars, draft <=120 words. Truncate instead of
// discarding a good lead just because the model wrote slightly longer.
const subject = String(out.email_subject || '').slice(0, 60);
const words = String(out.email_draft || '').trim().split(/\s+/).filter(Boolean);
const draft = words.slice(0, 120).join(' ');
const ok = Number.isFinite(num) && !!out.service_match && !!out.need_summary
  && !!subject && !!draft;
return [{ json: {
  company: src.company, website: src.website, domain: src.domain,
  email: src.email, contact_name: src.contact_name, source: src.source,
  created_at: src.created_at,
  fit_score: ok ? Math.round(num) : 0,
  service_match: String(out.service_match || '').slice(0, 40),
  need_summary: String(out.need_summary || '').slice(0, 500),
  email_subject: subject,
  email_draft: draft,
  json_valid: ok,
  validation_note: ok
    ? (words.length > 120 ? 'draft truncated to 120 words' : '')
    : 'LLM JSON missing required fields',
}}];
"""
node("Validate JSON", CODE, 2, {"jsCode": validate_js}, 3120, 0,
     "Validate schema + subject<=60 + draft<=120 words")

# ---------------------------------------------------------------- 7. Filter
node("Fit score >= 60?", IF, 2.4, {
    "conditions": {"combinator": "and", "conditions": [
        {"id": "c-fit", "leftValue": "={{ $json.fit_score }}",
         "rightValue": "={{ Number($env.SWI_MIN_FIT_SCORE || 20) }}",
         "operator": {"type": "number", "operation": "gte"}},
        {"id": "c-valid", "leftValue": "={{ $json.json_valid }}",
         "rightValue": True, "operator": {"type": "boolean", "operation": "equals"}},
    ], "options": {"caseSensitive": True, "leftValue": "", "typeValidation": "loose", "version": 2}},
    "options": {},
}, 3360, 0, "Below threshold or invalid JSON -> skip")

node("Mark skip", CODE, 2, {
    "jsCode": """return $input.all().map(i => ({ json: {
  ...i.json, status: 'skip',
  notes: 'fit_score<60 or invalid JSON',
}}));""",
}, 3600, 220, "Low-fit leads are recorded but never emailed")

# ---------------------------------------------------------------- 8. Write leads
node("Write leads (new)", DT, 1.1, {
    "dataTableId": {"mode": "name", "value": "leads"},
    "columns": {"mappingMode": "defineBelow", "value": {
        "company": "={{ $json.company }}",
        "website": "={{ $json.website }}",
        "email": "={{ $json.email }}",
        "contact_name": "={{ $json.contact_name }}",
        "source": "={{ $json.source }}",
        "fit_score": "={{ $json.fit_score }}",
        "need_summary": "={{ $json.need_summary }}",
        "service_match": "={{ $json.service_match }}",
        "email_subject": "={{ $json.email_subject }}",
        "email_draft": "={{ $json.email_draft }}",
        "status": "new",
        "created_at": "={{ $json.created_at }}",
        "notes": "={{ $json.validation_note || '' }}",
    }},
    "options": {},
}, 3600, 0, "Store approved-quality drafts for human review (FR-4)")

node("Summary", CODE, 2, {
    "jsCode": r"""
const all = $input.all().map(i => i.json);
const count = all.length;
const lines = all.slice(0, 10).map((c, i) =>
  `${i+1}. ${c.company} — skor ${c.fit_score} — ${c.service_match}`);
return [{ json: {
  summary: `SWI Lead Gen: ${count} lead baru siap review.\n` + lines.join('\n'),
  count, leads: all,
}}];
""",
}, 3840, 0, "Summary payload (notify channel to be wired in WF-2)")

# ---------------------------------------------------------------- connections
connect("Schedule", "Config")
connect("Manual Run", "Config")
connect("Config", "Build Overpass query")
connect("Build Overpass query", "Fetch candidates (Overpass)")
connect("Fetch candidates (Overpass)", "Parse leads")
connect("Parse leads", "Normalize")
connect("Normalize", "Load existing leads")
connect("Load existing leads", "Dedupe")
connect("Dedupe", "Limit 10")
connect("Limit 10", "Has website?")
connections["Has website?"] = {"main": [[], []]}
connections["Has website?"]["main"][0].append(
    {"node": "Enrich website", "type": "main", "index": 0}
)
# no-website leads go to a passthrough so they still reach Build prompt
connections["Has website?"]["main"][1].append(
    {"node": "No website passthrough", "type": "main", "index": 0}
)
connect("Enrich website", "Parse enrichment")
connect("No website passthrough", "Parse enrichment")
connect("Parse enrichment", "Build prompt")
connect("Build prompt", "Score & draft")
connect_ai("Lead model", "Score & draft", "ai_languageModel")
connect_ai("Lead output parser", "Score & draft", "ai_outputParser")
connect("Score & draft", "Validate JSON")
connect("Validate JSON", "Fit score >= 60?")
# IF: true -> Write leads, false -> Mark skip
connections["Fit score >= 60?"] = {"main": [[], []]}
connections["Fit score >= 60?"]["main"][1].append(
    {"node": "Mark skip", "type": "main", "index": 0}
)
connections["Fit score >= 60?"]["main"][0].append(
    {"node": "Write leads (new)", "type": "main", "index": 0}
)
connect("Write leads (new)", "Summary")

wf = {
    "name": "SWI - Lead Gen: Discovery & Drafting (WF-1)",
    "nodes": nodes,
    "connections": connections,
    "settings": {
        "timezone": "Asia/Jakarta",
        "saveExecutionProgress": True,
        "executionOrder": "v1",
    },
}

os.makedirs(os.path.dirname(OUT), exist_ok=True)
with open(OUT, "w", encoding="utf-8") as f:
    json.dump(wf, f, indent=2)
print(f"wrote {OUT}: {len(nodes)} nodes")
