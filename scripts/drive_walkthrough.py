#!/usr/bin/env python3
"""Drive the n8n UI through the English recording walkthrough.

Follows docs/walkthrough-en.md scene by scene:
  1. Intro        - zoom to fit
  2. Architecture - pan left -> right, dwell on key nodes
  3. Configuration- open the Config node
  4. Data tables  - visit all three tables
  5. Live run     - click Execute, watch nodes fire
  6. Results      - published_posts + run_logs
  7. Dedupe       - Execute again, different URL
  8. Wrap-up      - zoom to fit

Run:  python3 scripts/drive_walkthrough.py
"""
import os
import time
import sys
import traceback
import playwright.sync_api as pw

BASE = "http://localhost:5678"
WF_URL = f"{BASE}/workflow/UZv5qQMVxAe7C25s"
PID = "IfDqnb4HEywxZfTY"
DT_URL = f"{BASE}/projects/{PID}/datatables"

# table id -> name
TABLES = {
    "posted_urls": "UfkNmvLqqxPWNxKQ",
    "published_posts": "5EoQ58McoaYdI409",
    "run_logs": "tHWniyS65eoWU9zB",
}


def table_url(name):
    return f"{BASE}/projects/{PID}/datatables/{TABLES[name]}"


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


# pacing: multiply every wait by this factor (1.0 = script defaults)
SCALE = float(os.environ.get("WALKTHROUGH_SCALE", "2.0"))
CONTINUE_FILE = "/tmp/walkthrough_continue"
STATE_FILE = "/tmp/walkthrough_state"


def wait(sec, label=""):
    actual = int(sec * SCALE)
    log(f"  ... waiting {actual}s {label}")
    time.sleep(actual)


def wait_for_continue(next_scene):
    """Pause between scenes until the agent touches the continue file."""
    with open(STATE_FILE, "w") as f:
        f.write(str(next_scene))
    log(f"⏸  PAUSED before Scene {next_scene} - waiting for /tmp/walkthrough_continue")
    while True:
        if os.path.exists(CONTINUE_FILE):
            os.remove(CONTINUE_FILE)
            log(f"▶  continuing to Scene {next_scene}")
            return
        time.sleep(1)


# ---------------------------------------------------------------- helpers

def click_button_by_text(page, text, exact=False):
    """Click a <button> whose text contains `text`. JS click bypasses overlays."""
    return page.evaluate(
        """([text, exact]) => {
            const btns = Array.from(document.querySelectorAll('button'));
            const match = btns.find(b => {
                const t = (b.textContent || '').trim();
                return exact ? t === text : t.includes(text);
            });
            if (match) { match.click(); return (match.textContent||'').trim(); }
            return null;
        }""",
        [text, exact],
    )


def node_center(page, name):
    """Return the viewport center of a canvas node by its title."""
    return page.evaluate(
        """(name) => {
            const nodes = Array.from(document.querySelectorAll('.vue-flow__node'));
            const match = nodes.find(n => (n.textContent||'').includes(name));
            if (!match) return null;
            const r = match.getBoundingClientRect();
            return {x: r.left + r.width/2, y: r.top + r.height/2,
                    w: r.width, h: r.height,
                    left: r.left, top: r.top};
        }""",
        name,
    )


def click_node_center(page, name):
    """Double-click a node at its centre; returns the rect or None.

    n8n needs a double-click to open the node parameter panel.
    """
    rect = node_center(page, name)
    if not rect:
        log(f"  node '{name}' not found")
        return None
    if rect["w"] == 0:
        log(f"  node '{name}' has zero size")
        return None
    page.mouse.dblclick(rect["x"], rect["y"])
    return rect


def click_node_by_name(page, name):
    """Click a canvas node by its visible title."""
    return page.evaluate(
        """(name) => {
            const nodes = Array.from(document.querySelectorAll('.vue-flow__node'));
            const match = nodes.find(n => (n.textContent||'').includes(name));
            if (match) { match.click(); return true; }
            return false;
        }""",
        name,
    )


def pan(page, dx, dy):
    """Pan the vue-flow canvas with a synthetic wheel event."""
    return page.evaluate(
        """([dx, dy]) => {
            const pane = document.querySelector('.vue-flow__pane') || document.querySelector('.vue-flow');
            if (!pane) return false;
            const rect = pane.getBoundingClientRect();
            const opts = {bubbles:true, cancelable:true,
                          clientX: rect.left + rect.width/2,
                          clientY: rect.top + rect.height/2};
            pane.dispatchEvent(new WheelEvent('wheel',
                {...opts, deltaX: dx, deltaY: dy, deltaMode: 0}));
            return true;
        }""",
        [dx, dy],
    )


def api_fetch(page, path):
    """Fetch an n8n REST endpoint from the page context (cookies + browser-id)."""
    return page.evaluate(
        """async (path) => {
            try {
                const bid = localStorage.getItem('n8n-browserId');
                const r = await fetch(path, {credentials:'include',
                                           headers:{'browser-id': bid}});
                const d = await r.json();
                return {status: r.status, data: d};
            } catch (e) {
                return {status: 0, error: String(e)};
            }
        }""",
        path,
    )


def latest_execution(page):
    r = api_fetch(page, "/rest/executions?limit=1&sort=timestamp:DESC")
    try:
        results = r["data"]["data"]["results"]
        if results:
            e = results[0]
            return {"id": e["id"], "status": e["status"], "wf": e.get("workflowName")}
    except Exception:
        pass
    return None


def wait_for_new_execution(page, prev_id, timeout=360):
    """Poll until a new execution appears and reaches a terminal state."""
    start = time.time()
    while time.time() - start < timeout:
        e = latest_execution(page)
        if e and e["id"] != prev_id:
            if e["status"] in ("success", "failed", "crashed"):
                return e
            log(f"  execution {e['id'][:8]} status={e['status']}")
        time.sleep(5)
    return None


def dismiss_popovers(page):
    page.keyboard.press("Escape")
    page.wait_for_timeout(400)
    # click empty canvas area to clear focus
    try:
        page.mouse.click(200, 500)
    except Exception:
        pass
    page.wait_for_timeout(400)


def screenshot(page, name):
    try:
        page.screenshot(path=f"{name}.png")
        log(f"  screenshot -> {name}.png")
    except Exception as e:
        log(f"  screenshot failed: {e}")


# ---------------------------------------------------------------- scenes

def scene_intro(page):
    log("SCENE 1 - Intro: zoom to fit")
    dismiss_popovers(page)
    click_button_by_text(page, "Zoom to Fit")
    wait(3)
    screenshot(page, "rec-01-intro")
    wait(25, "(narrate intro)")


def scene_architecture(page):
    log("SCENE 2 - Architecture: pan left -> right")
    dismiss_popovers(page)
    click_button_by_text(page, "Zoom to Fit")
    wait(2)
    # zoom in a little so the pan is visible
    click_button_by_text(page, "Zoom In")
    wait(1)
    click_button_by_text(page, "Zoom In")
    wait(2)

    # left side first (triggers + config + robots)
    pan(page, -1400, -200)
    wait(2)
    screenshot(page, "rec-02a-left")
    wait(12, "(triggers, config, robots.txt)")

    # sitemaps
    pan(page, 1200, 0)
    wait(2)
    screenshot(page, "rec-02b-sitemaps")
    wait(12, "(wait/fetch/parse x4)")

    # merge + sort + ledger + reserve
    pan(page, 1200, 0)
    wait(2)
    screenshot(page, "rec-02c-merge")
    wait(12, "(merge, sort, ledger, reserve)")

    # LLM + guardrails
    pan(page, 1200, 0)
    wait(2)
    screenshot(page, "rec-02d-llm")
    wait(12, "(draft, rule guardrail, review)")

    # publish + branches
    pan(page, 1200, 0)
    wait(2)
    pan(page, 0, 600)
    wait(2)
    screenshot(page, "rec-02e-publish")
    wait(12, "(publish, quarantine branch)")

    # back to fit
    pan(page, -1200, -600)
    wait(1)
    click_button_by_text(page, "Zoom to Fit")
    wait(2)


def scene_config(page):
    log("SCENE 3 - Configuration: open Config node")
    dismiss_popovers(page)
    click_button_by_text(page, "Zoom to Fit")
    wait(3)
    # click the Config node at its centre (single click opens the panel)
    rect = click_node_center(page, "Config")
    log(f"  Config node rect: {rect}")
    wait(4)
    # if the panel didn't open, try again
    panel = page.evaluate(
        """() => !!document.querySelector('[data-test-id="node-parameters"], .node-parameters, aside[aria-label*="Parameter"]')"""
    )
    if not panel:
        log("  panel not open, retrying click")
        click_node_center(page, "Config")
        wait(3)
    screenshot(page, "rec-03-config")
    wait(25, "(narrate .env + AUTO_PUBLISH + PUBLISHER_MODE)")
    page.keyboard.press("Escape")
    wait(1)


def scene_data_tables(page):
    log("SCENE 4 - Data tables")
    page.goto(DT_URL)
    page.wait_for_load_state("networkidle")
    wait(3)
    screenshot(page, "rec-04-data-tables")
    wait(18, "(three tables)")

    for name, shot in (("posted_urls", "rec-04a-posted-urls"),
                       ("published_posts", "rec-04b-published"),
                       ("run_logs", "rec-04c-run-logs")):
        page.goto(table_url(name))
        page.wait_for_load_state("networkidle")
        wait(4)
        screenshot(page, shot)
        wait(10, f"({name})")


def scene_live_run(page):
    log("SCENE 5 - LIVE RUN")
    page.goto(WF_URL)
    page.wait_for_load_state("networkidle")
    wait(4)
    dismiss_popovers(page)
    click_button_by_text(page, "Zoom to Fit")
    wait(2)

    prev = latest_execution(page)
    prev_id = prev["id"] if prev else None
    log(f"  previous execution id: {prev_id}")

    # click Execute (Manual Run) via data-test-id
    clicked = page.evaluate(
        """() => {
            const btns = Array.from(document.querySelectorAll(
                'button[data-test-id^="execute-workflow-button-"]'));
            // prefer the Manual Run trigger, fall back to any
            let target = btns.find(b => b.dataset.testId.includes('Manual Run'))
                      || btns.find(b => !b.dataset.testId.includes('Schedule'));
            if (target) { target.click(); return target.dataset.testId; }
            return null;
        }"""
    )
    log(f"  clicked: {clicked}")
    if not clicked:
        # fallback: any execute button
        clicked = click_button_by_text(page, "Execute workflow")
        log(f"  fallback clicked: {clicked}")

    screenshot(page, "rec-05-running")
    wait(15, "(robots.txt firing)")

    # zoom into the active region a few times while it runs
    for i, (dx, dy) in enumerate([(-600, -100), (600, 0), (600, 0)]):
        wait(20)
        pan(page, dx, dy)
        screenshot(page, f"rec-05-run-{i+1}")

    log("  waiting for execution to finish (up to 6 min)...")
    result = wait_for_new_execution(page, prev_id, timeout=360)
    log(f"  execution result: {result}")
    screenshot(page, "rec-05-done")
    wait(10, "(show final state)")


def scene_results(page):
    log("SCENE 6 - Results")
    page.goto(table_url("published_posts"))
    page.wait_for_load_state("networkidle")
    wait(4)
    screenshot(page, "rec-06a-published")
    wait(18, "(the post)")
    page.goto(table_url("run_logs"))
    page.wait_for_load_state("networkidle")
    wait(4)
    screenshot(page, "rec-06b-run-logs")
    wait(12, "(audit trail)")


def scene_dedupe(page):
    log("SCENE 7 - Dedupe: run again")
    page.goto(WF_URL)
    page.wait_for_load_state("networkidle")
    wait(4)
    dismiss_popovers(page)
    click_button_by_text(page, "Zoom to Fit")
    wait(2)

    prev = latest_execution(page)
    prev_id = prev["id"] if prev else None

    clicked = page.evaluate(
        """() => {
            const btns = Array.from(document.querySelectorAll(
                'button[data-test-id^="execute-workflow-button-"]'));
            let target = btns.find(b => b.dataset.testId.includes('Manual Run'))
                      || btns.find(b => !b.dataset.testId.includes('Schedule'));
            if (target) { target.click(); return target.dataset.testId; }
            return null;
        }"""
    )
    log(f"  clicked: {clicked}")

    wait(20, "(select candidate should skip the used URL)")
    screenshot(page, "rec-07-running")

    result = wait_for_new_execution(page, prev_id, timeout=360)
    log(f"  execution result: {result}")
    screenshot(page, "rec-07-done")

    page.goto(table_url("published_posts"))
    page.wait_for_load_state("networkidle")
    wait(4)
    screenshot(page, "rec-07b-two-posts")
    wait(15, "(two different URLs)")


def scene_wrap(page):
    log("SCENE 8 - Wrap-up")
    page.goto(WF_URL)
    page.wait_for_load_state("networkidle")
    wait(4)
    dismiss_popovers(page)
    click_button_by_text(page, "Zoom to Fit")
    wait(3)
    screenshot(page, "rec-08-wrap")
    wait(20, "(closing remarks)")


# ---------------------------------------------------------------- main

def main():
    with pw.sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--start-maximized"])
        ctx = browser.new_context(viewport={"width": 1920, "height": 1080})
        page = ctx.new_page()

        log("opening signin")
        page.goto(f"{BASE}/signin")
        page.wait_for_load_state("networkidle")
        wait(2)

        page.locator('input[type="email"], input[name="email"], #email').first.fill("admin@example.com")
        page.locator('input[type="password"], input[name="password"]').first.fill("Test1234")
        page.locator('button:has-text("Sign in"), button[type="submit"]').first.click()
        page.wait_for_load_state("networkidle")
        wait(4)
        log(f"logged in, url={page.url}")

        page.goto(WF_URL)
        page.wait_for_load_state("networkidle")
        wait(5)
        log("workflow loaded")

        scenes = [
            scene_intro,
            scene_architecture,
            scene_config,
            scene_data_tables,
            scene_live_run,
            scene_results,
            scene_dedupe,
            scene_wrap,
        ]

        only = None
        if len(sys.argv) > 1:
            only = [int(x) for x in sys.argv[1].split(",")]

        for i, scene in enumerate(scenes, 1):
            if only and i not in only:
                continue
            try:
                scene(page)
            except Exception:
                log(f"SCENE {i} FAILED:\n{traceback.format_exc()}")
                screenshot(page, f"rec-error-scene{i}")
            # pause between scenes (skip after the last one)
            if i < len(scenes):
                wait_for_continue(i + 1)

        log("ALL SCENES DONE")
        wait(5)
        browser.close()


if __name__ == "__main__":
    main()
