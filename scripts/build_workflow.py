#!/usr/bin/env python3
"""
Generates the n8n workflow JSON for the PlacidWay -> Facebook daily social post demo.

Usage:
    python3 scripts/build_workflow.py

Outputs:
    workflows/placidway-daily-post.json
    workflows/setup-tables.json
    workflows/error-handler.json
    workflows/approve-post.json

Every node type / version / parameter here was verified against the installed
n8n 2.47.3 (see README "Node verification" section).
"""

import json
import os
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "workflows")

BASE_URL = "https://www.placidway.com"

SITEMAP_ARTICLES = f"{BASE_URL}/1/sitemap_articles.xml"
SITEMAP_PACKAGES = f"{BASE_URL}/1/sitemap_packages.xml"
SITEMAP_PROFILES = f"{BASE_URL}/sitemap_profiles.xml"
SITEMAP_ANSWERS = f"{BASE_URL}/sitemap/1/sitemap_answers.xml"

# The main pipeline in flow order, used only for canvas layout.
MAIN_CHAIN = [
    "Schedule", "Config", "Fetch robots.txt", "Parse robots",
    "Fetch sitemap index", "Parse sitemap index",
    "Wait Articles", "Fetch Articles sitemap", "Parse Articles sitemap",
    "Wait Packages", "Fetch Packages sitemap", "Parse Packages sitemap",
    "Wait Profiles", "Fetch Profiles sitemap", "Parse Profiles sitemap",
    "Wait Answers", "Fetch Answers sitemap", "Parse Answers sitemap",
    "Merge candidates", "Flatten and sort", "Load posted URLs",
    "Select candidate", "Has candidate?", "Reserve URL",
    "Wait before page fetch", "Fetch item page", "Parse item page",
    "Build draft prompt", "Draft post", "Merge draft + source",
    "Compose post", "Rule guardrail", "Rule pass?",
    "Build review prompt", "Compliance review", "Merge review + draft",
    "Normalize review verdict", "Review pass?", "Publish (simulated)",
    "Write published_posts", "Mark posted", "Log run",
]


def nid(label):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "placidway-n8n/" + label))


def node(label, ntype, version, params, x, y, note=None, color=None,
         retry=None, on_error=None, credentials=None):
    n = {
        "id": nid(label),
        "name": label,
        "type": ntype,
        "typeVersion": version,
        "position": [x, y],
        "parameters": params,
    }
    if note:
        n["notes"] = note
        n["notesInFlow"] = True
    if color:
        n["color"] = color
    if retry:
        n["retryOnFail"] = True
        n["maxTries"] = retry.get("maxTries", 3)
        n["waitBetweenTries"] = retry.get("waitBetweenTries", 1000)
    if on_error:
        n["onError"] = on_error
    if credentials:
        n["credentials"] = credentials
    return n


# --------------------------------------------------------------------------- #
# Code snippets
# --------------------------------------------------------------------------- #

PARSE_ROBOTS = r"""
const raw = $json.data || '';
const crawl = raw.match(/crawl-delay:\s*(\d+)/i);
const sitemap = raw.match(/sitemap:\s*(\S+)/i);
const disallow = (raw.match(/^Disallow:\s*(.+)$/gmi) || []).map(l => l.replace(/^Disallow:\s*/i, '').trim());

return [{
  json: {
    crawl_delay: crawl ? Number(crawl[1]) : 3,
    sitemap_url: sitemap ? sitemap[1].replace(/[>\s]+$/, '') : __BASE_URL__ + '/sitemap.xml',
    disallow_rules: disallow,
    robots_text: raw.slice(0, 3000),
  },
}];
""".replace("__BASE_URL__", json.dumps(BASE_URL))

PARSE_SITEMAP_INDEX = r"""
const raw = $json.data || '';
const locs = [...raw.matchAll(/<loc>\s*([^<]+?)\s*<\/loc>/g)].map(m => m[1].trim());

const find = (needle) => {
  const hit = locs.find(u => u.includes(needle));
  return hit || '';
};

return [{
  json: {
    articles_url: find('sitemap_articles.xml'),
    packages_url: find('sitemap_packages.xml'),
    profiles_url: find('sitemap_profiles.xml'),
    answers_url: find('sitemap_answers.xml'),
    sitemap_count: locs.length,
  },
}];
"""

# content type is baked in by the generator (per-node code)
PARSE_SITEMAP = r"""
const contentType = '__CONTENT_TYPE__';
const maxItems = Number($('Config').first().json.max_per_sitemap || 50);
const raw = $json.data || '';
const entries = [];
const re = /<url>([\s\S]*?)<\/url>/g;
let m;
while ((m = re.exec(raw)) !== null) {
  const block = m[1];
  const loc = ((block.match(/<loc>\s*([^<]+?)\s*<\/loc>/) || [])[1] || '').trim();
  const lastmod = ((block.match(/<lastmod>\s*([^<]+?)\s*<\/lastmod>/) || [])[1] || '').trim();
  const img = ((block.match(/<image:loc>\s*([^<]+?)\s*<\/image:loc>/) || [])[1] || '').trim();
  if (loc) entries.push({ url: loc, lastmod: lastmod, image_url: img, content_type: contentType });
}
entries.sort((a, b) => (b.lastmod || '').localeCompare(a.lastmod || '') || b.url.localeCompare(a.url));
return [{
  json: {
    items: entries.slice(0, maxItems),
    content_type: contentType,
    total: entries.length,
    kept: Math.min(entries.length, maxItems),
  },
}];
"""

FLATTEN_AND_SORT = r"""
const groups = $input.all().map(i => i.json || {});
const all = [];
for (const g of groups) {
  const items = (g && Array.isArray(g.items)) ? g.items : [];
  for (const it of items) all.push(it);
}
all.sort((a, b) => (b.lastmod || '').localeCompare(a.lastmod || '') || b.url.localeCompare(a.url));
const seen = new Set();
const deduped = all.filter(x => { if (seen.has(x.url)) return false; seen.add(x.url); return true; });
return [{ json: { candidates: deduped, candidate_count: deduped.length } }];
"""

SELECT_CANDIDATE = r"""
const candidates = $('Flatten and sort').first().json.candidates || [];
const postedItems = $('Load posted URLs').all().map(i => i.json || {}).filter(p => p.url);
const statusByUrl = new Map(postedItems.map(p => [p.url, p.status]));
const blocked = new Set(['published', 'quarantined', 'pending_review']);
const eligible = candidates.filter(c => {
  const st = statusByUrl.get(c.url);
  return !st || !blocked.has(st);
});
const selected = eligible[0] || null;
return [{
  json: {
    selected: selected,
    selected_url: selected ? selected.url : '',
    has_candidate: !!selected,
    eligible_count: eligible.length,
    candidate_count: candidates.length,
    posted_count: postedItems.length,
  },
}];
"""

PARSE_ITEM_PAGE = r"""
const html = $json.data || '';
const clean = s => (s || '').replace(/<[^>]+>/g, ' ').replace(/\s+/g, ' ').trim();

const title = clean(((html.match(/<title[^>]*>([\s\S]*?)<\/title>/i) || [])[1]));
const ogTitle = ((html.match(/<meta[^>]+property=["']og:title["'][^>]+content=["']([^"']*)["']/i) || [])[1] || '').trim();
const ogDesc = ((html.match(/<meta[^>]+property=["']og:description["'][^>]+content=["']([^"']*)["']/i) || [])[1] || '').trim();
const metaDesc = ((html.match(/<meta[^>]+name=["']description["'][^>]+content=["']([^"']*)["']/i) || [])[1] || '').trim();
const ogImage = ((html.match(/<meta[^>]+property=["']og:image["'][^>]+content=["']([^"']*)["']/i) || [])[1] || '').trim();

let body = html
  .replace(/<script[\s\S]*?<\/script>/gi, ' ')
  .replace(/<style[\s\S]*?<\/style>/gi, ' ')
  .replace(/<noscript[\s\S]*?<\/noscript>/gi, ' ')
  .replace(/<svg[\s\S]*?<\/svg>/gi, ' ')
  .replace(/<[^>]+>/g, ' ');
body = body
  .replace(/&nbsp;/g, ' ').replace(/&amp;/g, '&').replace(/&quot;/g, '"')
  .replace(/&#39;/g, "'").replace(/&lt;/g, '<').replace(/&gt;/g, '>')
  .replace(/&#x27;/g, "'").replace(/&#x2F;/g, '/');
body = body.replace(/\s+/g, ' ').trim();

const sel = $('Select candidate').first().json.selected || {};
const h1 = clean((html.match(/<h1[^>]*>([\s\S]*?)<\/h1>/i) || [])[1]);
let main = body;
if (h1) {
  const idx = body.indexOf(h1.slice(0, 40));
  if (idx > 0) main = body.slice(idx);
}
main = main.slice(0, 6000);

return [{
  json: {
    title: (ogTitle || title || sel.url || '').trim(),
    meta_description: (ogDesc || metaDesc || '').trim(),
    image_url: ogImage,
    source_url: sel.url || '',
    content_type: sel.content_type || '',
    source_body: main,
    source_body_length: main.length,
    page_fetched_at: new Date().toISOString(),
  },
}];
"""

BUILD_DRAFT_PROMPT = r"""
const lines = String($json.source_body || '').split('\n');
const url = String($json.source_url || '');
const title = String($json.title || '');
const summary = String($json.meta_description || '');
const body = String($json.source_body || '').slice(0, 4000);

const prompt = [
  'You write Facebook posts for PlacidWay, a medical tourism marketplace.',
  'Write ONE post about the source page below.',
  '',
  'RULES (hard):',
  '- Hook: <= 12 words, informative, NO superlatives (never "best", "cheapest", "leading").',
  '- value_lines: 2 to 4 short lines (max 14 words each) that state ONLY facts present in the source.',
  '- cta: one short call to action, e.g. "Read more:"',
  '- source_url: copy the source URL EXACTLY as given.',
  '- hashtags: 3 to 5 hashtags derived from the page (mix #MedicalTourism + treatment/country).',
  '- English, informative tone, at most 2 emojis.',
  '- Total characters BEFORE the link must be under 500.',
  '- Do NOT state or imply medical outcomes, guarantees, or recovery promises.',
  '- Do NOT invent prices. If you mention a price it must be copied verbatim from the source.',
  '',
  'SOURCE URL: ' + url,
  'PAGE TITLE: ' + title,
  'PAGE SUMMARY: ' + summary,
  'PAGE BODY:',
  body,
].join('\n');

return [{ json: { ...$json, draft_prompt: prompt } }];
"""

BUILD_REVIEW_PROMPT = r"""
const post = String($json.post_text || '');
const body = String($json.source_body || '').slice(0, 6000);

const prompt = [
  'You are a compliance reviewer for medical tourism social media.',
  'Does the post below make medical claims, promise outcomes, invent prices,',
  'or state facts that are NOT supported by the source page?',
  '',
  'Answer FAIL if ANY of these are true:',
  '- It claims a treatment is guaranteed, risk-free, painless, permanent, or a cure.',
  '- It promises an outcome (recovery speed, success rate, "patients feel better").',
  '- It contains a price that does not appear verbatim in the source body.',
  '- It states facts (statistics, accreditations, doctor credentials) absent from the source.',
  '- It uses superlatives in the hook ("best", "cheapest", "world-class").',
  'Otherwise answer PASS.',
  '',
  'POST:',
  post,
  '',
  'SOURCE BODY:',
  body,
].join('\n');

return [{ json: { ...$json, review_prompt: prompt } }];
"""

COMPOSE_POST = r"""
const o = $json.output || {};
const hook = (o.hook || '').trim();
const lines = Array.isArray(o.value_lines) ? o.value_lines.map(s => String(s).trim()).filter(Boolean) : [];
const cta = (o.cta || '').trim();
const hashtags = Array.isArray(o.hashtags) ? o.hashtags.map(s => String(s).trim()).filter(Boolean) : [];
const sourceUrl = (o.source_url || $json.source_url || '').trim();

const parts = [];
if (hook) parts.push(hook);
for (const l of lines) parts.push('- ' + l);
if (cta) parts.push(cta);
if (sourceUrl) parts.push(sourceUrl);
if (hashtags.length) parts.push(hashtags.join(' '));
const postText = parts.join('\n');

return [{
  json: {
    ...$json,
    post_text: postText,
    hashtags: hashtags,
    source_url: sourceUrl,
    pre_link_length: postText.split(/https?:\/\//)[0].length,
  },
}];
"""

RULE_GUARDRAIL = r"""
const post = ($json.post_text || '').trim();
const sourceBody = $json.source_body || '';
const sourceUrl = $json.source_url || '';
const hashtags = Array.isArray($json.hashtags) ? $json.hashtags : [];
const errors = [];

// 1. structure
const lines = post.split('\n').map(l => l.trim()).filter(Boolean);
if (lines.length < 3) errors.push('post has fewer than 3 lines (hook/value/cta/link)');
if (hashtags.length < 3) errors.push('too few hashtags: ' + hashtags.length + ' (min 3)');
if (hashtags.length > 5) errors.push('too many hashtags: ' + hashtags.length + ' (max 5)');

// 2. length < 500 before the link
const preLink = post.split(/https?:\/\//)[0].length;
if (preLink > 500) errors.push('pre-link length ' + preLink + ' > 500');

// 3. link check
const urlsInPost = post.match(/https?:\/\/[^\s]+/g) || [];
if (!urlsInPost.length) errors.push('no source link in post');
for (const u of urlsInPost) {
  const normalized = u.replace(/[).,]+$/, '');
  if (normalized !== sourceUrl) errors.push('link mismatch: ' + normalized + ' != ' + sourceUrl);
}

// 4. blacklist
const blacklist = [
  'guarantee', 'guaranteed', 'cure ', 'cures ', '100%', 'best results', 'best choice',
  'risk-free', 'risk free', 'permanent result', 'miracle', 'no side effects',
  'fully recovered', 'will heal', 'certainly', 'clinically proven', 'painless',
  'zero pain', 'instant result', 'first in the world', 'number one', 'no. 1',
];
const lower = post.toLowerCase();
for (const w of blacklist) {
  if (lower.includes(w)) errors.push('blacklisted phrase: "' + w.trim() + '"');
}

// 5. superlatives in hook
const hookLine = lines[0] || '';
const superlatives = ['best', 'cheapest', 'lowest cost', 'top-rated', 'world class', 'world-class', 'leading', 'premier'];
for (const w of superlatives) {
  if (hookLine.toLowerCase().includes(w)) errors.push('superlative in hook: "' + w + '"');
}

// 6. price check: every $amount in the post must exist verbatim in the source body
const priceRe = /\$\s?[\d,]+(?:\.\d+)?/g;
const postPrices = post.match(priceRe) || [];
for (const p of postPrices) {
  if (!sourceBody.includes(p)) errors.push('price not found verbatim in source: ' + p);
}

// 7. emoji budget (<= 2)
const emojis = post.match(/[\u{1F000}-\u{1FAFF}\u{2600}-\u{27BF}\u{2190}-\u{21FF}\u{2B00}-\u{2BFF}]/gu) || [];
if (emojis.length > 2) errors.push('too many emojis: ' + emojis.length + ' (max 2)');

// 8. marketing copy copied from source (og:description hint) is allowed but flagged
const marketingFlagged = sourceBody.toLowerCase().includes('save 40') || sourceBody.toLowerCase().includes('save 50');

return [{
  json: {
    ...$json,
    guardrail: {
      passed: errors.length === 0,
      errors: errors,
      errors_count: errors.length,
      pre_link_length: preLink,
      hashtags_count: hashtags.length,
      prices_found: postPrices,
      emojis_count: emojis.length,
      marketing_copy_flagged: marketingFlagged,
    },
  },
}];
"""

REVIEW_PASS_CHECK = r"""
const o = $json.output || {};
const decision = String(o.decision || '').toUpperCase();
const reasons = Array.isArray(o.reasons) ? o.reasons : [];
return [{
  json: {
    ...$json,
    review_passed: decision === 'PASS',
    review_decision: decision,
    review_reasons: reasons,
  },
}];
"""

PUBLISH = r"""
const cfg = $('Config').first().json || {};
const autoPublish = cfg.auto_publish === true;
const publisherMode = cfg.publisher_mode || 'simulate';
const now = new Date();
const runId = 'run_' + now.getTime();
const postId = 'sim_' + now.getTime();

let status = 'pending_review';
if (publisherMode === 'simulate') {
  status = autoPublish ? 'published' : 'pending_review';
} else {
  status = autoPublish ? 'published' : 'pending_review';
}

return [{
  json: {
    ...$json,
    run_id: runId,
    post_id: postId,
    status: status,
    publisher_mode: publisherMode,
    auto_publish: autoPublish,
    published_at: now.toISOString(),
  },
}];
"""

NO_NEW_CONTENT = r"""
const now = new Date();
const runId = 'run_' + now.getTime();
const count = $('Select candidate').first().json.candidate_count || 0;
return [{
  json: {
    run_id: runId,
    timestamp: now.toISOString(),
    status: 'no_new_content',
    selected_url: '',
    content_type: '',
    post_text: '',
    guardrail_result: 'n/a',
    publisher_mode: 'n/a',
    post_id: '',
    error: 'No unseen candidates left (candidates=' + count + ').',
    candidates_exhausted: true,
  },
}];
"""

QUARANTINE = r"""
const now = new Date();
const runId = 'run_' + now.getTime();
const g = $json.guardrail || {};
const r = $json.review_reasons || [];
const reasons = (g.errors && g.errors.length ? g.errors : r);
const sel = $('Select candidate').first().json.selected || {};
return [{
  json: {
    run_id: runId,
    timestamp: now.toISOString(),
    status: 'quarantined',
    selected_url: sel.url || '',
    content_type: sel.content_type || '',
    post_text: $json.post_text || '',
    guardrail_result: 'FAIL',
    publisher_mode: 'n/a',
    post_id: '',
    error: reasons.join(' | '),
    quarantine_reason: reasons,
  },
}];
"""

ALERT_NO_CONTENT = r"""
const cfg = $('Config').first().json || {};
const channel = cfg.alert_channel || '';
const text = '[PlacidWay Bot] No new content to post. All candidates are already published/quarantined.';
return [{ json: { channel: channel, message: text, text: text, severity: 'info', kind: 'no_new_content' } }];
"""

ALERT_QUARANTINE = r"""
const cfg = $('Config').first().json || {};
const channel = cfg.alert_channel || '';
const q = $('Quarantine').first().json || {};
const url = q.selected_url || '';
const reasons = Array.isArray(q.quarantine_reason) ? q.quarantine_reason : [q.quarantine_reason];
const text = '[PlacidWay Bot] Post quarantined for: ' + url + '\nReasons: ' + reasons.join('; ');
return [{ json: { channel: channel, message: text, text: text, severity: 'warning', kind: 'quarantine' } }];
"""

ERROR_HANDLER = r"""
// n8n's Error Trigger outputs { execution: { error, url, mode, ... }, workflow: {...} }
const ex = $json.execution || {};
const err = ex.error || {};
const failedNode = err.node || {};
const message = (typeof err === 'string' ? err : (err.message || 'Unknown error'));
return [{
  json: {
    run_id: 'run_' + new Date().getTime(),
    timestamp: new Date().toISOString(),
    status: 'failed',
    selected_url: '',
    content_type: '',
    post_text: '',
    guardrail_result: 'n/a',
    publisher_mode: 'n/a',
    post_id: '',
    error: message,
    error_type: err.type || err.name || 'unknown',
    workflow_id: $json.workflow ? $json.workflow.id : '',
    execution_id: ex.id || '',
    execution_url: ex.url || '',
    failed_node: failedNode.name || '',
  },
}];
"""

ERROR_ALERT = r"""
// This workflow has no Config node, so read the env var directly.
const channel = $env.ALERT_CHANNEL || '';
const text = '[PlacidWay Bot] Workflow FAILED: ' + ($json.error || 'unknown');
return [{ json: { channel: channel, message: text, text: text, severity: 'error', kind: 'workflow_error' } }];
"""

# --------------------------------------------------------------------------- #
# Node helpers
# --------------------------------------------------------------------------- #

def http_node(label, url_expr, x, y, note, color="#5ad8a6", method="GET",
              body=None, content_type=None):
    params = {
        "method": method,
        "url": url_expr,
        "sendHeaders": True,
        "specifyHeaders": "keypair",
        "headerParameters": {
            "parameters": [
                {
                    "name": "User-Agent",
                    "value": "PlacidWaySocialBot/1.0 (demo; respects robots.txt)",
                }
            ]
        },
        "options": {
            "timeout": 30000,
            "response": {"response": {"responseFormat": "text", "fullResponse": False}},
        },
    }
    if body is not None:
        params["sendBody"] = True
        params["contentType"] = content_type or "raw"
        params["body"] = body
    return node(
        label,
        "n8n-nodes-base.httpRequest",
        4.5,
        params,
        x,
        y,
        note=note,
        color=color,
        retry={"maxTries": 3, "waitBetweenTries": 2000},
    )


def code_node(label, code, x, y, note, color="#f6bd16", inputs=1):
    return node(
        label,
        "n8n-nodes-base.code",
        2,
        {"mode": "runOnceForAllItems", "language": "javaScript", "jsCode": code},
        x,
        y,
        note=note,
        color=color,
    )


def wait_node(label, x, y, note):
    return node(
        label,
        "n8n-nodes-base.wait",
        1.1,
        {
            "resume": "timeInterval",
            "amount": "={{ $('Parse robots').first().json.crawl_delay }}",
            "unit": "seconds",
        },
        x,
        y,
        note=note,
        color="#dcdde1",
    )


def data_table_node(label, table, operation, x, y, note, extra=None,
                    match_type="allConditions", return_all=False, limit=100,
                    color=None, always_output=False):
    params = {
        "resource": "row",
        "operation": operation,
        "dataTableId": {"mode": "name", "value": table},
    }
    if extra:
        params.update(extra)
    n = node(label, "n8n-nodes-base.dataTable", 1.1, params, x, y,
             note=note, color=color or "#a855f7")
    if always_output:
        # n8n drops the item if the node would output nothing (e.g. an empty
        # table). Pass the input item through so the chain keeps flowing.
        n["alwaysOutputData"] = True
    return n


def if_node(label, conditions, x, y, note, color="#ff9d4d"):
    return node(
        label,
        "n8n-nodes-base.if",
        2.4,
        {
            "conditions": {
                "combinator": "and",
                "conditions": conditions,
                "options": {
                    "caseSensitive": True,
                    "leftValue": "",
                    "typeValidation": "loose",
                    "version": 2,
                },
            }
        },
        x,
        y,
        note=note,
        color=color,
    )


def cond_bool(path, expected, cid):
    return {
        "id": cid,
        "leftValue": "={{ $json." + path + " }}",
        "rightValue": expected,
        "operator": {"type": "boolean", "operation": "equals"},
    }


def cond_str(path, expected, cid, op="equals"):
    return {
        "id": cid,
        "leftValue": "={{ $json." + path + " }}",
        "rightValue": expected,
        "operator": {"type": "string", "operation": op},
    }


def llm_chain(label, x, y, note, prompt_expr, schema, parser_name):
    return node(
        label,
        "@n8n/n8n-nodes-langchain.chainLlm",
        1.9,
        {
            "promptType": "define",
            "text": prompt_expr,
            "hasOutputParser": True,
        },
        x,
        y,
        note=note,
        color="#e8684a",
    )


def openai_model(label, x, y, note):
    return node(
        label,
        "@n8n/n8n-nodes-langchain.lmChatOpenAi",
        1.3,
        {
            "model": {"mode": "id", "value": "={{ $env.LLM_MODEL || 'gpt-4o-mini' }}"},
            # Use the Chat Completions endpoint: far more widely supported by
            # OpenAI-compatible providers than the Responses API.
            "responsesApiEnabled": False,
            "options": {
                "temperature": 0.4,
                "maxTokens": 800,
                "topP": 1,
            },
        },
        x,
        y,
        note=note,
        color="#e8684a",
        credentials={"openAiApi": {"id": "", "name": "OpenAI API"}},
    )


def output_parser(label, x, y, note, schema):
    return node(
        label,
        "@n8n/n8n-nodes-langchain.outputParserStructured",
        1.3,
        {
            # "manual" + inputSchema: the node otherwise defaults to
            # schemaType "fromJson" and ignores jsonSchema entirely.
            "schemaType": "manual",
            "inputSchema": schema,
            "autoFix": False,
        },
        x,
        y,
        note=note,
        color="#e8684a",
    )


# --------------------------------------------------------------------------- #
# Build the main workflow
# --------------------------------------------------------------------------- #

def reposition(nodes):
    """Lay the canvas out as a compact serpentine instead of one long row."""
    by_name = {n["name"]: n for n in nodes}
    per_row, dx, dy = 14, 240, 280

    for i, name in enumerate(MAIN_CHAIN):
        n = by_name.get(name)
        if not n:
            continue
        row, col = divmod(i, per_row)
        x = col * dx if row % 2 == 0 else (per_row - 1 - col) * dx
        n["position"] = [x, row * dy]

    # Manual trigger sits next to the schedule trigger.
    if "Manual Run" in by_name:
        by_name["Manual Run"]["position"] = [0, 220]

    # Sub-nodes (model / output parser) hover above their chain node.
    for name, anchor in [
        ("Draft model", "Draft post"),
        ("Draft output parser", "Draft post"),
        ("Review model", "Compliance review"),
        ("Review output parser", "Compliance review"),
    ]:
        if name in by_name and anchor in by_name:
            a = by_name[anchor]["position"]
            by_name[name]["position"] = [a[0] + (0 if "model" in name else 240), a[1] - 240]

    # Branch chains run underneath the main flow.
    bottom = (len(MAIN_CHAIN) // per_row + 1) * dy
    for i, name in enumerate(["No new content", "Log no content", "Alert no content", "Send no-content alert"]):
        if name in by_name:
            by_name[name]["position"] = [i * dx, bottom]
    for i, name in enumerate(["Quarantine", "Mark quarantined", "Log quarantine", "Alert quarantine", "Send quarantine alert"]):
        if name in by_name:
            by_name[name]["position"] = [i * dx, bottom + dy]


def build_main():
    nodes = []
    conns = {}

    def link(src, dst, src_index=0, dst_index=0, src_type="main"):
        conns.setdefault(src, {"main": []})
        # ensure enough output slots
        while len(conns[src]["main"]) <= src_index:
            conns[src]["main"].append([])
        conns[src]["main"][src_index].append({"node": dst, "type": "main", "index": dst_index})

    def link_ai(src, dst, ai_type):
        conns.setdefault(src, {"main": []})
        conns[src][ai_type] = [[{"node": dst, "type": ai_type, "index": 0}]]

    def link_in(src, dst, src_index, dst_index):
        conns.setdefault(src, {"main": []})
        while len(conns[src]["main"]) <= src_index:
            conns[src]["main"].append([])
        conns[src]["main"][src_index].append({"node": dst, "type": "main", "index": dst_index})

    # ---- triggers ------------------------------------------------------- #
    nodes.append(node("Schedule", "n8n-nodes-base.scheduleTrigger", 1.4, {
        "rule": {"interval": [{"field": "cronExpression",
                               "expression": "={{ $env.SCHEDULE_CRON || '0 9 * * *' }}"}]},
        "misfirePolicy": "runOnce",
    }, 0, 0, note="Daily 09:00 Asia/Jakarta (SCHEDULE_CRON)", color="#5b8ff9"))
    nodes.append(node("Manual Run", "n8n-nodes-base.manualTrigger", 1, {}, 0, 220,
                      note="Run the pipeline on demand (demo / testing)", color="#5b8ff9"))

    # ---- config --------------------------------------------------------- #
    cfg_assignments = []
    for i, (name, value, typ) in enumerate([
        ("base_url", "={{ $env.BASE_URL || 'https://www.placidway.com' }}", "string"),
        ("crawl_delay", 3, "number"),
        ("llm_model", "={{ $env.LLM_MODEL || 'gpt-4o-mini' }}", "string"),
        ("auto_publish", "={{ $env.AUTO_PUBLISH === 'true' }}", "boolean"),
        ("publisher_mode", "={{ $env.PUBLISHER_MODE || 'simulate' }}", "string"),
        ("alert_channel", "={{ $env.ALERT_CHANNEL || '' }}", "string"),
        ("reviewer_notify", "={{ $env.REVIEWER_NOTIFY || '' }}", "string"),
        ("max_per_sitemap", 50, "number"),
    ]):
        cfg_assignments.append({"id": f"cfg-{i}", "name": name, "value": value, "type": typ})

    nodes.append(node("Config", "n8n-nodes-base.set", 3.5, {
        "mode": "manual",
        "assignments": {"assignments": cfg_assignments},
        "options": {"dotNotation": True},
    }, 220, 110, note="All tunables come from env vars - see README", color="#5b8ff9"))
    link("Schedule", "Config")
    link("Manual Run", "Config")

    # ---- robots + sitemap index ---------------------------------------- #
    nodes.append(http_node("Fetch robots.txt", "={{ $json.base_url || '" + BASE_URL + "' }}/robots.txt",
                           440, 110, "Read Crawl-delay + Sitemap directive"))
    link("Config", "Fetch robots.txt")
    nodes.append(code_node("Parse robots", PARSE_ROBOTS, 660, 110,
                           "Extract crawl_delay (seconds) and sitemap URL"))
    link("Fetch robots.txt", "Parse robots")
    nodes.append(http_node("Fetch sitemap index",
                           "={{ $('Parse robots').first().json.sitemap_url }}",
                           880, 110, "Sitemap index -> per-section sub-sitemaps"))
    link("Parse robots", "Fetch sitemap index")
    nodes.append(code_node("Parse sitemap index", PARSE_SITEMAP_INDEX, 1100, 110,
                           "Resolve the 4 content-type sub-sitemaps we care about"))
    link("Fetch sitemap index", "Parse sitemap index")

    # ---- section sitemaps (sequential, crawl-delay between) ------------- #
    sitemaps = [
        ("Articles", "articles_url", "article"),
        ("Packages", "packages_url", "package"),
        ("Profiles", "profiles_url", "profile"),
        ("Answers", "answers_url", "answer"),
    ]
    x = 1320
    prev = "Parse sitemap index"
    for label, field, ctype in sitemaps:
        w = wait_node(f"Wait {label}", x, 110, "Honor robots.txt Crawl-delay (3s)")
        nodes.append(w)
        link(prev, f"Wait {label}")
        x += 220
        f = http_node(f"Fetch {label} sitemap",
                      "={{ $('Parse sitemap index').first().json." + field + " }}",
                      x, 110, f"{label} sitemap ({ctype} URLs with lastmod)")
        nodes.append(f)
        link(f"Wait {label}", f"Fetch {label} sitemap")
        x += 220
        p = code_node(f"Parse {label} sitemap",
                      PARSE_SITEMAP.replace("__CONTENT_TYPE__", ctype), x, 110,
                      f"Keep the {label.lower()} with the freshest lastmod", color="#5ad8a6")
        nodes.append(p)
        link(f"Fetch {label} sitemap", f"Parse {label} sitemap")
        prev = f"Parse {label} sitemap"
        x += 220

    # ---- merge + select ------------------------------------------------- #
    nodes.append(node("Merge candidates", "n8n-nodes-base.merge", 3.2, {
        "mode": "append",
        "numberInputs": 4,
    }, x, 110, note="One list: all 4 content types combined", color="#5ad8a6"))
    for idx, (label, _, _) in enumerate(sitemaps):
        link(f"Parse {label} sitemap", "Merge candidates", 0, idx)
    x += 220
    prev = "Merge candidates"

    nodes.append(code_node("Flatten and sort", FLATTEN_AND_SORT, x, 110,
                           "Normalize, dedupe, sort by lastmod (tiebreak: URL desc)", color="#5ad8a6"))
    link(prev, "Flatten and sort")
    x += 220

    nodes.append(data_table_node("Load posted URLs", "posted_urls", "get", x, 110,
                                 "Dedupe ledger: everything we have already touched",
                                 extra={"returnAll": True, "orderBy": True,
                                        "orderByColumn": "createdAt",
                                        "orderByDirection": "DESC"},
                                 always_output=True))
    link("Flatten and sort", "Load posted URLs")
    x += 220

    nodes.append(code_node("Select candidate", SELECT_CANDIDATE, x, 110,
                           "Freshest unseen URL (excludes published/quarantined/pending_review)", color="#f6bd16"))
    link("Load posted URLs", "Select candidate")
    x += 220

    nodes.append(if_node("Has candidate?", [cond_bool("has_candidate", True, "c-has")], x, 110,
                         "All candidates used up? -> clean exit, not an error"))
    link("Select candidate", "Has candidate?")
    x += 220

    # ---- success path --------------------------------------------------- #
    sel_x = x
    nodes.append(data_table_node("Reserve URL", "posted_urls", "insert", sel_x, 0,
                                 "Claim the URL (state: reserved) so a re-run cannot double-post",
                                 extra={"columns": {"mappingMode": "defineBelow", "value": {
                                     "url": "={{ $json.selected.url }}",
                                     "content_type": "={{ $json.selected.content_type }}",
                                     "status": "reserved",
                                     "created_at": "={{ $now.toISO() }}",
                                     "updated_at": "={{ $now.toISO() }}",
                                 }}},
                                 color="#a855f7"))
    link_in("Has candidate?", "Reserve URL", 0, 0)
    x += 220

    nodes.append(wait_node("Wait before page fetch", x, 0,
                           "One more crawl-delay before the single item page request"))
    link("Reserve URL", "Wait before page fetch")
    x += 220

    nodes.append(http_node("Fetch item page", "={{ $json.url }}", x, 0,
                           "Fetch ONLY the one selected page (1 request, not 15k)"))
    link("Wait before page fetch", "Fetch item page")
    x += 220

    nodes.append(code_node("Parse item page", PARSE_ITEM_PAGE, x, 0,
                           "title / og:image / cleaned main body (<=6k chars)", color="#5ad8a6"))
    link("Fetch item page", "Parse item page")
    x += 220

    # ---- LLM draft ------------------------------------------------------ #
    draft_x = x
    nodes.append(code_node("Build draft prompt", BUILD_DRAFT_PROMPT, draft_x, 0,
                           "Prompt template with the fetched source injected", color="#e8684a"))
    link("Parse item page", "Build draft prompt")
    x += 220
    nodes.append(llm_chain("Draft post", draft_x + 220, 0,
                           "LLM draft: hook + value lines + CTA + hashtags (structured JSON)",
                           "={{ $json.draft_prompt }}", None, "Draft output parser"))
    link("Build draft prompt", "Draft post")
    x += 220

    nodes.append(node("Merge draft + source", "n8n-nodes-base.merge", 3.2, {
        "mode": "combine",
        "combineBy": "combineByPosition",
        "joinMode": "keepMatches",
        "outputDataFrom": "both",
        "numberInputs": 2,
    }, x, 0, note="Re-attach the source fields the LLM node drops", color="#5ad8a6"))
    link("Draft post", "Merge draft + source", 0, 0)
    link("Parse item page", "Merge draft + source", 0, 1)
    x += 220

    nodes.append(code_node("Compose post", COMPOSE_POST, x, 0,
                           "Flatten the LLM's {output:{...}} into post_text + hashtags", color="#5ad8a6"))
    link("Merge draft + source", "Compose post")
    x += 220

    nodes.append(code_node("Rule guardrail", RULE_GUARDRAIL, x, 0,
                           "Deterministic checks: length, link, blacklist, prices, hashtags, emoji", color="#ff9d4d"))
    link("Compose post", "Rule guardrail")
    x += 220

    nodes.append(if_node("Rule pass?", [cond_bool("guardrail.passed", True, "c-rule")], x, 0,
                         "Rule-based gate (cheap, free, instant)"))
    link("Rule guardrail", "Rule pass?")
    x += 220

    # ---- LLM compliance review ----------------------------------------- #
    review_x = x
    nodes.append(code_node("Build review prompt", BUILD_REVIEW_PROMPT, review_x, 0,
                           "Independent reviewer prompt (post + source body)", color="#e8684a"))
    link_in("Rule pass?", "Build review prompt", 0, 0)
    x += 220
    nodes.append(llm_chain("Compliance review", review_x + 220, 0,
                           "Independent LLM pass: medical claims / invented prices / unsupported facts",
                           "={{ $json.review_prompt }}", None, "Review output parser"))
    link("Build review prompt", "Compliance review")
    x += 220

    nodes.append(node("Merge review + draft", "n8n-nodes-base.merge", 3.2, {
        "mode": "combine",
        "combineBy": "combineByPosition",
        "joinMode": "keepMatches",
        "outputDataFrom": "both",
        "numberInputs": 2,
    }, x, 0, note="Combine verdict with the draft payload", color="#5ad8a6"))
    link("Compliance review", "Merge review + draft", 0, 0)
    link("Rule guardrail", "Merge review + draft", 0, 1)
    x += 220

    nodes.append(code_node("Normalize review verdict", REVIEW_PASS_CHECK, x, 0,
                           "Normalize the reviewer verdict to a boolean", color="#ff9d4d"))
    link("Merge review + draft", "Normalize review verdict")
    x += 220

    nodes.append(if_node("Review pass?", [cond_bool("review_passed", True, "c-review")], x, 0,
                         "LLM gate: any FAIL -> quarantine"))
    link("Normalize review verdict", "Review pass?")
    x += 220

    # ---- publish -------------------------------------------------------- #
    nodes.append(code_node("Publish (simulated)", PUBLISH, x, 0,
                           "simulate -> published_posts; AUTO_PUBLISH=false -> pending_review", color="#5ad8a6"))
    link_in("Review pass?", "Publish (simulated)", 0, 0)
    x += 220

    nodes.append(data_table_node("Write published_posts", "published_posts", "insert", x, 0,
                                 "The 'published post' record (simulated publisher)",
                                 extra={"columns": {"mappingMode": "defineBelow", "value": {
                                     "post_id": "={{ $json.post_id }}",
                                     "url": "={{ $json.source_url }}",
                                     "title": "={{ $json.title }}",
                                     "content_type": "={{ $json.content_type }}",
                                     "post_text": "={{ $json.post_text }}",
                                     "image_url": "={{ $json.image_url }}",
                                     "status": "={{ $json.status }}",
                                     "guardrail_result": "PASS",
                                     "publisher_mode": "={{ $json.publisher_mode }}",
                                     "created_at": "={{ $json.published_at }}",
                                 }}}))
    link("Publish (simulated)", "Write published_posts")
    x += 220

    nodes.append(data_table_node("Mark posted", "posted_urls", "update", x, 0,
                                 "Flip reserved -> published/pending_review",
                                 extra={
                                     "matchType": "allConditions",
                                     "filters": {"conditions": [
                                         {"keyName": "url", "condition": "eq",
                                          "keyValue": "={{ $json.url }}"}
                                     ]},
                                     "columns": {"mappingMode": "defineBelow", "value": {
                                         "status": "={{ $json.status }}",
                                         "updated_at": "={{ $now.toISO() }}",
                                     }},
                                 }))
    link("Write published_posts", "Mark posted")
    x += 220

    nodes.append(data_table_node("Log run", "run_logs", "insert", x, 0,
                                 "Append-only run log (audit trail)",
                                 extra={"columns": {"mappingMode": "defineBelow", "value": {
                                     "run_id": "={{ $('Publish (simulated)').first().json.run_id }}",
                                     "timestamp": "={{ $('Publish (simulated)').first().json.published_at }}",
                                     "selected_url": "={{ $('Publish (simulated)').first().json.source_url }}",
                                     "content_type": "={{ $('Publish (simulated)').first().json.content_type }}",
                                     "post_text": "={{ $('Publish (simulated)').first().json.post_text }}",
                                     "guardrail_result": "PASS",
                                     "publisher_mode": "={{ $('Publish (simulated)').first().json.publisher_mode }}",
                                     "post_id": "={{ $('Publish (simulated)').first().json.post_id }}",
                                     "status": "={{ $('Publish (simulated)').first().json.status }}",
                                     "error": "",
                                 }}}))
    link("Mark posted", "Log run")

    # ---- no-content branch --------------------------------------------- #
    nodes.append(code_node("No new content", NO_NEW_CONTENT, sel_x, 320,
                           "Clean exit: alert + log, NOT a workflow error", color="#dcdde1"))
    link_in("Has candidate?", "No new content", 1, 0)

    nodes.append(data_table_node("Log no content", "run_logs", "insert", sel_x + 220, 320,
                                 "Record the clean exit",
                                 extra={"columns": {"mappingMode": "defineBelow", "value": {
                                     "run_id": "={{ $json.run_id }}",
                                     "timestamp": "={{ $json.timestamp }}",
                                     "selected_url": "",
                                     "content_type": "",
                                     "post_text": "",
                                     "guardrail_result": "n/a",
                                     "publisher_mode": "n/a",
                                     "post_id": "",
                                     "status": "no_new_content",
                                     "error": "={{ $json.error }}",
                                 }}}))
    link("No new content", "Log no content")

    nodes.append(code_node("Alert no content", ALERT_NO_CONTENT, sel_x + 440, 320,
                           "Build the alert payload", color="#ff4d4f"))
    link("Log no content", "Alert no content")

    nodes.append(node("Send no-content alert", "n8n-nodes-base.httpRequest", 4.5, {
        "method": "POST",
        "url": "={{ $json.channel }}",
        "sendBody": True,
        "contentType": "raw",
        "body": "={{ JSON.stringify({text: $json.text, content: $json.text, message: $json.message}) }}",
        "options": {"timeout": 10000, "response": {"response": {"responseFormat": "text"}}},
    }, sel_x + 660, 320, note="ALERT_CHANNEL webhook (Telegram/Discord/generic)", color="#ff4d4f",
        on_error="continueRegularOutput"))
    link("Alert no content", "Send no-content alert")

    # ---- quarantine branch --------------------------------------------- #
    q_x = review_x
    nodes.append(code_node("Quarantine", QUARANTINE, q_x, 520,
                           "Mark the item quarantined; it will never be selected again", color="#ff4d4f"))
    link_in("Rule pass?", "Quarantine", 1, 0)
    link_in("Review pass?", "Quarantine", 1, 0)

    nodes.append(data_table_node("Mark quarantined", "posted_urls", "update", q_x + 220, 520,
                                 "reserved -> quarantined",
                                 extra={
                                     "matchType": "allConditions",
                                     "filters": {"conditions": [
                                         {"keyName": "url", "condition": "eq",
                                          "keyValue": "={{ $json.selected_url }}"}
                                     ]},
                                     "columns": {"mappingMode": "defineBelow", "value": {
                                         "status": "quarantined",
                                         "updated_at": "={{ $now.toISO() }}",
                                     }},
                                 }, color="#ff4d4f"))
    link("Quarantine", "Mark quarantined")

    nodes.append(data_table_node("Log quarantine", "run_logs", "insert", q_x + 440, 520,
                                 "Record the quarantine + reasons",
                                 extra={"columns": {"mappingMode": "defineBelow", "value": {
                                     "run_id": "={{ $('Quarantine').first().json.run_id }}",
                                     "timestamp": "={{ $('Quarantine').first().json.timestamp }}",
                                     "selected_url": "={{ $('Quarantine').first().json.selected_url }}",
                                     "content_type": "={{ $('Quarantine').first().json.content_type }}",
                                     "post_text": "={{ $('Quarantine').first().json.post_text }}",
                                     "guardrail_result": "FAIL",
                                     "publisher_mode": "n/a",
                                     "post_id": "",
                                     "status": "quarantined",
                                     "error": "={{ $('Quarantine').first().json.error }}",
                                 }}}, color="#ff4d4f"))
    link("Mark quarantined", "Log quarantine")

    nodes.append(code_node("Alert quarantine", ALERT_QUARANTINE, q_x + 660, 520,
                           "Build the quarantine alert", color="#ff4d4f"))
    link("Log quarantine", "Alert quarantine")

    nodes.append(node("Send quarantine alert", "n8n-nodes-base.httpRequest", 4.5, {
        "method": "POST",
        "url": "={{ $json.channel }}",
        "sendBody": True,
        "contentType": "raw",
        "body": "={{ JSON.stringify({text: $json.text, content: $json.text, message: $json.message}) }}",
        "options": {"timeout": 10000, "response": {"response": {"responseFormat": "text"}}},
    }, q_x + 880, 520, note="Human-in-the-loop: a FAIL must never be silent", color="#ff4d4f",
        on_error="continueRegularOutput"))
    link("Alert quarantine", "Send quarantine alert")

    # ---- LLM sub-nodes -------------------------------------------------- #
    nodes.append(openai_model("Draft model", draft_x + 220, -220,
                              "GPT-4o-mini via LLM_MODEL env var"))
    link_ai("Draft model", "Draft post", "ai_languageModel")
    nodes.append(output_parser("Draft output parser", draft_x + 440, -220,
                               "Force JSON: hook, value_lines, cta, hashtags, source_url",
                               json.dumps({
                                   "type": "object",
                                   "properties": {
                                       "hook": {"type": "string"},
                                       "value_lines": {"type": "array", "items": {"type": "string"}},
                                       "cta": {"type": "string"},
                                       "hashtags": {"type": "array", "items": {"type": "string"}},
                                       "source_url": {"type": "string"},
                                   },
                                   "required": ["hook", "value_lines", "cta", "hashtags", "source_url"],
                               }, indent=2)))
    link_ai("Draft output parser", "Draft post", "ai_outputParser")

    nodes.append(openai_model("Review model", review_x + 220, -220,
                              "Same model, independent prompt"))
    link_ai("Review model", "Compliance review", "ai_languageModel")
    nodes.append(output_parser("Review output parser", review_x + 440, -220,
                               "Force JSON: decision PASS/FAIL + reasons",
                               json.dumps({
                                   "type": "object",
                                   "properties": {
                                       "decision": {"type": "string"},
                                       "reasons": {"type": "array", "items": {"type": "string"}},
                                       "medical_claims": {"type": "array", "items": {"type": "string"}},
                                       "invented_prices": {"type": "array", "items": {"type": "string"}},
                                       "unsupported_facts": {"type": "array", "items": {"type": "string"}},
                                   },
                                   "required": ["decision", "reasons"],
                               }, indent=2)))
    link_ai("Review output parser", "Compliance review", "ai_outputParser")

    reposition(nodes)

    wf = {
        "name": "PlacidWay Daily Social Post",
        "nodes": nodes,
        "connections": conns,
        "active": False,
        "settings": {
            "timezone": "Asia/Jakarta",
            "saveExecutionProgress": True,
            "executionOrder": "v1",
        },
        "pinData": {},
        "meta": {
            "version": "2.47.3",
            "instanceId": "",
        },
    }
    return wf


# --------------------------------------------------------------------------- #
# Setup / error / approve workflows
# --------------------------------------------------------------------------- #

def build_setup():
    nodes = [
        node("Manual Setup", "n8n-nodes-base.manualTrigger", 1, {}, 0, 0,
             note="Run once to create the 3 data tables", color="#5b8ff9"),
    ]
    tables = {
        "posted_urls": [
            ("url", "string"), ("status", "string"), ("content_type", "string"),
            ("created_at", "string"), ("updated_at", "string"),
        ],
        "published_posts": [
            ("post_id", "string"), ("url", "string"), ("title", "string"),
            ("content_type", "string"), ("post_text", "string"),
            ("image_url", "string"), ("status", "string"),
            ("guardrail_result", "string"), ("publisher_mode", "string"),
            ("created_at", "string"),
        ],
        "run_logs": [
            ("run_id", "string"), ("timestamp", "string"), ("selected_url", "string"),
            ("content_type", "string"), ("post_text", "string"),
            ("guardrail_result", "string"), ("publisher_mode", "string"),
            ("post_id", "string"), ("status", "string"), ("error", "string"),
        ],
    }
    x = 220
    conns = {}
    prev = "Manual Setup"
    for name, cols in tables.items():
        label = f"Create {name}"
        nodes.append(node(label, "n8n-nodes-base.dataTable", 1.1, {
            "resource": "table",
            "operation": "create",
            "dataTableId": {"mode": "name", "value": name},
            "tableName": name,
            "columns": {"column": [{"name": c, "type": t} for c, t in cols]},
            "options": {"createIfNotExists": True},
        }, x, 0, note=f"{name}: {len(cols)} columns", color="#a855f7"))
        conns.setdefault(prev, {"main": [[{"node": label, "type": "main", "index": 0}]]})
        prev = label
        x += 220
    return {
        "name": "PlacidWay - Setup Tables",
        "nodes": nodes,
        "connections": conns,
        "active": False,
        "settings": {"timezone": "Asia/Jakarta"},
    }


def build_error_handler():
    nodes = [
        node("Error Trigger", "n8n-nodes-base.errorTrigger", 1, {}, 0, 0,
             note="Catches ANY unhandled error in the main workflow", color="#ff4d4f"),
        code_node("Build error record", ERROR_HANDLER, 220, 0,
                  "Normalize the error payload", color="#ff4d4f"),
        data_table_node("Log failure", "run_logs", "insert", 440, 0,
                        "Persist the failure - never silent",
                        extra={"columns": {"mappingMode": "defineBelow", "value": {
                            "run_id": "={{ $json.run_id }}",
                            "timestamp": "={{ $json.timestamp }}",
                            "selected_url": "",
                            "content_type": "",
                            "post_text": "",
                            "guardrail_result": "n/a",
                            "publisher_mode": "n/a",
                            "post_id": "",
                            "status": "failed",
                            "error": "={{ $json.error }}",
                        }}}, color="#ff4d4f"),
        code_node("Build alert", ERROR_ALERT, 660, 0, "Alert payload", color="#ff4d4f"),
        node("Send alert", "n8n-nodes-base.httpRequest", 4.5, {
            "method": "POST",
            "url": "={{ $json.channel }}",
            "sendBody": True,
            "contentType": "raw",
            "body": "={{ JSON.stringify({text: $json.text, content: $json.text, message: $json.message}) }}",
            "options": {"timeout": 10000, "response": {"response": {"responseFormat": "text"}}},
        }, 880, 0, note="ALERT_CHANNEL webhook", color="#ff4d4f",
            on_error="continueRegularOutput"),
        node("Stop", "n8n-nodes-base.stopAndError", 1, {
            "errorMessage": "={{ $('Build error record').first().json.error }}",
            "errorType": "errorMessage",
        }, 1100, 0, note="Surface the failure in the n8n UI", color="#ff4d4f"),
    ]
    conns = {
        "Error Trigger": {"main": [[{"node": "Build error record", "type": "main", "index": 0}]]},
        "Build error record": {"main": [[{"node": "Log failure", "type": "main", "index": 0}]]},
        "Log failure": {"main": [[{"node": "Build alert", "type": "main", "index": 0}]]},
        "Build alert": {"main": [[{"node": "Send alert", "type": "main", "index": 0}]]},
        "Send alert": {"main": [[{"node": "Stop", "type": "main", "index": 0}]]},
    }
    return {
        "name": "PlacidWay - Error Handler",
        "nodes": nodes,
        "connections": conns,
        "active": False,
        "settings": {"timezone": "Asia/Jakarta"},
    }


def build_approve():
    nodes = [
        node("Approve Post", "n8n-nodes-base.manualTrigger", 1, {}, 0, 0,
             note="Approve (or reject) a pending_review post", color="#5b8ff9"),
        node("Approval decision", "n8n-nodes-base.set", 3.5, {
            "mode": "manual",
            "assignments": {"assignments": [
                {"id": "ap-1", "name": "url",
                 "value": "={{ $json.url || 'https://www.placidway.com/article/...' }}", "type": "string"},
                {"id": "ap-2", "name": "decision", "value": "approved", "type": "string"},
            ]},
            "options": {"dotNotation": True},
        }, 220, 0, note="Set url + 'approved' (or 'rejected')", color="#5b8ff9"),
        data_table_node("Update posted_urls", "posted_urls", "update", 440, 0,
                        "pending_review -> published | quarantined",
                        extra={
                            "matchType": "allConditions",
                            "filters": {"conditions": [
                                {"keyName": "url", "condition": "eq",
                                 "keyValue": "={{ $json.url }}"}
                            ]},
                            "columns": {"mappingMode": "defineBelow", "value": {
                                "status": "={{ $json.decision === 'approved' ? 'published' : 'quarantined' }}",
                                "updated_at": "={{ $now.toISO() }}",
                            }},
                        }, color="#a855f7"),
        data_table_node("Update published_posts", "published_posts", "update", 660, 0,
                        "Flip the post record to match",
                        extra={
                            "matchType": "allConditions",
                            "filters": {"conditions": [
                                {"keyName": "url", "condition": "eq",
                                 "keyValue": "={{ $json.url }}"}
                            ]},
                            "columns": {"mappingMode": "defineBelow", "value": {
                                "status": "={{ $json.decision === 'approved' ? 'published' : 'quarantined' }}",
                            }},
                        }, color="#a855f7"),
    ]
    conns = {
        "Approve Post": {"main": [[{"node": "Approval decision", "type": "main", "index": 0}]]},
        "Approval decision": {"main": [[{"node": "Update posted_urls", "type": "main", "index": 0}]]},
        "Update posted_urls": {"main": [[{"node": "Update published_posts", "type": "main", "index": 0}]]},
    }
    return {
        "name": "PlacidWay - Approve / Reject Post",
        "nodes": nodes,
        "connections": conns,
        "active": False,
        "settings": {"timezone": "Asia/Jakarta"},
    }


def main():
    os.makedirs(OUT, exist_ok=True)
    for name, wf in [
        ("placidway-daily-post.json", build_main()),
        ("setup-tables.json", build_setup()),
        ("error-handler.json", build_error_handler()),
        ("approve-post.json", build_approve()),
    ]:
        path = os.path.join(OUT, name)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(wf, f, indent=2, ensure_ascii=False)
        print(f"wrote {path} ({len(wf['nodes'])} nodes)")


if __name__ == "__main__":
    main()
