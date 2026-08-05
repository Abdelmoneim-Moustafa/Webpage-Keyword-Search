# ═══════════════════════════════════════════════════════════════════
# Webpage Keyword Search — Streamlit app
# Sibling of the PDF Keyword Search tool. Same output schema, same
# autosave/recovery pattern, same UI conventions — different engine
# underneath, because these targets are live JS-rendered product pages
# (Siemens SiePortal, Festo, ifm, Phoenix Contact, Rittal, Danfoss, etc)
# instead of static PDFs.
#
# SPEED STRATEGY (why this is fast, not just "a browser for everything"):
#   Pass 1 — plain HTTP GET + BeautifulSoup, many workers in parallel.
#            Cheap and very fast. Works for any page whose data is in
#            the server-rendered HTML.
#   Pass 2 — only the rows that Pass 1 couldn't read (JS-rendered,
#            too-short body, blocked) get escalated to a real headless
#            browser (Playwright). Slower, but only pays that cost
#            where it's actually needed.
#   This mirrors the spirit of your PDF tool's request → mirror-retry
#   pattern, just with a browser as the "retry" instead of a mirror URL.
# ═══════════════════════════════════════════════════════════════════
import streamlit as st
import pandas as pd
import io
import os
import re
import time
import json
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

st.set_page_config(
    page_title="Webpage Keyword Search",
    page_icon="🌐",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ══════════════════════════════════════════════════════════════════
# SECTION 1 — Session state
# ══════════════════════════════════════════════════════════════════
if "results_df" not in st.session_state: st.session_state.results_df = None
if "running"    not in st.session_state: st.session_state.running    = False

SEARCH_LIMIT       = 20_000     # lower than the PDF tool's 50k — browser
                                 # rendering is heavier per-row than PDF DL
DEFAULT_FAST_WORKERS = 15
DEFAULT_PW_WORKERS   = 4
DEFAULT_TIMEOUT      = 20

_AUTOSAVE_FILE = "/tmp/webpage_search_autosave.csv"
_AUTOSAVE_META = "/tmp/webpage_search_meta.json"


# ══════════════════════════════════════════════════════════════════
# SECTION 2 — Theme-aware CSS (colors via Streamlit CSS vars only)
# ══════════════════════════════════════════════════════════════════
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
# SECTION 3 — Status enum + colors (mirrors the PDF tool's S class)
# ══════════════════════════════════════════════════════════════════
class S:
    FOUND        = "Found"
    NOT_FOUND    = "Not Found"
    RENDER_ISSUE = "Page did not fully render — needs review (JS/selectors)"
    BLOCKED      = "Blocked / Access Denied"
    FAILED       = "Failed to load page"

_DOT_COLOR = {
    S.FOUND:        "#16a34a",
    S.NOT_FOUND:    "#6b7280",
    S.RENDER_ISSUE: "#f59e0b",
    S.BLOCKED:      "#dc2626",
    S.FAILED:       "#7c3aed",
}
_EMOJI = {
    S.FOUND: "✅", S.NOT_FOUND: "❌", S.RENDER_ISSUE: "🟡",
    S.BLOCKED: "🟣", S.FAILED: "🔺",
}

OUTPUT_COLUMNS = [
    "URL", "Keyword", "Extraction Option", "URL_Status", "URL_Search_Status",
    "Keyword_Status", "feature_name", "feature_value", "Keyword_Search_Status",
]

# ══════════════════════════════════════════════════════════════════
# SECTION 4 — Fast HTTP pass (BeautifulSoup)
# ══════════════════════════════════════════════════════════════════
_MIN_USEFUL_CHARS = 400
_JS_PLACEHOLDER_SIGNS = [
    "please enable javascript", "enable javascript to continue",
    "loading, please wait", "you need to enable javascript",
]
_BLOCK_SIGNS = [
    "access denied", "are you a human", "captcha", "unusual traffic",
    "request blocked", "403 forbidden", "pardon our interruption",
]
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")

_session_local = threading.local()


def _get_session():
    if not hasattr(_session_local, "s"):
        s = requests.Session()
        retry = Retry(total=2, backoff_factor=0.5,
                       status_forcelist=[429, 500, 502, 503, 504])
        s.mount("https://", HTTPAdapter(max_retries=retry))
        s.mount("http://", HTTPAdapter(max_retries=retry))
        s.headers.update({"User-Agent": _UA})
        _session_local.s = s
    return _session_local.s


def _extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    return soup.get_text(separator=" ", strip=True)


def _classify_text(text: str):
    """Returns 'ok' | 'js_placeholder' | 'blocked' | 'too_short'."""
    lowered = text.lower()
    if any(s in lowered for s in _BLOCK_SIGNS):
        return "blocked"
    if any(s in lowered for s in _JS_PLACEHOLDER_SIGNS):
        return "js_placeholder"
    if len(text.strip()) < _MIN_USEFUL_CHARS:
        return "too_short"
    return "ok"


def _fast_fetch(url: str, timeout: int):
    """Returns (text_or_None, category). category in:
    ok / js_placeholder / blocked / too_short / http_error / exception"""
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

    text = _extract_visible_text(resp.text)
    return text, _classify_text(text)


# ══════════════════════════════════════════════════════════════════
# SECTION 5 — Playwright escalation pass (only for rows Pass 1 missed)
# ══════════════════════════════════════════════════════════════════
EXPAND_SELECTORS_BY_HOST = {
    "phoenixcontact.com": ["text=Expand all", "button:has-text('Expand all')"],
    "festo.com":          ["button:has-text('Show more')", "text=Show all"],
}
NAV_TIMEOUT_MS       = 30000
POST_LOAD_WAIT_MS    = 2000


def _host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url)
    return m.group(1).lower() if m else ""


def _expand_selectors_for(url: str):
    host = _host_of(url)
    for key, sels in EXPAND_SELECTORS_BY_HOST.items():
        if key in host:
            return sels
    return []


def _playwright_fetch(context, url: str, timeout_ms: int):
    """Runs inside an already-open Playwright browser context.
    Returns (text_or_None, category)."""
    from playwright.sync_api import TimeoutError as PWTimeout
    page = context.new_page()
    try:
        try:
            page.goto(url, timeout=timeout_ms, wait_until="domcontentloaded")
        except PWTimeout:
            return None, "timeout"
        except Exception:
            return None, "exception"

        try:
            page.wait_for_load_state("networkidle", timeout=timeout_ms)
        except PWTimeout:
            pass

        for sel in _expand_selectors_for(url):
            try:
                loc = page.locator(sel).first
                if loc.is_visible(timeout=1200):
                    loc.click(timeout=1200)
                    page.wait_for_timeout(400)
            except Exception:
                pass

        page.wait_for_timeout(POST_LOAD_WAIT_MS)
        text = page.evaluate("document.body ? document.body.innerText : ''") or ""
        return text, _classify_text(text)
    finally:
        page.close()


def _run_playwright_batch(rows_subset, case_sensitive, timeout_ms, on_row_done):
    """Runs a chunk of rows through one Playwright browser instance,
    sequentially within this worker thread."""
    from playwright.sync_api import sync_playwright
    results = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            context = browser.new_context(user_agent=_UA)
            for row in rows_subset:
                text, cat = _playwright_fetch(context, row["URL"], timeout_ms)
                res = _finalize_row(row, text, cat, case_sensitive, engine="Rendered (Browser)")
                results.append(res)
                on_row_done(res)
            browser.close()
    except Exception as e:
        # Playwright itself unavailable/broken (e.g. browser not installed)
        for row in rows_subset:
            res = _finalize_row(
                row, None, "exception", case_sensitive,
                engine="Rendered (Browser)",
                note=f"Playwright error: {e}",
            )
            results.append(res)
            on_row_done(res)
    return results


# ══════════════════════════════════════════════════════════════════
# SECTION 6 — Keyword search + row finalization (matches PDF schema)
# ══════════════════════════════════════════════════════════════════
def _parse_keywords(raw):
    return [k.strip() for k in str(raw).split("|") if k.strip()]


def _search_keyword(text, kw, case_sensitive):
    hay = text if case_sensitive else text.lower()
    needle = kw if case_sensitive else kw.lower()
    return hay.count(needle)


def _best_snippet(text, kw, case_sensitive, radius=60):
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
}


def _finalize_row(row, text, category, case_sensitive, engine, note=""):
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

    if category in ("timeout", "ssl", "connection", "exception") or (category and category.startswith("http_")):
        msg = _CAT_MSG.get(category, category)
        if category and category.startswith("http_"):
            msg = f"HTTP {category.split('_')[1]}"
        return done(URL_Status=0, URL_Search_Status=note or msg,
                    Keyword_Search_Status=S.FAILED)

    if category == "blocked":
        return done(URL_Status=0, URL_Search_Status="Blocked by site",
                    Keyword_Search_Status=S.BLOCKED)

    if category in ("js_placeholder", "too_short"):
        # Pass 1 flags this for escalation; Pass 2 (Playwright) sets the
        # real terminal status. If we get here from Pass 2 itself and
        # STILL see this, it's a genuine render problem worth a human look.
        return done(URL_Status=3, URL_Search_Status="Rendered but content too thin",
                    Keyword_Search_Status=S.RENDER_ISSUE,
                    _needs_escalation=(engine == "Fast (HTTP)"))

    # category == "ok" -> real keyword search
    keywords = _parse_keywords(raw_keyword) or [str(raw_keyword)]
    found, missing, total = [], [], 0
    snippet = ""
    for kw in keywords:
        cnt = _search_keyword(text, kw, case_sensitive)
        if cnt > 0:
            found.append(kw)
            total += cnt
            if not snippet:
                snippet = _best_snippet(text, kw, case_sensitive)
        else:
            missing.append(kw)

    if found:
        return done(URL_Status=3, URL_Search_Status="Done", Keyword_Status=3.0,
                    feature_name=", ".join(found), feature_value=snippet,
                    Keyword_Search_Status=S.FOUND)
    return done(URL_Status=3, URL_Search_Status="Done", Keyword_Status=3.0,
                feature_name=str(raw_keyword), Keyword_Search_Status=S.NOT_FOUND)


# ══════════════════════════════════════════════════════════════════
# SECTION 7 — Autosave / recovery (mirrors PDF tool exactly)
# ══════════════════════════════════════════════════════════════════
def _autosave(result_dicts, processed, total):
    try:
        df = pd.DataFrame(result_dicts)[OUTPUT_COLUMNS]
        df.to_csv(_AUTOSAVE_FILE, index=False, encoding="utf-8-sig")
        with open(_AUTOSAVE_META, "w") as f:
            json.dump({
                "rows": len(df), "processed": processed, "total": total,
                "saved_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            }, f)
    except Exception:
        pass  # autosave must never crash the run


def _load_autosave():
    try:
        if os.path.exists(_AUTOSAVE_FILE) and os.path.getsize(_AUTOSAVE_FILE) > 0:
            df = pd.read_csv(_AUTOSAVE_FILE, dtype={"Keyword": str, "URL": str})
            meta = {}
            if os.path.exists(_AUTOSAVE_META):
                with open(_AUTOSAVE_META) as f:
                    meta = json.load(f)
            return df, meta
    except Exception:
        pass
    return None, None


def _clear_autosave():
    for p in (_AUTOSAVE_FILE, _AUTOSAVE_META):
        try:
            os.remove(p)
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# SECTION 8 — Output builders
# ══════════════════════════════════════════════════════════════════
def _sheet(writer, df, col, val, name):
    sub = df[df[col] == val]
    if not sub.empty:
        sub.to_excel(writer, sheet_name=name, index=False)


def df_to_excel_bytes(df):
    buf = io.BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        clean = df[OUTPUT_COLUMNS]
        clean.to_excel(w, sheet_name="All Results", index=False)
        _sheet(w, clean, "Keyword_Search_Status", S.FOUND, "Found")
        _sheet(w, clean, "Keyword_Search_Status", S.NOT_FOUND, "Not Found")
        _sheet(w, clean, "Keyword_Search_Status", S.RENDER_ISSUE, "Needs Review")
        _sheet(w, clean, "Keyword_Search_Status", S.BLOCKED, "Blocked")
        _sheet(w, clean, "Keyword_Search_Status", S.FAILED, "Failed")
    return buf.getvalue()


def df_to_csv_bytes(df):
    return df[OUTPUT_COLUMNS].to_csv(index=False).encode("utf-8-sig")


def _make_template():
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


# ══════════════════════════════════════════════════════════════════
# SECTION 9 — Sidebar
# ══════════════════════════════════════════════════════════════════
with st.sidebar:
    st.markdown('<div class="wks-side-title">🌐 Webpage Keyword Search</div>', unsafe_allow_html=True)
    st.markdown(f'<div class="wks-limit-banner">⚠️ Limit {SEARCH_LIMIT:,} rows / run</div>', unsafe_allow_html=True)

    st.markdown("---")
    st.markdown('<div class="wks-side-title">⚡ Search Configuration</div>', unsafe_allow_html=True)
    case_sensitive = st.checkbox("Case-Sensitive Search", value=False)

    st.markdown('<div class="wks-side-title">🚀 Performance</div>', unsafe_allow_html=True)
    fast_workers = st.slider(
        "Fast-pass Workers (HTTP)", 5, 40, DEFAULT_FAST_WORKERS, 1,
        help="Parallel plain HTTP requests. This pass is cheap — push it high.",
    )
    pw_workers = st.slider(
        "Browser Workers (Playwright)", 1, 8, DEFAULT_PW_WORKERS, 1,
        help="Parallel headless-browser instances for pages the fast pass "
             "couldn't read. Keep this low (2-6) — each one is a real browser.",
    )
    timeout = st.slider("Per-URL Timeout (sec)", 5, 60, DEFAULT_TIMEOUT, 5)

    st.markdown("---")
    st.markdown('<div class="wks-side-title">📥 Input Template</div>', unsafe_allow_html=True)
    st.download_button(
        "⬇️ Download Template (.xlsx)", data=_make_template(),
        file_name="Webpage_Keyword_Search_Template.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        use_container_width=True,
    )

    st.markdown("---")
    st.markdown('<div class="wks-side-title">📊 Status Legend</div>', unsafe_allow_html=True)
    legend_html = "".join(
        f'<div class="wks-legend-row"><span class="wks-dot" style="background:{_DOT_COLOR[s]}"></span>'
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

    saved_df, saved_meta = _load_autosave()
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
                st.rerun()
        with cr2:
            if st.button("🗑 Clear", use_container_width=True):
                _clear_autosave()
                st.rerun()
        st.download_button(
            "📥 Download Saved Progress", data=df_to_csv_bytes(saved_df),
            file_name=f"partial_{saved_meta.get('saved_at','').replace(' ','_').replace(':','-')}.csv",
            mime="text/csv", use_container_width=True,
        )

# ══════════════════════════════════════════════════════════════════
# SECTION 10 — Header
# ══════════════════════════════════════════════════════════════════
status_badge = "🟢 Ready" if not st.session_state.running else "🟠 Running"
st.markdown(f"""
<div class="wks-hero">
  <div>
    <div style="font-size:1.3rem;font-weight:800;">🌐 Webpage Keyword Search</div>
    <div style="opacity:0.75;font-size:0.85rem;">Fast HTTP pass · Browser fallback for JS pages · 5-Status Output</div>
  </div>
  <div class="wks-badge" style="background:{'#16a34a' if not st.session_state.running else '#f59e0b'};">{status_badge}</div>
</div>
""", unsafe_allow_html=True)

tab_search, tab_results, tab_logs, tab_guide = st.tabs(["🔍 Search", "📄 Results", "📋 Logs", "📘 Guide"])

# ══════════════════════════════════════════════════════════════════
# SECTION 11 — Search tab
# ══════════════════════════════════════════════════════════════════
with tab_search:
    c_left, c_right = st.columns([2, 1])

    with c_left:
        st.markdown("### 📁 Upload Input File")
        st.caption("Excel (.xlsx) or CSV with `URL` and `Keyword` columns.")
        uploaded = st.file_uploader("Upload", type=["xlsx", "xls", "csv"], label_visibility="collapsed")
        st.download_button(
            "⬇️ Download Template to Fill In", data=_make_template(),
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

    if uploaded:
        try:
            if uploaded.name.endswith(".csv"):
                input_df = pd.read_csv(uploaded, dtype={"Keyword": str, "URL": str})
            else:
                input_df = pd.read_excel(uploaded, dtype={"Keyword": str, "URL": str})
            input_df.columns = [c.strip() for c in input_df.columns]

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
                bc1, bc2, bc3 = st.columns([3, 1, 1])
                with bc1:
                    start_btn = st.button("🚀 Start Search", use_container_width=True,
                                           type="primary", disabled=st.session_state.running)
                with bc2:
                    stop_btn = st.button("⏹ Stop", use_container_width=True)
                with bc3:
                    if st.session_state.results_df is not None:
                        if st.button("🗑 Clear", use_container_width=True):
                            st.session_state.results_df = None
                            st.rerun()

                if stop_btn:
                    st.session_state.running = False

                if start_btn and not st.session_state.running:
                    st.session_state.running = True
                    st.session_state.results_df = None
                    rows = input_df.to_dict("records")
                    total = len(rows)

                    prog_bar = st.progress(0, text="Starting fast pass...")
                    status_txt = st.empty()
                    log_box = st.empty()

                    all_results, escalate_rows = [], []
                    completed = [0]
                    log_lines = []
                    start_ts = time.time()

                    def _log(msg):
                        ts = datetime.now().strftime("%H:%M:%S")
                        log_lines.append(f"[{ts}] {msg}")
                        if len(log_lines) > 150:
                            log_lines.pop(0)

                    def _refresh(label):
                        pct = min(completed[0] / total, 1.0) if total else 1.0
                        elapsed = time.time() - start_ts
                        rate = completed[0] / elapsed if elapsed > 0 else 0
                        eta = (total - completed[0]) / rate if rate > 0 else 0
                        prog_bar.progress(pct, text=f"[{label}] {completed[0]:,}/{total:,} · "
                                                     f"{rate:.1f}/s · ETA {eta:.0f}s")
                        status_txt.markdown(
                            f"**{S.FOUND}:** {sum(1 for r in all_results if r['Keyword_Search_Status']==S.FOUND)} · "
                            f"**{S.NOT_FOUND}:** {sum(1 for r in all_results if r['Keyword_Search_Status']==S.NOT_FOUND)} · "
                            f"**Needs Review:** {sum(1 for r in all_results if r['Keyword_Search_Status']==S.RENDER_ISSUE)} · "
                            f"**Blocked:** {sum(1 for r in all_results if r['Keyword_Search_Status']==S.BLOCKED)} · "
                            f"**Failed:** {sum(1 for r in all_results if r['Keyword_Search_Status']==S.FAILED)}"
                        )
                        log_box.code("\n".join(log_lines[-12:]) or "…")

                    # ── PASS 1: fast HTTP, high concurrency ──────────────
                    with ThreadPoolExecutor(max_workers=fast_workers) as ex:
                        futures = {}
                        for row in rows:
                            fut = ex.submit(_fast_fetch, str(row["URL"]).strip(), timeout)
                            futures[fut] = row

                        for fut in as_completed(futures):
                            row = futures[fut]
                            text, cat = fut.result()
                            res = _finalize_row(row, text, cat, case_sensitive, engine="Fast (HTTP)")
                            if res["_needs_escalation"]:
                                escalate_rows.append(row)
                            else:
                                all_results.append(res)
                                completed[0] += 1
                                _log(f"[fast] {row['URL']} -> {res['Keyword_Search_Status']}")

                            if completed[0] % 50 == 0:
                                _autosave(all_results, completed[0], total)
                            _refresh("Fast pass")

                    # ── PASS 2: Playwright escalation, low concurrency ───
                    if escalate_rows and st.session_state.running:
                        _log(f"Escalating {len(escalate_rows)} rows to browser rendering...")
                        _refresh("Rendering")
                        chunks = [escalate_rows[i::pw_workers] for i in range(pw_workers)]
                        chunks = [c for c in chunks if c]

                        def _on_row_done(res):
                            all_results.append(res)
                            completed[0] += 1
                            _log(f"[render] {res['URL']} -> {res['Keyword_Search_Status']}")
                            if completed[0] % 20 == 0:
                                _autosave(all_results, completed[0], total)
                            _refresh("Rendering")

                        with ThreadPoolExecutor(max_workers=len(chunks)) as ex:
                            futs = [ex.submit(_run_playwright_batch, chunk, case_sensitive, timeout * 1000, _on_row_done)
                                    for chunk in chunks]
                            for f in as_completed(futs):
                                f.result()

                    _autosave(all_results, completed[0], total)
                    result_df = pd.DataFrame(all_results)[OUTPUT_COLUMNS]
                    st.session_state.results_df = result_df
                    st.session_state.running = False
                    _log("Done.")
                    st.rerun()

        except Exception as e:
            st.error(f"Failed to read file: {e}")

# ══════════════════════════════════════════════════════════════════
# SECTION 12 — Results tab (with charts + colored status)
# ══════════════════════════════════════════════════════════════════
with tab_results:
    rdf = st.session_state.results_df
    if rdf is None or rdf.empty:
        st.info("Run a search to see results here.")
    else:
        m1, m2, m3, m4, m5 = st.columns(5)
        counts = rdf["Keyword_Search_Status"].value_counts()
        for col, status, label in [
            (m1, S.FOUND, "Found"), (m2, S.NOT_FOUND, "Not Found"),
            (m3, S.RENDER_ISSUE, "Needs Review"), (m4, S.BLOCKED, "Blocked"),
            (m5, S.FAILED, "Failed"),
        ]:
            col.metric(f"{_EMOJI[status]} {label}", int(counts.get(status, 0)))

        chart = rdf["Keyword_Search_Status"].value_counts().reset_index()
        chart.columns = ["Status", "Count"]
        st.bar_chart(chart.set_index("Status"))

        st.markdown("---")
        all_statuses = rdf["Keyword_Search_Status"].unique().tolist()
        sel = st.multiselect("Filter by status", all_statuses, default=all_statuses)
        flt = rdf[rdf["Keyword_Search_Status"].isin(sel)]

        display_df = flt.copy()
        display_df["Keyword_Search_Status"] = display_df["Keyword_Search_Status"].apply(
            lambda s: f"{_EMOJI.get(s,'')} {s}"
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
# SECTION 13 — Logs tab
# ══════════════════════════════════════════════════════════════════
with tab_logs:
    st.caption("Live log appears in the Search tab while a job is running. "
               "Autosave writes to disk every 50 rows (fast pass) / 20 rows "
               "(render pass) so a crash or lost connection never loses "
               "more than that.")
    saved_df, saved_meta = _load_autosave()
    if saved_df is not None:
        st.write(saved_meta)
        st.dataframe(saved_df.tail(50), use_container_width=True)
    else:
        st.info("No autosave file present yet.")

# ══════════════════════════════════════════════════════════════════
# SECTION 14 — Guide tab
# ══════════════════════════════════════════════════════════════════
with tab_guide:
    st.markdown("""
### How this differs from the PDF tool
Live product pages often load their real spec data via JavaScript *after*
the page loads — a plain HTTP request sees an empty shell. So every URL
runs through two passes:

1. **Fast pass** — plain HTTP request + text extraction. Cheap, parallel,
   handles any page whose data is already in the server HTML.
2. **Browser pass** — only for rows the fast pass couldn't read. A real
   headless browser renders the page (including clicking "expand" sections
   on sites like Phoenix Contact) before reading the text.

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
That means even the browser pass couldn't get enough content — usually
because the site needs an extra click (an accordion, a "load more"
button) that isn't in the expand-selector list yet, or the data is
gated behind a login. Tell me which vendor and I'll add its selector.
""")
