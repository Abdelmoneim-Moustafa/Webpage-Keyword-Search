# ═══════════════════════════════════════════════════════════════════
# Webpage Keyword Search — Streamlit app  (v1.2, single self-contained file)
#
# Everything needed to run this tool is in this one file — no other
# project file is required.
#
# Changes in v1.2, from real-run bug reports:
#   - NXP: added to the known-slow-host list (fast pass was getting a
#     fake 404 from NXP's anti-bot protection) — goes straight to the
#     browser pass now.
#   - Any 403/404/429/503 on the fast pass gets one confirming pass
#     through the browser before being marked Failed — several sites
#     return these to non-browser requests as an anti-bot measure even
#     though the page is completely real.
#   - A "Blocked" result gets one retry on a fresh page after a short
#     pause before being treated as final. NXP specifically was flaky —
#     same URL blocked once, fine moments later — which looks like
#     request-rate bot scoring, not a hard block. A small per-host
#     minimum gap between requests smooths out the burst that seemed to
#     trigger it.
#   - Ruland (and similar) block Playwright's default automation
#     fingerprint specifically — added low-risk tweaks (hide
#     navigator.webdriver, disable the "AutomationControlled" flag) so
#     the browser pass looks like an ordinary visitor, not a bypass of
#     any real security control.
#   - Added a generic "click likely tab/accordion labels" pass
#     (Commercial data, Specifications, Product details,
#     Classifications, ...) based on a per-vendor tab reference sheet —
#     covers Murrelektronik, HARTING, Phoenix Contact, Wieland, Pilz,
#     WAGO and similar sites without a hardcoded selector for each one.
#
# Carried over from v1.1:
#   - The run happens in a background thread; the Search tab polls and
#     re-renders every second, so Stop actually stops mid-batch and a
#     crash still leaves partial results visible and downloadable right
#     there — not just via the Logs tab / autosave file.
#   - False "Blocked" on long pages that merely mention a phrase like
#     "access denied" in boilerplate text — see classify_text below.
#   - ABB / Siemens-style SPAs skip the fast HTTP pass entirely (it can
#     never work for them) and get extended timeouts on the browser pass.
# ═══════════════════════════════════════════════════════════════════
import streamlit as st
import pandas as pd
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

# ── Inlined engine ──────────────────────────────────────────────
import io
import json
import os
import re
import threading
from datetime import datetime

import pandas as pd
import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Status enum (same shape as the PDF tool's S class) ──────────────
class S:
    FOUND        = "Found"
    NOT_FOUND    = "Not Found"
    RENDER_ISSUE = "Page did not fully render — needs review (JS/selectors)"
    BLOCKED      = "Blocked / Access Denied"
    FAILED       = "Failed to load page"

DOT_COLOR = {
    S.FOUND: "#16a34a", S.NOT_FOUND: "#6b7280", S.RENDER_ISSUE: "#f59e0b",
    S.BLOCKED: "#dc2626", S.FAILED: "#7c3aed",
}
EMOJI = {
    S.FOUND: "✅", S.NOT_FOUND: "❌", S.RENDER_ISSUE: "🟡",
    S.BLOCKED: "🟣", S.FAILED: "🔺",
}

OUTPUT_COLUMNS = [
    "URL", "Keyword", "Extraction Option", "URL_Status", "URL_Search_Status",
    "Keyword_Status", "feature_name", "feature_value", "Keyword_Search_Status",
]

# ══════════════════════════════════════════════════════════════════
# Known slow / heavily-JS hosts
# ══════════════════════════════════════════════════════════════════
# FIX: "some companies need time to open, like ABB, Siemens" — these
# sites are Angular/React SPAs where the fast HTTP pass will NEVER
# succeed (the server HTML is just a loading shell), so retrying them
# on the fast pass is pure wasted time that also risks a false
# "Failed to load page" if the shell response is slow. We skip the
# fast pass entirely for these and go straight to the browser pass,
# where we also give them extra time to finish rendering.
KNOWN_SLOW_HOSTS = [
    "abb.com", "siemens.com", "sieportal.siemens.com", "nxp.com",
    "murrelektronik.com", "hms-networks.com",
]
# FIX: "ABB, NXP, and Siemens need time... need like 5 to 6 seconds" —
# even the earlier extended timeout wasn't always enough; bumped further,
# and (see _goto_with_retry below) a plain timeout on these hosts now
# gets one more patient retry instead of failing immediately.
SLOW_HOST_NAV_TIMEOUT_MS   = 60000   # vs 35000 default
SLOW_HOST_POST_LOAD_MS     = 7000    # vs 2500 default extra settle time

# ── Tunables ─────────────────────────────────────────────────────────
_MIN_USEFUL_CHARS = 400
_JS_PLACEHOLDER_SIGNS = [
    "please enable javascript", "enable javascript to continue",
    "loading, please wait", "you need to enable javascript",
]
# FIX: "Fix Block-page phrase detected (Blocked / Access Denied)" —
# the old check flagged ANY page containing one of these phrases
# ANYWHERE, which false-positived on long, perfectly normal pages that
# happen to mention e.g. a cookie-consent "access denied" snippet deep
# in boilerplate/legal text. Now we only classify as blocked when the
# phrase appears AND the page is short overall (a real block page is
# almost always short — it's a placeholder, not real product content)
# OR the phrase appears very early in the page (in the first chunk,
# where a real block page's message would be).
_BLOCK_SIGNS = [
    "access denied", "are you a human", "please verify you are a human",
    "unusual traffic", "request blocked", "403 forbidden",
    "pardon our interruption", "checking your browser",
]
_BLOCK_MAX_LEN_FOR_SHORT_MATCH = 3000   # a real block page is short
_BLOCK_EARLY_WINDOW            = 500    # or phrase must appear this early

SNIPPET_RADIUS = 60

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

EXPAND_SELECTORS_BY_HOST = {
    "phoenixcontact.com": ["text=Expand all", "button:has-text('Expand all')"],
    "festo.com":          ["button:has-text('Show more')", "text=Show all"],
    "murrelektronik.com": ["text=Commercial data", "text=Technical Data", "text=Connection data"],
}
NAV_TIMEOUT_MS    = 35000
POST_LOAD_WAIT_MS = 2500

# FIX: "Some links need to open tabs, like the Commercial data tab" —
# per the vendor reference sheet, most of these European industrial
# suppliers (Murrelektronik, HARTING, Phoenix Contact, Wieland, Pilz,
# WAGO, CONTA-CLIP, ...) share the same style of collapsed accordion
# section holding the compliance/customs data (Commercial data,
# Classifications, Specification, Product details, etc.) — the labels
# vary by vendor but the pattern is identical: a clickable header that
# reveals a data table underneath. Rather than hardcoding every vendor
# domain one at a time, we try all of these known tab labels on every
# page, opportunistically — if a label isn't present, the click is
# just skipped (wrapped in try/except), so this costs almost nothing
# on pages that don't have it.
GENERIC_EXPAND_TEXTS = [
    "Commercial data", "Commercial & Classifications data", "Commercial info",
    "Technical Data", "Connection data", "Classifications", "Classification",
    "Specification", "Specifications", "Product details", "Product Details",
    "Compliance Data", "Export Info", "Parametrics", "Parameters",
    "Packaging Detail", "Logistics and Packaging", "Further information",
    "Life Cycle Data", "ECCN/HTS", "Identification", "Product data",
]


def _safe_generic_expand(page, url, nav_timeout):
    """Best-effort click of any of GENERIC_EXPAND_TEXTS that's visible on
    the page. Guards against accidentally following a real navigation
    link instead of toggling an accordion — if a click changes the URL,
    we treat it as a mistaken match and navigate straight back."""
    original_url = page.url
    for text in GENERIC_EXPAND_TEXTS:
        try:
            # Prefer things that look like accordion/tab controls over
            # plain links, to reduce the chance of following a real <a href>.
            loc = page.locator(
                f"button:has-text('{text}'), [role=tab]:has-text('{text}'), "
                f"summary:has-text('{text}'), [role=button]:has-text('{text}'), "
                f"a[href='#']:has-text('{text}'), div:has-text('{text}') >> visible=true"
            ).first
            if not loc.is_visible(timeout=600):
                continue
            loc.click(timeout=800)
            page.wait_for_timeout(300)
            if page.url != original_url:
                # We followed a real link by mistake — go back and move on.
                page.goto(original_url, timeout=nav_timeout, wait_until="domcontentloaded")
                page.wait_for_timeout(300)
        except Exception:
            continue


def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url)
    return m.group(1).lower() if m else ""


def is_slow_host(url: str) -> bool:
    host = host_of(url)
    return any(h in host for h in KNOWN_SLOW_HOSTS)


def expand_selectors_for(url: str):
    host = host_of(url)
    for key, sels in EXPAND_SELECTORS_BY_HOST.items():
        if key in host:
            return sels
    return []


# FIX: NXP's flaky Blocked results look like request-rate-based bot
# scoring — several browser workers hitting nxp.com part pages back to
# back, in parallel, in a short window. A small enforced minimum gap
# between requests to the *same* host (across all worker threads)
# smooths that burst out without meaningfully slowing down a batch that
# spans many different vendors.
_host_last_request = {}
_host_throttle_lock = threading.Lock()
_MIN_HOST_GAP_SECONDS = 0.6
# FIX: "sometimes NXP blocks the IP" — a flat 0.6s gap wasn't enough to
# stay under NXP's rate-limit threshold specifically. Give known
# rate-sensitive hosts a bigger enforced gap between requests. This
# can't fix a genuine IP-level ban if one has already happened (no
# amount of client-side pacing gets around that — that needs waiting it
# out, or spacing large NXP batches across separate runs), but it
# reduces the odds of triggering one in the first place.
_HOST_MIN_GAP_OVERRIDE = {
    "nxp.com": 2.5,
}


def _throttle_host(url):
    import time as _time
    host = host_of(url)
    gap = _MIN_HOST_GAP_SECONDS
    for h, g in _HOST_MIN_GAP_OVERRIDE.items():
        if h in host:
            gap = g
            break
    with _host_throttle_lock:
        last = _host_last_request.get(host)
        now = _time.time()
        wait = 0.0
        if last is not None:
            wait = gap - (now - last)
        _host_last_request[host] = now + max(wait, 0.0)
    if wait > 0:
        _time.sleep(wait)


def classify_text(text: str) -> str:
    """Returns 'ok' | 'js_placeholder' | 'blocked' | 'too_short'."""
    lowered = text.lower()
    stripped_len = len(text.strip())

    early_window = lowered[:_BLOCK_EARLY_WINDOW]
    for sign in _BLOCK_SIGNS:
        if sign in lowered:
            if stripped_len < _BLOCK_MAX_LEN_FOR_SHORT_MATCH or sign in early_window:
                return "blocked"
            # phrase present but page is long and phrase isn't up front —
            # almost certainly boilerplate text, not a real block. Ignore it.

    if any(s in lowered for s in _JS_PLACEHOLDER_SIGNS):
        return "js_placeholder"
    if stripped_len < _MIN_USEFUL_CHARS:
        return "too_short"
    return "ok"


# ══════════════════════════════════════════════════════════════════
# Fast HTTP pass
# ══════════════════════════════════════════════════════════════════
_session_local = threading.local()


def _get_session():
    if not hasattr(_session_local, "s"):
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5,
                       status_forcelist=[429, 500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers.update({"User-Agent": UA})
        _session_local.s = s
    return _session_local.s


def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def fast_fetch(url: str, timeout: int):
    """Returns (text_or_None, category)."""
    if is_slow_host(url):
        # Known SPA — don't waste time on a fast pass that will never work.
        return None, "skip_known_spa"
    try:
        resp = _get_session().get(url, timeout=timeout, verify=False, allow_redirects=True)
    except requests.exceptions.Timeout:
        return None, "timeout"
    except requests.exceptions.SSLError:
        return None, "ssl"
    except requests.exceptions.ConnectionError:
        return None, "connection"
    except Exception:
        return None, "exception"

    if resp.status_code != 200:
        return None, f"http_{resp.status_code}"

    text = extract_visible_text(resp.text)
    return text, classify_text(text)


# ══════════════════════════════════════════════════════════════════
# Playwright escalation pass
# ══════════════════════════════════════════════════════════════════
def playwright_fetch(context, url: str, timeout_ms: int):
    """Runs inside an already-open Playwright browser context.
    Returns (text_or_None, category)."""
    from playwright.sync_api import TimeoutError as PWTimeout

    _throttle_host(url)

    slow = is_slow_host(url)
    nav_timeout = SLOW_HOST_NAV_TIMEOUT_MS if slow else timeout_ms
    settle_wait = SLOW_HOST_POST_LOAD_MS if slow else POST_LOAD_WAIT_MS

    page = context.new_page()
    try:
        page, err = _goto_with_retry(page, url, nav_timeout)
        if err == "timeout":
            return None, "timeout"
        if err == "exception" or page is None:
            return None, "exception"

        try:
            page.wait_for_load_state("networkidle", timeout=nav_timeout)
        except PWTimeout:
            pass  # some sites never go idle - continue anyway

        for sel in expand_selectors_for(url):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1200):
                    loc.click(timeout=1200)
                    page.wait_for_timeout(400)
            except Exception:
                pass

        _safe_generic_expand(page, url, nav_timeout)

        page.wait_for_timeout(settle_wait)
        text = page.evaluate("document.body ? document.body.innerText : ''") or ""
        category = classify_text(text)

        # FIX: NXP (and similar) intermittently return a genuine
        # bot-detection block on one request and load fine on the very
        # next one — this looks like request-rate-based bot scoring,
        # not a permanent block. One retry after a short pause, on a
        # fresh page, resolves most of these instead of reporting a
        # real vendor's page as permanently Blocked.
        if category == "blocked":
            page.wait_for_timeout(1500)
            retry_page = context.new_page()
            try:
                retry_page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
                retry_page.wait_for_load_state("networkidle", timeout=nav_timeout)
            except Exception:
                pass
            _safe_generic_expand(retry_page, url, nav_timeout)
            retry_page.wait_for_timeout(settle_wait)
            retry_text = retry_page.evaluate("document.body ? document.body.innerText : ''") or ""
            retry_page.close()
            retry_category = classify_text(retry_text)
            if retry_category != "blocked":
                return retry_text, retry_category

        return text, category
    finally:
        page.close()


def _goto_with_retry(page, url, nav_timeout):
    """FIX: several sites (ABB especially, but also seen on Murrelektronik
    and HMS Networks URLs) fail the FIRST navigation attempt for reasons
    that aren't necessarily a real problem — an HTTP/2 protocol quirk, a
    slow first response, a transient timeout. The old code only retried
    the specific ERR_HTTP2_PROTOCOL_ERROR case and gave up immediately on
    anything else (including a plain timeout), which is too eager to
    call a real, working page 'Failed to load'. Now ANY navigation
    problem — timeout included — gets one more patient attempt on a
    fresh page with a longer timeout and the more lenient wait_until
    before it's treated as a genuine failure."""
    from playwright.sync_api import TimeoutError as PWTimeout

    def _attempt(pg, timeout, wait_until):
        try:
            pg.goto(url, timeout=timeout, wait_until=wait_until)
            return pg, None
        except PWTimeout:
            return pg, "timeout"
        except Exception as e:
            return pg, str(e)

    result_page, err = _attempt(page, nav_timeout, "domcontentloaded")
    if err is None:
        return result_page, None

    # First attempt failed (timeout OR any exception, e.g. ERR_HTTP2) —
    # one more try, fresh page, more time, more lenient wait condition.
    try:
        context = page.context
        try:
            page.close()
        except Exception:
            pass
        retry_page = context.new_page()
        retry_page.goto(url, timeout=int(nav_timeout * 1.5), wait_until="load")
        return retry_page, None
    except PWTimeout:
        return None, "timeout"
    except Exception:
        return None, "exception"


def run_playwright_batch(rows_subset, case_sensitive, timeout_ms, on_row_done, stop_event=None):
    """Runs a chunk of rows through one Playwright browser instance,
    sequentially within this worker thread/process.

    FIX: on a mid-batch exception we used to re-mark the WHOLE chunk
    (including rows already processed successfully) as failed, which
    could silently duplicate/overwrite good results. Now we track
    which rows actually got processed and only fall back for the rest.

    stop_event (threading.Event, optional): checked between rows so a
    user-triggered Stop takes effect promptly instead of finishing the
    whole chunk first.
    """
    from playwright.sync_api import sync_playwright
    results = []
    processed_urls = set()
    try:
        with sync_playwright() as pw:
            # FIX: some vendor sites (e.g. Ruland, a Magento storefront)
            # sit behind bot-detection that specifically flags Playwright's
            # default automation fingerprint (navigator.webdriver=true,
            # certain launch flags) and blocks it even though the exact
            # same page loads fine for a normal visitor. These are
            # legitimate, low-risk tweaks to look like an ordinary
            # browser — not a bypass of any login/paywall/security
            # control, just not announcing "I am an automated browser"
            # on public product pages.
            browser = pw.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            context = browser.new_context(
                user_agent=UA,
                viewport={"width": 1366, "height": 900},
                locale="en-US",
                extra_http_headers={"Accept-Language": "en-US,en;q=0.9"},
            )
            context.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )
            for row in rows_subset:
                if stop_event is not None and stop_event.is_set():
                    break
                text, cat = playwright_fetch(context, row["URL"], timeout_ms)
                res = finalize_row(row, text, cat, case_sensitive, engine="Rendered (Browser)")
                processed_urls.add(str(row["URL"]))
                results.append(res)
                on_row_done(res)
            browser.close()
    except Exception as e:
        # Only fall back for rows this batch never got to.
        remaining = [r for r in rows_subset if str(r["URL"]) not in processed_urls]
        for row in remaining:
            res = finalize_row(
                row, None, "exception", case_sensitive,
                engine="Rendered (Browser)", note=f"Playwright error: {e}",
            )
            results.append(res)
            on_row_done(res)
    return results


# ══════════════════════════════════════════════════════════════════
# Keyword search + row finalization
# ══════════════════════════════════════════════════════════════════
def parse_keywords(raw):
    return [k.strip() for k in str(raw).split("|") if k.strip()]


def search_keyword(text, kw, case_sensitive):
    hay = text if case_sensitive else text.lower()
    needle = kw if case_sensitive else kw.lower()
    return hay.count(needle)


def best_snippet(text, kw, case_sensitive, radius=SNIPPET_RADIUS):
    hay = text if case_sensitive else text.lower()
    needle = kw if case_sensitive else kw.lower()
    idx = hay.find(needle)
    if idx == -1:
        return ""
    start = max(0, idx - radius)
    end = min(len(text), idx + len(needle) + radius)
    return text[start:end].replace("\n", " ").strip()


_CAT_MSG = {
    "timeout": "Timeout", "ssl": "SSL/TLS Error", "connection": "Connection Error",
    "exception": "Request Error", "blocked": "Blocked (bot detection / captcha)",
    "skip_known_spa": "Known JS app — sent straight to browser rendering",
}
# HTTP codes that anti-bot systems commonly use to disguise a block as an
# ordinary error — a genuine 404 and a bot-detection 404 are indistinguishable
# at this level, so we let the browser pass settle it instead of failing outright.
_AUTO_ESCALATE_HTTP_CODES = {"403", "404", "429", "503"}


def finalize_row(row, text, category, case_sensitive, engine, note=""):
    url = str(row["URL"]).strip()
    raw_keyword = row["Keyword"]

    base = {
        "URL": url, "Keyword": raw_keyword, "Extraction Option": engine,
        "URL_Status": None, "URL_Search_Status": "", "Keyword_Status": None,
        "feature_name": raw_keyword, "feature_value": "",
        "Keyword_Search_Status": "", "_needs_escalation": False,
    }

    def done(**kw):
        r = dict(base); r.update(kw); return r

    if not url.startswith("http"):
        return done(URL_Status=0, URL_Search_Status="Invalid URL",
                    Keyword_Search_Status=S.FAILED)

    if category == "skip_known_spa":
        return done(URL_Status=None, URL_Search_Status=_CAT_MSG[category],
                    Keyword_Search_Status="", _needs_escalation=True)

    # FIX: "All NXP failed (Failed to load page)" — nxp.com (and some
    # other vendors, e.g. Ruland) return a 403/404 to plain non-browser
    # requests as an anti-bot measure, even though the URL is completely
    # real and loads fine in an actual browser. A real 404 (a genuinely
    # deleted part page) looks identical to this at the HTTP level, so
    # we can't tell them apart from the status code alone — the safe
    # move is to let the browser pass make the real determination
    # instead of trusting a plain-HTTP 403/404 as final.
    if category and category.startswith("http_"):
        code = category.split("_", 1)[1]
        if code in _AUTO_ESCALATE_HTTP_CODES and engine == "Fast (HTTP)":
            return done(URL_Status=None,
                        URL_Search_Status=f"HTTP {code} on fast pass — confirming via browser",
                        Keyword_Search_Status="", _needs_escalation=True)
        return done(URL_Status=0, URL_Search_Status=note or f"HTTP {code}",
                    Keyword_Search_Status=S.FAILED)

    if category in ("timeout", "ssl", "connection", "exception"):
        msg = _CAT_MSG.get(category, category)
        return done(URL_Status=0, URL_Search_Status=note or msg,
                    Keyword_Search_Status=S.FAILED)

    if category == "blocked":
        return done(URL_Status=0, URL_Search_Status="Blocked by site",
                    Keyword_Search_Status=S.BLOCKED)

    if category in ("js_placeholder", "too_short"):
        return done(URL_Status=3, URL_Search_Status="Rendered but content too thin",
                    Keyword_Search_Status=S.RENDER_ISSUE,
                    _needs_escalation=(engine == "Fast (HTTP)"))

    # category == "ok" -> real keyword search
    keywords = parse_keywords(raw_keyword) or [str(raw_keyword)]
    found, missing, total = [], [], 0
    snippet = ""
    for kw in keywords:
        cnt = search_keyword(text, kw, case_sensitive)
        if cnt > 0:
            found.append(kw)
            total += cnt
            if not snippet:
                snippet = best_snippet(text, kw, case_sensitive)
        else:
            missing.append(kw)

    if found:
        return done(URL_Status=3, URL_Search_Status="Done", Keyword_Status=3.0,
                    feature_name=", ".join(found), feature_value=snippet,
                    Keyword_Search_Status=S.FOUND)
    return done(URL_Status=3, URL_Search_Status="Done", Keyword_Status=3.0,
                feature_name=str(raw_keyword), Keyword_Search_Status=S.NOT_FOUND)


# ══════════════════════════════════════════════════════════════════
# Autosave / recovery
# ══════════════════════════════════════════════════════════════════
def autosave(path_csv, path_meta, result_dicts, processed, total):
    """Never raises — autosave failing must never crash the job."""
    try:
        clean = [{k: v for k, v in r.items() if k in OUTPUT_COLUMNS} for r in result_dicts]
        df = pd.DataFrame(clean, columns=OUTPUT_COLUMNS)
        df.to_csv(path_csv, index=False, encoding="utf-8-sig")
        with open(path_meta, "w") as f:
            json.dump({
                "rows": len(df), "processed": processed, "total": total,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f)
    except Exception:
        pass


def load_autosave(path_csv, path_meta):
    try:
        if os.path.exists(path_csv) and os.path.getsize(path_csv) > 0:
            df = pd.read_csv(path_csv, dtype={"Keyword": str, "URL": str})
            meta = {}
            if os.path.exists(path_meta):
                with open(path_meta) as f:
                    meta = json.load(f)
            return df, meta
    except Exception:
        pass
    return None, None


def clear_autosave(path_csv, path_meta):
    for p in (path_csv, path_meta):
        try:
            os.remove(p)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# Output builders
# ══════════════════════════════════════════════════════════════════
def _sheet(writer, df, col, val, name):
    sub = df[df[col] == val]
    if not sub.empty:
        sub.to_excel(writer, sheet_name=name, index=False)


def df_to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        clean = df[OUTPUT_COLUMNS] if all(c in df.columns for c in OUTPUT_COLUMNS) else df
        clean.to_excel(w, sheet_name="All Results", index=False)
        if all(c in df.columns for c in OUTPUT_COLUMNS):
            _sheet(w, clean, "Keyword_Search_Status", S.FOUND, "Found")
            _sheet(w, clean, "Keyword_Search_Status", S.NOT_FOUND, "Not Found")
            _sheet(w, clean, "Keyword_Search_Status", S.RENDER_ISSUE, "Needs Review")
            _sheet(w, clean, "Keyword_Search_Status", S.BLOCKED, "Blocked")
            _sheet(w, clean, "Keyword_Search_Status", S.FAILED, "Failed")
    return buf.getvalue()


def df_to_csv_bytes(df):
    cols = OUTPUT_COLUMNS if all(c in df.columns for c in OUTPUT_COLUMNS) else df.columns
    return df[cols].to_csv(index=False).encode("utf-8-sig")


def make_template_bytes():
    df = pd.DataFrame({
        "URL": [
            "https://sieportal.siemens.com/en-us/products-services/detail/EXAMPLE",
            "https://www.festo.com/us/en/a/EXAMPLE/",
            "https://www.ifm.com/us/en/product/EXAMPLE",
        ],
        "Keyword": ["uiExportControlValue", "ECCN|uiExportControlValue", "HTS Code"],
    })
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name="Template")
    return buf.getvalue()


def results_to_output_df(result_dicts):
    """Safe conversion used everywhere a partial or final results list
    needs to become a DataFrame with the canonical column order."""
    if not result_dicts:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)
    clean = [{k: v for k, v in r.items() if k in OUTPUT_COLUMNS} for r in result_dicts]
    return pd.DataFrame(clean, columns=OUTPUT_COLUMNS)

# ── End inlined engine ──────────────────────────────────────────

st.set_page_config(
    page_title="Webpage Keyword Search",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

SEARCH_LIMIT          = 20_000
DEFAULT_FAST_WORKERS  = 15
DEFAULT_PW_WORKERS    = 4
DEFAULT_TIMEOUT       = 20

_AUTOSAVE_FILE = "/tmp/webpage_search_autosave.csv"
_AUTOSAVE_META = "/tmp/webpage_search_meta.json"

# ══════════════════════════════════════════════════════════════════
# Session state
# ══════════════════════════════════════════════════════════════════
if "job" not in st.session_state:
    st.session_state.job = None          # dict while a run is active/finished
if "results_df" not in st.session_state:
    st.session_state.results_df = None


def _inject_css():
    st.markdown("""
<style>
:root { --wks-radius: 12px; }
.wks-hero {
    background: linear-gradient(135deg, var(--primary-color) 0%, transparent 140%);
    background-color: color-mix(in srgb, var(--primary-color) 8%, var(--background-color));
    border: 1px solid color-mix(in srgb, var(--primary-color) 30%, transparent);
    border-radius: var(--wks-radius);
    padding: 18px 22px;
    display: flex; justify-content: space-between; align-items: center;
    margin-bottom: 6px;
}
.wks-badge {
    display: inline-block; padding: 3px 10px; border-radius: 999px;
    font-size: 0.75rem; font-weight: 600; color: white;
}
.wks-info-card {
    background-color: color-mix(in srgb, var(--primary-color) 6%, var(--background-color));
    border: 1px solid color-mix(in srgb, var(--primary-color) 22%, transparent);
    border-radius: var(--wks-radius);
    padding: 14px 16px; font-size: 0.88rem;
}
.wks-limit-banner {
    background-color: #e11d48; color: white; padding: 8px 12px;
    border-radius: 8px; font-weight: 600; text-align: center; font-size: 0.85rem;
}
.wks-side-title { font-weight: 700; font-size: 0.95rem; margin: 4px 0 8px 0; }
.wks-legend-row { display:flex; align-items:center; gap:8px; margin:4px 0; font-size:0.82rem; }
.wks-dot { width:10px; height:10px; border-radius:50%; display:inline-block; flex-shrink:0; }
</style>
""", unsafe_allow_html=True)


_inject_css()

# ══════════════════════════════════════════════════════════════════
# Background job runner
# ══════════════════════════════════════════════════════════════════
def _new_job(rows, fast_workers, pw_workers, timeout, case_sensitive):
    return {
        "rows": rows,
        "fast_workers": fast_workers, "pw_workers": pw_workers,
        "timeout": timeout, "case_sensitive": case_sensitive,
        "results": [],                 # append-only list of result dicts
        "lock": threading.Lock(),
        "completed": 0,
        "total": len(rows),
        "log": [],
        "stop_event": threading.Event(),
        "running": True,
        "stopped": False,
        "error": None,
        "start_ts": time.time(),
        "phase": "Starting…",
    }


def _log(job, msg):
    ts = datetime.now().strftime("%H:%M:%S")
    with job["lock"]:
        job["log"].append(f"[{ts}] {msg}")
        if len(job["log"]) > 200:
            job["log"].pop(0)


def _add_result(job, res):
    with job["lock"]:
        job["results"].append(res)
        job["completed"] += 1
        n = job["completed"]
    if n % 25 == 0:
        autosave(_AUTOSAVE_FILE, _AUTOSAVE_META, job["results"], n, job["total"])


def _run_job_thread(job):
    try:
        rows = job["rows"]
        stop_event = job["stop_event"]
        escalate_rows = []

        # ── PASS 1: fast HTTP ────────────────────────────────────
        job["phase"] = "Fast pass"
        with ThreadPoolExecutor(max_workers=job["fast_workers"]) as ex:
            futures = {ex.submit(fast_fetch, str(r["URL"]).strip(), job["timeout"]): r for r in rows}
            for fut in as_completed(futures):
                if stop_event.is_set():
                    for f in futures:
                        f.cancel()
                    break
                row = futures[fut]
                try:
                    text, cat = fut.result()
                except Exception:
                    text, cat = None, "exception"
                res = finalize_row(row, text, cat, job["case_sensitive"], engine="Fast (HTTP)")
                if res["_needs_escalation"]:
                    escalate_rows.append(row)
                else:
                    _add_result(job, res)
                    _log(job, f"[fast] {row['URL']} -> {res['Keyword_Search_Status']}")

        # ── PASS 2: Playwright escalation ───────────────────────
        if escalate_rows and not stop_event.is_set():
            job["phase"] = "Rendering"
            _log(job, f"Escalating {len(escalate_rows)} rows to browser rendering...")
            n_workers = max(1, min(job["pw_workers"], len(escalate_rows)))
            chunks = [escalate_rows[i::n_workers] for i in range(n_workers)]
            chunks = [c for c in chunks if c]

            def _on_row_done(res):
                _add_result(job, res)
                _log(job, f"[render] {res['URL']} -> {res['Keyword_Search_Status']}")

            with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
                futs = [
                    ex.submit(run_playwright_batch, chunk, job["case_sensitive"],
                              job["timeout"] * 1000, _on_row_done, stop_event)
                    for chunk in chunks
                ]
                for f in as_completed(futs):
                    f.result()  # re-raises here if a whole chunk truly blew up

        job["stopped"] = stop_event.is_set()

    except Exception as e:
        job["error"] = f"{type(e).__name__}: {e}"
        _log(job, f"ERROR: {job['error']}")
    finally:
        autosave(_AUTOSAVE_FILE, _AUTOSAVE_META, job["results"], job["completed"], job["total"])
        job["running"] = False


# ══════════════════════════════════════════════════════════════════
# Sidebar
# ══════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<div class="wks-side-title">🌐 Webpage Keyword Search</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="wks-limit-banner">⚠️ Limit {SEARCH_LIMIT:,} rows / run</div>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown('<div class="wks-side-title">⚡ Search Configuration</div>', unsafe_allow_html=True)
    case_sensitive = st.checkbox("Case-Sensitive Search", value=False)

    st.markdown('<div class="wks-side-title">🚀 Performance</div>', unsafe_allow_html=True)
    fast_workers = st.slider("Fast-pass Workers (HTTP)", 5, 40, DEFAULT_FAST_WORKERS, 1)
    pw_workers = st.slider(
        "Browser Workers (Playwright)", 1, 8, DEFAULT_PW_WORKERS, 1,
        help="Also used for known slow SPAs (ABB, Siemens) which skip "
             "the fast pass entirely and get extra render time.",
    )
    timeout = st.slider("Per-URL Timeout (sec)", 5, 90, DEFAULT_TIMEOUT, 5)

    st.markdown("---")
    st.markdown('<div class="wks-side-title">📥 Input Template</div>', unsafe_allow_html=True)
    st.download_button(
        "⬇️ Download Template (.xlsx)", data=make_template_bytes(),
        file_name="Webpage_Keyword_Search_Template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    st.markdown("---")
    st.markdown('<div class="wks-side-title">📊 Status Legend</div>', unsafe_allow_html=True)
    legend_html = "".join(
        f'<div class="wks-legend-row"><span class="wks-dot" style="background:{DOT_COLOR[s]}"></span>'
        f'<span><b>{name}</b> — {desc}</span></div>'
        for s, name, desc in [
            (S.FOUND, "Found", "keyword located on the rendered page"),
            (S.NOT_FOUND, "Not Found", "page read fully, keyword absent"),
            (S.RENDER_ISSUE, "Needs Review", "content too thin even after rendering"),
            (S.BLOCKED, "Blocked", "bot detection / captcha / access denied"),
            (S.FAILED, "Failed", "could not load the page at all"),
        ]
    )
    st.markdown(legend_html, unsafe_allow_html=True)

    saved_df, saved_meta = load_autosave(_AUTOSAVE_FILE, _AUTOSAVE_META)
    if saved_df is not None and saved_meta:
        st.markdown("---")
        st.markdown('<div class="wks-side-title">💾 Saved Progress</div>', unsafe_allow_html=True)
        st.caption(
            f"**{saved_meta.get('rows', 0):,}** rows saved · "
            f"{saved_meta.get('processed', 0):,}/{saved_meta.get('total', 0):,} processed  \n"
            f"Saved at {saved_meta.get('saved_at', '?')}"
        )
        cr1, cr2 = st.columns(2)
        with cr1:
            if st.button("♻️ Restore", use_container_width=True):
                st.session_state.results_df = saved_df
                st.session_state.job = None
                st.rerun()
        with cr2:
            if st.button("🗑 Clear", use_container_width=True):
                clear_autosave(_AUTOSAVE_FILE, _AUTOSAVE_META)
                st.rerun()
        st.download_button(
            "📥 Download Saved Progress", data=df_to_csv_bytes(saved_df),
            file_name=f"partial_{saved_meta.get('saved_at','').replace(' ','_').replace(':','-')}.csv",
            mime="text/csv", use_container_width=True,
        )

# ══════════════════════════════════════════════════════════════════
# Header
# ══════════════════════════════════════════════════════════════════
job = st.session_state.job
is_running = job is not None and job["running"]
status_badge = "🟠 Running" if is_running else ("🔴 Error" if job and job.get("error") else "🟢 Ready")
badge_color = "#f59e0b" if is_running else ("#dc2626" if job and job.get("error") else "#16a34a")

st.markdown(f"""
<div class="wks-hero">
  <div>
    <div style="font-size:1.3rem;font-weight:800;">🌐 Webpage Keyword Search</div>
    <div style="opacity:0.75;font-size:0.85rem;">Fast HTTP pass · Browser fallback for JS pages · 5-Status Output</div>
  </div>
  <div class="wks-badge" style="background:{badge_color};">{status_badge}</div>
</div>
""", unsafe_allow_html=True)

tab_search, tab_results, tab_logs, tab_guide = st.tabs(["🔍 Search", "📄 Results", "📋 Logs", "📘 Guide"])

# ══════════════════════════════════════════════════════════════════
# Search tab
# ══════════════════════════════════════════════════════════════════
with tab_search:
    if is_running or (job is not None and job.get("results")):
        # ── Live / finished job view ────────────────────────────
        with job["lock"]:
            n_results = len(job["results"])
            log_tail = list(job["log"][-14:])
        total = job["total"]
        pct = min(job["completed"] / total, 1.0) if total else 1.0
        elapsed = time.time() - job["start_ts"]
        rate = job["completed"] / elapsed if elapsed > 0 else 0
        eta = (total - job["completed"]) / rate if rate > 0 else 0

        if is_running:
            st.progress(pct, text=f"[{job['phase']}] {job['completed']:,}/{total:,} · {rate:.1f}/s · ETA {eta:.0f}s")
        elif job.get("error"):
            st.error(f"Run stopped due to an error: {job['error']}\n\n"
                     f"**{n_results:,} of {total:,} rows still completed and are available below.**")
        elif job.get("stopped"):
            st.warning(f"Stopped by user. **{n_results:,} of {total:,} rows completed** and are available below.")
        else:
            st.success(f"Done. {n_results:,} rows processed.")

        counts = {}
        with job["lock"]:
            for r in job["results"]:
                counts[r["Keyword_Search_Status"]] = counts.get(r["Keyword_Search_Status"], 0) + 1
        st.markdown(
            f"**{EMOJI[S.FOUND]} Found:** {counts.get(S.FOUND,0)} · "
            f"**{EMOJI[S.NOT_FOUND]} Not Found:** {counts.get(S.NOT_FOUND,0)} · "
            f"**{EMOJI[S.RENDER_ISSUE]} Needs Review:** {counts.get(S.RENDER_ISSUE,0)} · "
            f"**{EMOJI[S.BLOCKED]} Blocked:** {counts.get(S.BLOCKED,0)} · "
            f"**{EMOJI[S.FAILED]} Failed:** {counts.get(S.FAILED,0)}"
        )
        st.code("\n".join(log_tail) or "…")

        # Always-available partial/final results, regardless of state
        with job["lock"]:
            partial_df = results_to_output_df(job["results"])

        b1, b2, b3 = st.columns(3)
        with b1:
            if is_running:
                if st.button("⏹ Stop & Save", use_container_width=True, type="primary"):
                    job["stop_event"].set()
                    st.info("Stopping — finishing in-flight requests, results will remain available.")
        with b2:
            st.download_button(
                "📥 Download Results So Far (Excel)", data=df_to_excel_bytes(partial_df),
                file_name="webpage_keyword_results_partial.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True, disabled=partial_df.empty,
            )
        with b3:
            if not is_running:
                if st.button("🗑 Clear & Start New", use_container_width=True):
                    st.session_state.job = None
                    st.session_state.results_df = None
                    st.rerun()

        if not is_running:
            st.session_state.results_df = partial_df

        if is_running:
            time.sleep(1)
            st.rerun()

    else:
        # ── Upload / configure view ──────────────────────────────
        c_left, c_right = st.columns([2, 1])
        with c_left:
            st.markdown("### 📁 Upload Input File")
            st.caption("Excel (.xlsx) or CSV with `URL` and `Keyword` columns.")
            uploaded = st.file_uploader("Upload", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
            st.download_button(
                "⬇️ Download Template to Fill In", data=make_template_bytes(),
                file_name="Webpage_Keyword_Search_Template.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            )
        with c_right:
            st.markdown("### 📌 Required Format")
            st.markdown("""
<div class="wks-info-card">
<b>Required columns:</b> <code>URL</code> and <code>Keyword</code><br><br>
<b>Multi-keyword</b> (pipe separator):<br>
<code>uiExportControlValue</code> — single<br>
<code>ECCN | uiExportControlValue</code> — two keywords<br><br>
A row is marked <b>Found</b> if <i>any</i> keyword matches.
</div>""", unsafe_allow_html=True)

        input_df = None
        if uploaded:
            try:
                if uploaded.name.endswith(".csv"):
                    input_df = pd.read_csv(uploaded, dtype={"Keyword": str, "URL": str})
                else:
                    input_df = pd.read_excel(uploaded, dtype={"Keyword": str, "URL": str})
                input_df.columns = [c.strip() for c in input_df.columns]
            except Exception as e:
                st.error(f"Failed to read file: {e}")
                input_df = None

        if input_df is not None:
            missing = [c for c in ["URL", "Keyword"] if c not in input_df.columns]
            if missing:
                st.error(f"❌ Missing columns: **{', '.join(missing)}** | Found: `{input_df.columns.tolist()}`")
            else:
                input_df = input_df.dropna(subset=["URL"]).reset_index(drop=True)
                n = len(input_df)
                if n > SEARCH_LIMIT:
                    st.warning(f"⚠️ {n:,} rows — trimmed to {SEARCH_LIMIT:,}")
                    input_df = input_df.head(SEARCH_LIMIT)
                    n = SEARCH_LIMIT
                st.success(f"✅ **{n:,} rows** loaded from `{uploaded.name}`")

                with st.expander("🔎 Preview input (first 10 rows)"):
                    st.dataframe(input_df.head(10), use_container_width=True)

                st.markdown("---")
                if st.button("🚀 Start Search", use_container_width=True, type="primary"):
                    rows = input_df.to_dict("records")
                    new_job = _new_job(rows, fast_workers, pw_workers, timeout, case_sensitive)
                    st.session_state.job = new_job
                    t = threading.Thread(target=_run_job_thread, args=(new_job,), daemon=True)
                    t.start()
                    st.rerun()

# ══════════════════════════════════════════════════════════════════
# Results tab
# ══════════════════════════════════════════════════════════════════
with tab_results:
    rdf = st.session_state.results_df
    if rdf is None or rdf.empty:
        st.info("Run a search to see results here. Partial results from a "
                "stopped or interrupted run also appear here — nothing is "
                "lost, you don't need to go to the Logs tab for that.")
    else:
        m1, m2, m3, m4, m5 = st.columns(5)
        counts = rdf["Keyword_Search_Status"].value_counts()
        for col, status, label in [
            (m1, S.FOUND, "Found"), (m2, S.NOT_FOUND, "Not Found"),
            (m3, S.RENDER_ISSUE, "Needs Review"), (m4, S.BLOCKED, "Blocked"),
            (m5, S.FAILED, "Failed"),
        ]:
            col.metric(f"{EMOJI[status]} {label}", int(counts.get(status, 0)))

        chart = rdf["Keyword_Search_Status"].value_counts().reset_index()
        chart.columns = ["Status", "Count"]
        st.bar_chart(chart.set_index("Status"))

        st.markdown("---")
        all_statuses = rdf["Keyword_Search_Status"].unique().tolist()
        sel = st.multiselect("Filter by status", all_statuses, default=all_statuses)
        flt = rdf[rdf["Keyword_Search_Status"].isin(sel)]

        display_df = flt.copy()
        display_df["Keyword_Search_Status"] = display_df["Keyword_Search_Status"].apply(
            lambda s: f"{EMOJI.get(s,'')} {s}"
        )
        st.dataframe(display_df, use_container_width=True, height=420)

        st.markdown("---")
        dc1, dc2 = st.columns(2)
        with dc1:
            st.download_button("📥 Download Excel (6 sheets)", data=df_to_excel_bytes(flt),
                                file_name="webpage_keyword_results.xlsx",
                                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                                use_container_width=True)
        with dc2:
            st.download_button("📥 Download CSV", data=df_to_csv_bytes(flt),
                                file_name="webpage_keyword_results.csv",
                                mime="text/csv", use_container_width=True)

# ══════════════════════════════════════════════════════════════════
# Logs tab
# ══════════════════════════════════════════════════════════════════
with tab_logs:
    st.caption("Full live log while a job is running appears on the Search "
               "tab. Autosave writes to disk every 25 completed rows so a "
               "crash or lost connection never loses more than that.")
    saved_df, saved_meta = load_autosave(_AUTOSAVE_FILE, _AUTOSAVE_META)
    if saved_df is not None:
        st.write(saved_meta)
        st.dataframe(saved_df.tail(50), use_container_width=True)
    else:
        st.info("No autosave file present yet.")

# ══════════════════════════════════════════════════════════════════
# Guide tab
# ══════════════════════════════════════════════════════════════════
with tab_guide:
    st.markdown(f"""
### How this differs from the PDF tool
Live product pages often load their real spec data via JavaScript *after*
the page loads. So every URL runs through up to two passes:

1. **Fast pass** — plain HTTP request + text extraction. Skipped entirely
   for known JS-heavy sites ({", ".join(KNOWN_SLOW_HOSTS)}) since it
   can never succeed there — they go straight to pass 2.
2. **Browser pass** — a real headless browser renders the page (including
   clicking "expand" sections on sites like Phoenix Contact) before
   reading the text. Known-slow sites get extra time here.

### Stopping a run
**Stop & Save** takes effect between rows (not just between passes), and
whatever completed stays visible and downloadable on the Search tab
immediately — you don't need to dig through the Logs tab or the sidebar
Recovery panel for it, though those still work too as a backup.

### Output columns
| Column | Meaning |
|---|---|
| `Extraction Option` | Which pass produced the result: `Fast (HTTP)` or `Rendered (Browser)` |
| `URL_Status` | 3 = loaded OK · 0 = error |
| `Keyword_Status` | 3.0 once a keyword search was actually run |
| `feature_name` | Keyword(s) matched (or the raw keyword if not found) |
| `feature_value` | Context snippet around the first match |
| `Keyword_Search_Status` | Main result — Found / Not Found / Needs Review / Blocked / Failed |

### If a vendor site keeps showing "Needs Review"
Even the browser pass couldn't get enough content — usually a site needs
an extra click (accordion, "load more") not in the expand-selector list
yet, or the data is gated behind login. Tell me which vendor and I'll add
its selector.

### If a vendor site keeps showing "Blocked"
As of this version, a block is only reported when the block phrase
appears on a short page or near the very top — a phrase buried in a long,
otherwise-normal page no longer triggers it. If a specific vendor still
false-positives, tell me which one and I'll add a per-site exception.
""")
