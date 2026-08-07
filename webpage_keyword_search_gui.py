"""
Webpage Keyword Search - Desktop GUI  (v1.2, single self-contained file)
=========================================================================
Everything needed to run this tool is in this one file — no other
project file is required. Output schema matches the PDF tool exactly:
same 9 columns as the Streamlit app.

CHANGES IN v1.2:
    - NXP: added to the known-slow-host list, so it goes straight to
      browser rendering instead of getting a fake 404 from the fast pass.
    - Any 403/404/429/503 on the fast pass now gets one confirming pass
      through the browser instead of being marked Failed immediately —
      several sites (NXP, Ruland, ...) return these codes to non-browser
      requests as an anti-bot measure even though the page is real.
    - A "Blocked" result now gets one retry on a fresh page after a
      short pause before being treated as final — NXP in particular was
      flaky (same URL blocked once, fine the next), which looks like
      request-rate-based bot scoring rather than a hard block.
    - A small per-host minimum gap between browser requests smooths out
      the request bursts that seemed to trigger that scoring.
    - Ruland (and similar sites) specifically block Playwright's default
      automation fingerprint — added low-risk tweaks (hides
      navigator.webdriver, disables the "AutomationControlled" flag) so
      the browser pass looks like an ordinary visitor.
    - Added a generic "click likely tab/accordion labels" pass (Commercial
      data, Specifications, Product details, Classifications, ...) based
      on the per-vendor tab reference sheet, in addition to the existing
      per-host selectors — covers Murrelektronik, HARTING, Phoenix
      Contact, Wieland, Pilz, WAGO, and similar sites without needing a
      hardcoded selector for every single one.

SETUP:
    pip install -r webpage_requirements.txt
    playwright install chromium

RUN AS A SCRIPT:
    python webpage_keyword_search_gui.py

BUILD AS A WINDOWS .EXE:
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name WebpageKeywordSearch webpage_keyword_search_gui.py
    -> dist/WebpageKeywordSearch.exe
    (No --add-data needed anymore — this file has no sibling module to bundle.)
    Playwright's browser binary still needs `playwright install chromium`
    run once on whichever machine actually runs the exe.
"""

import os
import tempfile
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

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
]
SLOW_HOST_NAV_TIMEOUT_MS   = 45000   # vs 30000 default
SLOW_HOST_POST_LOAD_MS     = 5000    # vs 2000 default extra settle time

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
NAV_TIMEOUT_MS    = 30000
POST_LOAD_WAIT_MS = 2000

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


def _throttle_host(url):
    import time as _time
    host = host_of(url)
    with _host_throttle_lock:
        last = _host_last_request.get(host)
        now = _time.time()
        wait = 0.0
        if last is not None:
            wait = _MIN_HOST_GAP_SECONDS - (now - last)
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
    """FIX: ABB (new.abb.com) intermittently throws
    net::ERR_HTTP2_PROTOCOL_ERROR on the first navigation attempt — not a
    timeout, so the old code gave up immediately and logged 'Failed to
    load page' even though the site itself was fine. This is a known
    Chromium/HTTP2 quirk on some sites. One retry with a fresh page
    almost always succeeds, so we do that before giving up for real."""
    from playwright.sync_api import TimeoutError as PWTimeout
    try:
        page.goto(url, timeout=nav_timeout, wait_until="domcontentloaded")
        return page, None
    except PWTimeout:
        return page, "timeout"
    except Exception as e:
        if "ERR_HTTP2_PROTOCOL_ERROR" in str(e) or "ERR_HTTP2" in str(e):
            try:
                context = page.context
                page.close()
                retry_page = context.new_page()
                retry_page.goto(url, timeout=nav_timeout, wait_until="load")
                return retry_page, None
            except Exception:
                return None, "exception"
        return page, "exception"


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

_AUTOSAVE_PATH = os.path.join(tempfile.gettempdir(), "webpage_search_gui_autosave.xlsx")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Webpage Keyword Search")
        self.geometry("820x600")
        self.resizable(True, True)

        self.input_path = tk.StringVar()
        self.fast_workers = tk.IntVar(value=15)
        self.pw_workers = tk.IntVar(value=4)
        self.timeout = tk.IntVar(value=20)
        self.case_sensitive = tk.BooleanVar(value=False)

        self.df = None
        self.results = []          # append-only, protected by self.lock
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.worker_thread = None
        self.total = 0

        self._build_ui()

    # ── UI ────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        frm_top = ttk.Frame(self)
        frm_top.pack(fill="x", **pad)
        ttk.Button(frm_top, text="Upload File (Excel/CSV)", command=self.on_upload).pack(side="left")
        ttk.Label(frm_top, textvariable=self.input_path).pack(side="left", padx=10)

        frm_opts = ttk.Frame(self)
        frm_opts.pack(fill="x", **pad)
        ttk.Label(frm_opts, text="Fast-pass workers:").pack(side="left")
        ttk.Spinbox(frm_opts, from_=5, to=40, width=4, textvariable=self.fast_workers).pack(side="left", padx=(4, 16))
        ttk.Label(frm_opts, text="Browser workers:").pack(side="left")
        ttk.Spinbox(frm_opts, from_=1, to=8, width=4, textvariable=self.pw_workers).pack(side="left", padx=(4, 16))
        ttk.Label(frm_opts, text="Timeout (s):").pack(side="left")
        ttk.Spinbox(frm_opts, from_=5, to=90, width=4, textvariable=self.timeout).pack(side="left", padx=(4, 16))
        ttk.Checkbutton(frm_opts, text="Case sensitive", variable=self.case_sensitive).pack(side="left")

        frm_run = ttk.Frame(self)
        frm_run.pack(fill="x", **pad)
        self.run_btn = ttk.Button(frm_run, text="Run Search", command=self.on_run)
        self.run_btn.pack(side="left")
        self.stop_btn = ttk.Button(frm_run, text="Stop && Save", command=self.on_stop, state="disabled")
        self.stop_btn.pack(side="left", padx=10)
        self.save_btn = ttk.Button(frm_run, text="Save Results As...", command=self.on_save, state="disabled")
        self.save_btn.pack(side="left")

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=8)

        counts_frame = ttk.Frame(self)
        counts_frame.pack(fill="x", padx=8)
        self.counts_var = tk.StringVar(value="")
        ttk.Label(counts_frame, textvariable=self.counts_var).pack(anchor="w")

        log_frame = ttk.Frame(self)
        log_frame.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(log_frame, wrap="word", height=20)
        scroll = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def log_line(self, text):
        self.log.insert("end", text + "\n")
        self.log.see("end")

    # ── Upload ────────────────────────────────────────────────────
    def on_upload(self):
        path = filedialog.askopenfilename(
            title="Select input file",
            filetypes=[("Excel/CSV files", "*.xlsx *.xls *.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            df = pd.read_csv(path, dtype={"Keyword": str, "URL": str}) if path.lower().endswith(".csv") \
                else pd.read_excel(path, dtype={"Keyword": str, "URL": str})
            df.columns = [c.strip() for c in df.columns]
            if "URL" not in df.columns or "Keyword" not in df.columns:
                messagebox.showerror("Invalid file", "File must have 'URL' and 'Keyword' columns.")
                return
            self.df = df.dropna(subset=["URL"]).reset_index(drop=True)
            self.input_path.set(path)
            self.status_var.set(f"Loaded {len(self.df)} rows from {Path(path).name}")
            self.log_line(f"Loaded {len(self.df)} rows from {path}")
        except Exception as e:
            messagebox.showerror("Failed to load file", str(e))

    # ── Run / Stop ────────────────────────────────────────────────
    def on_run(self):
        if self.df is None or self.df.empty:
            messagebox.showwarning("No file", "Upload a file first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "A job is already in progress.")
            return

        self.results = []
        self.stop_event = threading.Event()
        self.total = len(self.df)
        self.progress["value"] = 0
        self.progress["maximum"] = self.total
        self.run_btn.config(state="disabled")
        self.stop_btn.config(state="normal")
        self.save_btn.config(state="disabled")
        self.status_var.set("Running...")
        self.log_line("Starting job...")

        self.worker_thread = threading.Thread(target=self._run_worker, daemon=True)
        self.worker_thread.start()

    def on_stop(self):
        self.stop_event.set()
        self.status_var.set("Stopping — finishing in-flight requests...")
        self.stop_btn.config(state="disabled")

    def _add_result(self, res):
        with self.lock:
            self.results.append(res)
            n = len(self.results)
        if n % 25 == 0:
            self._autosave()
        self.after(0, self._on_row_ui_update, res, n)

    def _on_row_ui_update(self, res, n):
        self.progress.configure(value=n)
        self.log_line(f"{res['URL']} -> {res['Keyword_Search_Status']}")
        counts = {}
        with self.lock:
            for r in self.results:
                counts[r["Keyword_Search_Status"]] = counts.get(r["Keyword_Search_Status"], 0) + 1
        self.counts_var.set(
            "  ".join(f"{EMOJI.get(k,'')} {k}: {v}" for k, v in counts.items())
        )

    def _autosave(self):
        try:
            with self.lock:
                df = results_to_output_df(self.results)
            if not df.empty:
                with open(_AUTOSAVE_PATH, "wb") as f:
                    f.write(df_to_excel_bytes(df))
        except Exception:
            pass  # autosave must never crash the run

    def _run_worker(self):
        try:
            rows = self.df.to_dict("records")
            escalate_rows = []

            with ThreadPoolExecutor(max_workers=max(1, self.fast_workers.get())) as ex:
                futures = {ex.submit(fast_fetch, str(r["URL"]).strip(), self.timeout.get()): r for r in rows}
                for fut in as_completed(futures):
                    if self.stop_event.is_set():
                        for f in futures:
                            f.cancel()
                        break
                    row = futures[fut]
                    text, cat = fut.result()
                    res = finalize_row(row, text, cat, self.case_sensitive.get(), engine="Fast (HTTP)")
                    if res["_needs_escalation"]:
                        escalate_rows.append(row)
                    else:
                        self._add_result(res)

            if escalate_rows and not self.stop_event.is_set():
                self.after(0, self.status_var.set, "Rendering (browser pass)...")
                n_workers = max(1, min(self.pw_workers.get(), len(escalate_rows)))
                chunks = [escalate_rows[i::n_workers] for i in range(n_workers)]
                chunks = [c for c in chunks if c]
                with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
                    futs = [
                        ex.submit(run_playwright_batch, chunk, self.case_sensitive.get(),
                                  self.timeout.get() * 1000, self._add_result, self.stop_event)
                        for chunk in chunks
                    ]
                    for f in as_completed(futs):
                        f.result()

            self._autosave()
            self.after(0, self._on_job_finished, None)
        except Exception:
            err = traceback.format_exc()
            self._autosave()
            self.after(0, self._on_job_finished, err)

    def _on_job_finished(self, error):
        self.run_btn.config(state="normal")
        self.stop_btn.config(state="disabled")
        n = len(self.results)
        if error:
            self.status_var.set(f"Job stopped with an error — {n} rows still completed, see below.")
            self.log_line("ERROR:\n" + error)
            messagebox.showwarning(
                "Job stopped",
                f"An error interrupted the run, but {n} rows completed and are ready to save.\n\n"
                "Click 'Save Results As...' to keep them — see the log for the error detail.",
            )
        elif self.stop_event.is_set():
            self.status_var.set(f"Stopped by user. {n} rows completed.")
        else:
            self.status_var.set(f"Done. {n} rows processed.")
        if self.results:
            self.save_btn.config(state="normal")
            self.on_save(auto_prompt=True)

    # ── Save ──────────────────────────────────────────────────────
    def on_save(self, auto_prompt=False):
        if not self.results:
            if not auto_prompt:
                messagebox.showwarning("Nothing to save", "Run a search first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save results as",
            defaultextension=".xlsx",
            filetypes=[("Excel file", "*.xlsx"), ("CSV file", "*.csv")],
        )
        if not path:
            return
        with self.lock:
            out_df = results_to_output_df(self.results)
        if path.lower().endswith(".csv"):
            out_df.to_csv(path, index=False, encoding="utf-8-sig")
        else:
            with open(path, "wb") as f:
                f.write(df_to_excel_bytes(out_df))
        self.log_line(f"Saved output to {path}")
        messagebox.showinfo("Saved", f"Output saved to:\n{path}")


if __name__ == "__main__":
    App().mainloop()
