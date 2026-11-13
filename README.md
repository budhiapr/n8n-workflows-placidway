# PlacidWay → Facebook Daily Social Post Automation

An n8n take-home build: every day at 09:00 (Asia/Jakarta) the workflow pulls the
freshest unseen PlacidWay page from the XML sitemaps, drafts an on-brand
Facebook post with an LLM, runs it through a two-layer guardrail, and "publishes"
it — in this demo the publisher is **simulated**, so nothing ever touches a real
Facebook page.

> **No real posting.** `PUBLISHER_MODE=simulate` writes the finished post to an
> n8n Data Table. The publisher is isolated behind a single node so a real
> Facebook Graph API call can replace it later without touching the pipeline.

---

## Submission

| Item | Link / location |
|---|---|
| Public repository | https://github.com/budhiapr/n8n-workflows-placidway |
| Exported workflow | [`workflows/placidway-daily-post.json`](workflows/placidway-daily-post.json) (+ `setup-tables`, `error-handler`, `approve-post`) |
| Sample posts | [`samples/sample-posts.json`](samples/sample-posts.json) — 6 posts, source URLs included |
| Screen recording | [`n8n-automation-budhi-apriyanto.mp4`](n8n-automation-budhi-apriyanto.mp4) |

---

## Table of contents

1. [Architecture](#architecture)
2. [Repository layout](#repository-layout)
3. [Prerequisites](#prerequisites)
4. [Setup](#setup)
5. [Environment variables](#environment-variables)
6. [Running it](#running-it)
7. [Data tables](#data-tables)
8. [Guardrails](#guardrails)
9. [Failure handling](#failure-handling)
10. [Sample posts](#sample-posts)
11. [Verification performed](#verification-performed)
12. [Walkthrough scripts](#walkthrough-scripts)
13. [Deliverables checklist](#deliverables-checklist)

---

## Architecture

```
┌─ Schedule (cron 0 9 * * *, Asia/Jakarta) ─┐
└─ Manual Run (testing) ─────────────────────┘
                    │
                    ▼
             Config (env-driven)
                    │
     ┌──────────────┴───────────────┐
     ▼                              ▼
Fetch robots.txt              (Crawl-delay: 3,
     │                         Sitemap: …/sitemap.xml)
     ▼
Parse robots ──► Fetch sitemap index ──► Parse sitemap index
                                             │
   ┌─────────────────────────────────────────┼───────────────────────────┐
   ▼                                         ▼                           ▼
Wait 3s → Fetch articles sitemap     Wait 3s → Fetch packages sitemap   … profiles, answers
   │                                         │                           │
   └──────────────────► Merge (append) ◄──────┴───────────────────────────┘
                                             │
                                             ▼
                              Flatten, dedupe, sort by lastmod
                                             │
                                             ▼
                     Load posted_urls (dedupe ledger)
                                             │
                                             ▼
                 Select candidate (freshest URL not yet
                 published / quarantined / pending_review)
                              │              │
                       has candidate     no candidate
                              │              │
                              ▼              ▼
                      Reserve URL      No new content → log + alert
                      (status=reserved)        (clean exit, not an error)
                              │
                              ▼
                  Wait 3s → Fetch item page (exactly 1 request)
                              │
                              ▼
                    Parse item page (title, og:image, body ≤6k)
                              │
                              ▼
                     Build draft prompt (Code)
                              │
                              ▼
        ┌───────────── Draft post (LLM, structured output) ─────────────┐
        │  Model: OpenAI-compatible chat model   Output Parser: JSON    │
        └───────────────────────────────────────────────────────────────┘
                              │
                              ▼
              Merge draft + source → Compose post
                              │
                              ▼
                  ┌──── Rule guardrail (deterministic) ────┐
                  │  length, link, blacklist, verbatim       │
                  │  prices, hashtags, emoji, superlatives   │
                  └───────────────────┬──────────────────────┘
                                      │ PASS
                                      ▼
                  ┌──── Compliance review (independent LLM pass) ────┐
                  │  medical claims / invented prices / unsupported   │
                  └───────────────────┬───────────────────────────────┘
                                      │ PASS
                                      ▼
                        Publish (simulated)
                              │
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
       Write          Mark posted        Log run
       published_posts reserved →      (run_logs)
       (pending_review  pending_review
        or published)
                                      ▲
        Any FAIL at either gate ───────┘
              │
              ▼
        Quarantine → Mark quarantined → Log quarantine → Alert
```

### Why these choices

| Decision | Rationale |
|---|---|
| **Sitemap-first discovery** | One cheap request lists thousands of URLs with `lastmod`. No DOM scraping for discovery, trivially rate-limited. Validated by spike. |
| **Sequential fetches with Wait nodes** | `Crawl-delay: 3` from `robots.txt` is honoured literally. A `SplitInBatches` loop would make the delay harder to prove in a walkthrough. |
| **Only the selected page is fetched** | 1 request per run instead of thousands. |
| **Data Tables, not Google Sheets** | Native to n8n, no extra OAuth, visible in the UI during the demo. |
| **State machine `reserved → published \| pending_review \| quarantined`** | n8n has no cross-node transactions. Reserving first means a crash mid-run leaves a retryable `reserved` row instead of a silent double-post. |
| **Rule guardrail before the LLM pass** | Deterministic checks are free and instant; the LLM pass only runs on posts that already pass the cheap checks. |
| **`AUTO_PUBLISH=false` default** | Approval mode is the safe default for a demo and exercises the reviewer path. |
| **Env-driven config** | Every tunable is in `.env`; the workflow JSON stays identical across environments. |

---

## Repository layout

```
.
├── docker-compose.yml          # n8n service (pinned 2.47.3)
├── .env.example                # env template
├── .env                        # local values (gitignored)
├── workflows/                  # the deliverable: importable n8n JSON
│   ├── placidway-daily-post.json   # main pipeline (56 nodes)
│   ├── setup-tables.json           # run once: creates the 3 data tables
│   ├── error-handler.json          # Error Trigger → log → alert
│   └── approve-post.json           # manual approve/reject of pending_review
├── scripts/
│   ├── build_workflow.py       # generator for everything in workflows/
│   ├── reload_workflows.py     # delete + re-import workflows via the API
│   ├── setup_demo.py           # one-time instance wiring (credential, error workflow)
│   ├── serve_workflows.py      # tiny CORS static server (local dev only)
│   └── mock_openai.py          # offline OpenAI-compatible mock for testing
└── samples/
    ├── sample-posts.json       # 3 posts from real PlacidWay pages
    └── raw/                    # extracted text of those pages (evidence)
```

> `workflows/*.json` are **generated**. Edit `scripts/build_workflow.py` and
> re-run `python3 scripts/build_workflow.py` — do not hand-edit the JSON.

---

## Prerequisites

- Docker + Docker Compose v2
- Python 3.9+ (only for the helper scripts; no pip packages required)
- An OpenAI-compatible API key (the default config points at [`kenari.id`](https://kenari.id),
  model `laguna-xs-2-1:free`)

---

## Setup

### 1. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in at minimum:

```bash
OPENAI_API_KEY=your-key-here
```

### 2. Start n8n

```bash
docker compose up -d
```

n8n comes up on <http://localhost:5678>. On first load it asks you to create an
owner account — that email/password is what `scripts/setup_demo.py` uses, so
export them if they differ from the defaults:

```bash
export N8N_EMAIL=you@example.com
export N8N_PASSWORD=your-password
```

### 3. Create the owner credential

The LLM nodes need an OpenAI-compatible credential named **`OpenAI API`**.
`scripts/setup_demo.py` creates it for you (and points it at `LLM_BASE_URL`), so
this step is optional — but if you prefer the UI:

> Credentials → Add credential → **OpenAI** → API Key = your key,
> **Base URL** = `https://kenari.id/v1`

### 4. Import the workflows

Either drag-and-drop the four files from `workflows/` into the n8n canvas
(**Import Workflow from File**), or use the helper:

```bash
python3 scripts/reload_workflows.py
```

`reload_workflows.py` archives and deletes any existing `PlacidWay*` workflows
first, so it is safe to re-run while iterating.

### 5. One-time instance wiring

```bash
python3 scripts/setup_demo.py
```

This does the things that cannot be baked into the exported JSON because they
are instance-specific:

1. Creates (or updates) the **`OpenAI API`** credential with `LLM_BASE_URL`.
2. Points both LLM nodes at that credential id.
3. Activates the error handler and wires it as the main workflow's
   `settings.errorWorkflow`.

### 6. Create the data tables

Open **PlacidWay - Setup Tables** and click **Execute Workflow**. It creates
`posted_urls`, `published_posts` and `run_logs`.

---

## Environment variables

| Var | Default | Purpose |
|---|---|---|
| `SCHEDULE_CRON` | `0 9 * * *` | Daily run time |
| `N8N_TZ` / `GENERIC_TIMEZONE` | `Asia/Jakarta` | Cron + timestamps |
| `CRAWL_DELAY` | `3` | Seconds between requests (from `robots.txt`) |
| `LLM_BASE_URL` | `https://kenari.id/v1` | OpenAI-compatible endpoint |
| `LLM_MODEL` | `laguna-xs-2-1:free` | Model for draft + review |
| `OPENAI_API_KEY` | — | API key for the LLM |
| `AUTO_PUBLISH` | `false` | `true` = publish immediately, `false` = approval mode |
| `PUBLISHER_MODE` | `simulate` | `simulate` (today) or `facebook` (future) |
| `ALERT_CHANNEL` | — | Generic webhook for quarantine / no-content / failure alerts |
| `REVIEWER_NOTIFY` | — | Reviewer notification channel |
| `N8N_ENCRYPTION_KEY` | — | Encrypts credentials in the n8n database |
| `N8N_SECURE_COOKIE` | `false` | Set `true` when serving n8n over HTTPS |
| `N8N_BLOCK_ENV_ACCESS_IN_NODE` | `false` | Must stay `false`: the workflow reads `$env` |

---

## Running it

**Manual run (what the recording shows):**

1. Open **PlacidWay Daily Social Post**.
2. Click **Execute workflow** (bottom centre).
3. Watch the canvas: sitemaps → select → fetch → LLM → guardrails → publish.
4. Open **Data tables → `published_posts`** to see the finished post, and
   **`run_logs`** for the audit trail.

A full run takes roughly **25–35 seconds**, most of it the deliberate
`Crawl-delay: 3` waits.

> The three data tables start **empty** on a fresh setup. The first run
> populates them; `samples/sample-posts.json` shows what the finished post
> records look like.

**Scheduled run:** open the workflow and click **Publish** (or
`POST /rest/workflows/{id}/activate`). The Schedule trigger then fires daily at
09:00 Asia/Jakarta.

**Approving a draft:** open **PlacidWay - Approve / Reject Post**, set the `url`
and `decision` (`approved` / `rejected`), and run it. `pending_review` flips to
`published` or `quarantined` in both `posted_urls` and `published_posts`.

### Offline testing with the mock LLM

`scripts/mock_openai.py` is a tiny OpenAI-compatible server used during
development so the pipeline can be exercised without spending API credits.
It answers `/v1/chat/completions` (and `/v1/responses`) with deterministic,
source-derived content.

```bash
python3 scripts/mock_openai.py        # listens on :8766
```

Then point the credential's **Base URL** at
`http://host.docker.internal:8766/v1` and run the workflow. Set
`MOCK_FORCE_REVIEW=1` to make the reviewer answer `FAIL`, which exercises the
quarantine branch.

The mock is a **testing aid, not part of the demo** — the shipped `.env`
points at the real provider.

> **Note on error workflows:** n8n only fires a workflow's error workflow for
> **non-manual** executions (schedule, trigger, retry). A failed *manual* run
> shows the error on the canvas but does not trigger the handler. To see the
> handler fire, retry a failed execution (Executions → the failed run →
> **Retry**), or let the schedule produce the failure.

---

## Data tables

### `posted_urls` — the dedupe ledger

| Column | Type | Notes |
|---|---|---|
| `url` | string | PlacidWay page URL (primary key in practice) |
| `status` | string | `reserved` → `published` \| `pending_review` \| `quarantined` |
| `content_type` | string | `article` \| `package` \| `profile` \| `answer` |
| `created_at` / `updated_at` | string | ISO timestamps |

Selection only considers URLs whose status is **not** `published`,
`quarantined` or `pending_review`. A `reserved` row left behind by a crashed run
is retryable, so a bad run can never cause a silent double-post.

### `published_posts` — the simulated publisher

`post_id` (`sim_<timestamp>`), `url`, `title`, `content_type`, `post_text`,
`image_url`, `status`, `guardrail_result`, `publisher_mode`, `created_at`.

### `run_logs` — the audit trail

One row per run: `run_id`, `timestamp`, `selected_url`, `content_type`,
`post_text`, `guardrail_result`, `publisher_mode`, `post_id`, `status`, `error`.
Written on every path — success, `no_new_content`, `quarantined`, and `failed`.

---

## Guardrails

Two independent layers. Both must pass before anything is published.

### Layer 1 — deterministic (Code node, free and instant)

- **Length**: fewer than 500 characters before the link.
- **Structure**: ≥3 lines, 3–5 hashtags.
- **Link**: the only URL in the post must equal the source URL.
- **Blacklist**: `guarantee`, `cure`, `100%`, `best results`, `risk-free`,
  `miracle`, `no side effects`, `permanent result`, `clinically proven`,
  `painless`, `zero pain`, `instant result`, `first in the world`, `number one`.
- **Superlatives in the hook**: `best`, `cheapest`, `lowest cost`, `top-rated`,
  `world-class`, `leading`, `premier`.
- **Prices**: every `$amount` in the post must appear **verbatim** in the source
  body — this is the guard that stops an LLM from inventing a price.
- **Emojis**: at most 2.

### Layer 2 — LLM compliance review (independent prompt)

A second model call with a reviewer prompt: *does this post make medical claims,
promise outcomes, invent prices, or state facts not supported by the source?*
Answers `PASS`/`FAIL` plus reasons, enforced by a structured output schema.

Any `FAIL` → the URL is marked `quarantined` (permanently excluded from future
selection), the run is logged, and an alert is sent.

---

## Failure handling

| Failure | Detection | Handling |
|---|---|---|
| PlacidWay down / timeout | HTTP node error | 3 tries, 2s apart; then the error workflow logs `failed` and alerts. URL stays `reserved` → retryable. |
| All content already posted | Empty eligible set | Logs `no_new_content`, sends an info alert, exits **successfully** (not an error). |
| LLM API error | AI node error | Retried by n8n, then the error workflow alerts. |
| Guardrail FAIL | Rule or LLM gate | Quarantine + log + alert. Quarantined URLs are never selected again. |
| Re-run the same day | `posted_urls` lookup | Already `pending_review`/`published` → next candidate is selected. |

**Nothing fails silently.** Every path writes to `run_logs`, and the
`PlacidWay - Error Handler` workflow (active) catches any unhandled error,
writes a `failed` row, POSTs to `ALERT_CHANNEL`, and re-raises so the execution
shows as failed in the UI.

---

## Sample posts

`samples/sample-posts.json` contains five posts from real PlacidWay pages:

| # | Source | Content type | Produced by |
|---|---|---|---|
| 1 | `article/5693/…` Gastric Sleeve in Tijuana | article | curated, validated against the guardrail code |
| 2 | `article/5677/…` Rhinoplasty in Istanbul | article | curated, validated against the guardrail code |
| 3 | `article/5527/…` Hair Transplant in Turkey | article | curated, validated against the guardrail code |
| 4 | `profile/326534/…` Wolistic Center, Istanbul | profile | **live workflow run** with the real LLM |
| 5 | `profile/325417/…` Dentaray Clinic, Istanbul | profile | **live workflow run** with the real LLM |

`samples/raw/article_*.txt` holds the extracted page text the curated posts were
written against, so the verbatim-price rule can be re-checked.

The three curated posts were validated against the workflow's **actual**
rule-guardrail code:

```
sample-1: passed=True errors=[] pre_link_len=268 hashtags=4 prices=['$4,000','$6,500','$15,000','$25,000']
sample-2: passed=True errors=[] pre_link_len=315 hashtags=4 prices=['$3,000','$7,500','$6,500','$13,000']
sample-3: passed=True errors=[] pre_link_len=277 hashtags=4 prices=['$1,500','$5,000','$3,800','$15,000']
```

---

## Verification performed

End-to-end runs against a live local n8n 2.47.3 (Docker) with the real
PlacidWay sitemaps, real item pages, and the real LLM (`laguna-xs-2-1:free`
through kenari.id):

- **Full pipeline**: all 41 nodes of the main workflow executed successfully,
  including both LLM calls and both guardrail gates.
- **Dedupe**: a second run selected a *different* URL, because the first was
  already `pending_review`.
- **State machine**: `posted_urls` flipped `reserved → pending_review`;
  `published_posts` and `run_logs` each got exactly one row per run.
- **Guardrail**: the three sample posts pass the deterministic layer with zero
  errors (length, hashtags, verbatim prices, no blacklisted terms). During
  development the rule layer also caught a genuine violation (only 2 hashtags)
  and quarantined the item correctly.
- **Approval mode**: `AUTO_PUBLISH=false` produced `pending_review`, not
  `published`.
- **Error path**: a forced LLM failure (bad API key) produced a `failed` row in
  `run_logs` with the real error message via the error-handler workflow.

Real output from the first scheduled-style run:

```
Find aesthetic and wellness services in Istanbul
- Plastic surgery and medical aesthetics available
- Women's health and nutrition services offered
- Psychology consultations provided
- Published June 4, 2026, updated November 10, 2026
Read more:
https://www.placidway.com/profile/326534/Wolistic-Center
#MedicalTourism #AestheticClinic #Istanbul #WellnessCenter
```

---

## Walkthrough scripts

Two full narration scripts (shot list, timing, word-for-word narration, visual
checklist) were used to record the demo — one in Bahasa Indonesia, one in
English. Both run about 8–9 minutes and follow the same structure:
intro → architecture → configuration → data tables → live run → results →
dedupe → wrap-up.

---

## Deliverables checklist

Mapped against the AI Automation Candidate Assessment:

| # | Requirement | Where |
|---|---|---|
| 1 | Workflow source: exported workflow file | `workflows/placidway-daily-post.json` (+ `setup-tables`, `error-handler`, `approve-post`) |
| 2 | README: setup, env vars, how to run, architecture | `README.md` |
| 3 | Sample output: ≥3 posts from real PlacidWay pages + source URLs | `samples/sample-posts.json` (6 posts, URLs verified HTTP 200) |
| 4 | 5–10 min narrated screen recording | recorded (live run → post → results → dedupe) |

### Functional requirements coverage

| Requirement | Implementation |
|---|---|
| Daily schedule **and** manual run | `Schedule` (cron `0 9 * * *`, Asia/Jakarta) + `Manual Run` trigger |
| Extract title, URL, body/summary, featured image | sitemap index → 4 category sitemaps → single item-page fetch; `og:image` captured |
| Select one item per run, never repeat a URL | `posted_urls` ledger + `reserved`/`published`/`quarantined`/`pending_review` states |
| LLM post: hook, 2–4 value lines, CTA, link, 3–5 hashtags, <500 chars before link | structured draft node + `Compose post`, deterministic length/hashtag checks |
| Guardrails: rule-based + second LLM pass | `Rule guardrail` (length, hashtags, exact link, verbatim prices, blacklist) then independent `Compliance review` LLM |
| Publish to test page **or** simulation mode | `PUBLISHER_MODE=simulate` writes to `published_posts` (real Graph API swap is isolated in one node) |
| Auto-publish vs approval mode flag | `AUTO_PUBLISH=false` → posts land as `pending_review` |
| Log each run + retry/alert on failure | `run_logs` table + error-handler workflow (Error Trigger → log → alert → stopAndError) |

> **Note on the recording requirement:** the assessment asks for the post to be shown
> "on your test Facebook Page", but also explicitly permits running in draft/simulation
> mode. This build uses simulation — the finished post is shown in the `published_posts`
> data table inside n8n instead of a Facebook page.

> **Stretch goals** (variety rotation, image branding, cross-posting, dashboard,
> engagement read-back) were intentionally **not** pursued to keep scope on the core
> deliverables.

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `ExpressionError: access to env vars denied` | `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` in `.env` |
| Login works but the page bounces back to sign-in | `N8N_SECURE_COOKIE=false` when using plain HTTP |
| `A Model sub-node must be connected and enabled` | Re-run `scripts/setup_demo.py` |
| `The selected error workflow has no published version` | Run `scripts/setup_demo.py` (it activates the error handler first) |
| LLM node returns an empty/invalid structure | Check `LLM_BASE_URL` and that the model supports the Chat Completions endpoint |
| Nothing in `published_posts` | Check `run_logs` for `no_new_content`, and confirm the three data tables exist |

## License / attribution

Built as a take-home demonstration. PlacidWay content is used only as public
source material for the extraction exercise; no pages are republished.
