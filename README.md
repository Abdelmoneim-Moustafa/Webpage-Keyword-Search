# 🌐 Webpage Keyword Search — v1.2

> **Fast · Resilient · 5-Status Output**
> Search for keywords on live product webpages at scale.
> Upload a file, start the job, download clean results — even if it crashes halfway.

Sibling tool to the PDF Keyword Search System — same input/output
conventions, same status schema — built for **live, JS-rendered product
pages** (NXP, Siemens, ABB, Ruland, Murrelektronik, HARTING, Phoenix
Contact, Festo, ifm, Rittal, Danfoss, and similar vendor sites) instead
of static PDFs.

---

## 🗂️ Project Structure

```
webpage-keyword-search/
├── app.py                ← Streamlit app — single file, no other project file needed
├── webpage_keyword_search_gui.py   ← Desktop GUI (Tkinter) — single file, packages to .exe
├── requirements.txt
└── README.md                       ← this file
```

Just **two interfaces now**, each fully self-contained — the engine that
used to live in a separate shared file is inlined into both, so you can
hand either one over on its own with nothing else to copy alongside it.
That does mean a future bug fix has to be applied to both files rather
than once — worth knowing if you're maintaining this yourself.

| Interface | Best for |
|---|---|
| **Streamlit app** | Day-to-day use — live progress, charts, a Stop button, recovery panel |
| **Desktop GUI (.exe)** | Handing the tool to someone without a Python setup |

---

## 🚀 Setup

```bash
pip install -r requirements.txt
playwright install chromium     # one-time, downloads the headless browser
```

**Run the Streamlit app:**
```bash
streamlit run app.py
```
Opens at `http://localhost:8501`

**Build the desktop .exe:**
```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name WebpageKeywordSearch webpage_keyword_search_gui.py
```
The `.exe` lands in `dist/WebpageKeywordSearch.exe` — no `--add-data`
flag needed, since this file has no sibling module to bundle anymore.
Playwright's browser binary still isn't bundled inside it — run
`playwright install chromium` once on whichever machine actually runs
the exe.

---

## 📋 Input File Format

Excel (.xlsx) or CSV with exactly these two columns:

| URL | Keyword |
|---|---|
| `https://www.nxp.com/part/BB202` | `Harmonized Tariff (US) Disclaimer` |
| `https://shop.murrelektronik.com/.../85055.html` | `Customs tariff number` |

- Multiple keywords in one row: separate with `\|` — a row is **Found**
  if *any* one of them matches
- Blank rows are ignored automatically
- Maximum **20,000 rows** per run (a real browser per page is heavier
  than a PDF download)
- Download the template from the Search tab or sidebar before filling in your data

---

## ⚙️ How the Engine Works

```
1. Known slow/JS-only site (ABB, Siemens, NXP, ...)?
       → skip straight to Pass 2 — the fast pass could never work there

2. PASS 1 — Fast HTTP
   Plain request + text extraction, many workers in parallel.
   ├─ real content found              → search it, done → "Fast (HTTP)"
   ├─ JS-shell / content too thin     → queue for Pass 2
   ├─ 403/404/429/503 status code     → queue for Pass 2 (see note below)
   └─ genuine block/captcha page      → mark Blocked, done

3. PASS 2 — Browser rendering (Playwright)
   Real headless Chromium, low concurrency, disguised as an ordinary
   browser (not flagged as automated).
   ├─ throttled per-host so a burst of requests to one vendor doesn't
   │  trip their bot-detection scoring
   ├─ clicks known per-vendor "Expand all" buttons, PLUS a generic
   │  best-effort pass over common tab/accordion labels (Commercial
   │  data, Specifications, Classifications, Product details, ...)
   ├─ retries once automatically on an HTTP/2 protocol error (hit ABB)
   ├─ retries once on a "Blocked" result after a short pause (hit NXP)
   └─ searches the rendered text, done → "Rendered (Browser)."
```

**Why 403/404 get a second chance:** several vendors (NXP, Ruland, and
likely others) return a 403 or 404 to plain non-browser HTTP requests
as an anti-bot measure, even though the same URL loads a
completely real page in an actual browser. A genuine "this part doesn't
exist" 404 looks identical at the HTTP level, so rather than guess, the
tool lets the browser make the real call.

### Known slow / JS-only sites
Currently: `abb.com`, `siemens.com`, `sieportal.siemens.com`, `nxp.com`.
Tell me if another vendor turns out to need this too — it's a one-line addition.

### Tab/accordion handling
Many vendors hide the compliance/customs data behind a collapsed
section — the label varies (Murrelektronik: "Commercial data",
Phoenix Contact: "Commercial & Classifications data", Renesas: "product
table", ifm: "Further information", ...). Rather than a selector per
vendor, the tool tries a broad list of the common label patterns on
every page — cheap to attempt, safely skipped where a label doesn't
exist. If a vendor's data still comes back "Needs Review" or "Not
Found" and you can see it in the actual page under a differently
labeled tab, tell me the label and I'll add it.

**Honest limitation:** I can't run this against the real vendor sites
from my side to verify every selector fires correctly — I based the
label list on the tab-reference sheet and general page structure, not a
live click-through of each site. Treat the first run against a new
batch as a check, not a guarantee.

---

## 🚦 The 5 Result Statuses

| Status | Meaning |
|---|---|
| ✅ **Found** | Keyword was located on the rendered page |
| ❌ **Not Found** | Page was read successfully — keyword is not present |
| 🟡 **Needs Review** | Content stayed too thin even after rendering — likely an unhandled tab/accordion label, or data gated behind login |
| 🟣 **Blocked / Access Denied** | A genuine bot-detection/captcha page that survived a retry — not just a page mentioning "captcha" in a footer |
| 🔺 **Failed to load page** | Page could not be reached at all (timeout, connection error, invalid URL) |

**Not Found** vs **Needs Review**: *Not Found* means the tool read real
content and the keyword genuinely isn't there. *Needs Review* means the
tool isn't confident it read real content in the first place.

---

## 📤 Output File Columns

Matches the PDF tool's schema exactly:

| Column | Description |
|---|---|
| `URL` | Original URL from your input file |
| `Keyword` | Keyword as entered in your input file |
| `Extraction Option` | `Fast (HTTP)` or `Rendered (Browser)` — which pass produced this row |
| `URL_Status` | `3` = page loaded OK · `0` = error |
| `URL_Search_Status` | `Done`, or the specific failure/skip/retry reason |
| `Keyword_Status` | `3.0` once a keyword search actually ran; blank otherwise |
| `feature_name` | Keyword(s) that matched (or the raw keyword, if not found) |
| `feature_value` | ~120-character context snippet around the first match |
| `Keyword_Search_Status` | **Main result** — one of the 5 statuses above |

### Excel Output — 6 Sheets
All Results · Found · Not Found · Needs Review · Blocked · Failed

---

## 💾 Nothing Gets Lost — Stopping, Crashing, Closing

- **Stop & Save** interrupts between individual rows — click it and
  whatever's done is immediately visible and downloadable.
- A genuine error mid-run doesn't just show an error and freeze — rows
  that already completed stay on screen with working downloads.
- Underneath both interfaces, progress also autosaves to disk every
  20–50 rows regardless of what's on screen:
  - **Streamlit** — sidebar "Saved Progress" panel: Restore, Clear, or
    Download Saved Progress.
  - **Desktop GUI** — autosaves to a temp file every 25 rows even
    though the Save dialog only prompts once the job finishes.

---

## ⚙️ Settings Reference

| Setting | Default | Range | Notes |
|---|---|---|---|
| **Fast-pass Workers** | 15 | 5 – 40 | Just HTTP requests — cheap, push it high |
| **Browser Workers** | 4 | 1 – 8 | Real Chromium instances — keep this low |
| **Timeout per URL** | 20 s | 5 – 90 s | Known-slow sites get an extended timeout automatically |
| **Case-Sensitive Search** | OFF | ON / OFF | OFF: `ABC` matches `abc` |

---

## 🛠 Requirements

```
streamlit>=1.32.0
pandas>=2.0.0
openpyxl>=3.1.0
requests>=2.31.0
urllib3>=2.0.0
beautifulsoup4>=4.12.0
playwright>=1.42.0
```
**Python 3.10+** required. After `pip install`, also run once:
`playwright install chromium`
