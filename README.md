# 🌐 Webpage Keyword Search — v1.0

> **Fast · Resilient · 5-Status Output**
> Search for keywords on live product webpages at scale.
> Upload a file, start the job, download clean results.

Sibling tool to the [PDF Keyword Search System](./README.md) — same
input/output conventions, same status schema, different engine: this tool
targets **live JS-rendered product pages** (Siemens SiePortal, Festo, ifm,
Phoenix Contact, Rittal, Danfoss, Mersen, etc.) instead of static PDFs.

---

## ✨ What This System Does

You provide a list of product-page URLs and keywords (e.g. an export
control classification field like `uiExportControlValue`). The system
loads each page, reads the rendered text, searches for your keyword, and
reports exactly what it found — in one of five clear result values.

Unlike a PDF, most of these pages load their real data via JavaScript
*after* the initial page load — a plain HTTP request sees an empty shell.
So the tool runs a **two-pass hybrid engine**: a fast plain-HTTP pass for
any page whose data is already in the server HTML, and a real headless
browser only for the pages that actually need JavaScript to render.

---

## ✅ Key Features

| Feature | Details |
|---|---|
| ⚡ **Two-pass hybrid engine** | Fast HTTP pass first; only escalates to a real browser for pages that need JS rendering |
| 🖱️ **Auto-expand accordions** | Clicks "Expand all" / "Show more" sections on vendor sites that hide specs behind an accordion |
| 🔁 **Automatic retry** | Failed downloads retried; per-URL timeout is configurable |
| 💾 **Auto-save every 20–50 rows** | Progress saved to disk — survives page refresh, lost connection, or a crashed browser |
| ♻️ **Recovery panel** | Restore or download partial results at any time |
| 🔍 **Full text search** | Counts every occurrence, not just the first; multi-keyword `|` syntax supported |
| 📊 **Charts & status legend** | Color-coded result counts and a bar chart on the Results tab |
| ☀️🌙 **Adaptive theme** | Clean in both light and dark mode |

---

## 🗂️ Project Structure

```
webpage-keyword-search/
├── app.py       ← Streamlit application (primary interface)
├── webpage_keyword_search_gui.py   ← Desktop GUI version (Tkinter, packages to .exe)
├── requirements.txt        ← Python dependencies
└── README.md                       ← This file
```

Three interfaces, same underlying logic — pick whichever fits how you
work:

| Interface | Best for |
|---|---|
| **Streamlit app** | Day-to-day use, browser-based, matches the PDF tool's UI |
| **Desktop GUI (.exe)** | Handing the tool to someone without a Python setup |

---

## 🚀 Setup

### Requirements

- **Python 3.10 or higher**
- Internet access to load pages
- ~300 MB free disk for the Chromium browser Playwright installs

### Install and Run (Streamlit)

```bash
pip install -r webpage_requirements.txt
playwright install chromium

streamlit run streamlit_app.py
```

Opens at `http://localhost:8501`

### Build the Desktop .exe

```bash
pip install pyinstaller
pyinstaller --onefile --noconsole --name WebpageKeywordSearch webpage_keyword_search_gui.py
```
The `.exe` lands in `dist/WebpageKeywordSearch.exe`. Note: the Chromium
binary Playwright uses is **not** bundled inside the exe — run
`playwright install chromium` once on whichever machine actually runs it.

---

## 📋 Input File Format

Upload an **Excel (.xlsx)** or **CSV** file with exactly these two columns:

| URL | Keyword |
|---|---|
| `https://example.com/product/12345` | `uiExportControlValue` |
| `https://example.com/product/67890` | `ECCN\|uiExportControlValue` |

**Notes:**
- `URL` must be a direct link to a product page
- `Keyword` is the exact term/field name to search for
- For multiple keywords in one row, separate with `|` — e.g. `term1|term2|term3`
- Blank rows are ignored automatically
- Maximum **20,000 rows** per run (lower than the PDF tool's 50,000 —
  browser rendering is heavier per row than a PDF download)
- Download the template from the Search tab or sidebar before filling your data

---

## 🚦 The 5 Result Statuses

Every row in the output will have exactly one of these values in
`Keyword_Search_Status`:

| Status | Meaning |
|---|---|
| ✅ **Found** | Keyword was located on the rendered page |
| ❌ **Not Found** | Page was read successfully — keyword is not present |
| 🟡 **Needs Review** *(page did not fully render)* | Content stayed too thin even after browser rendering — usually means the site needs an extra click (accordion, "load more") not yet configured, or the data is gated behind login |
| 🟣 **Blocked / Access Denied** | Bot detection, captcha, or an explicit access-denied page |
| 🔺 **Failed to load page** | Page could not be reached at all (timeout, connection error, invalid URL) |

> The difference between **Not Found** and **Needs Review**:
> - **Not Found** = the tool read real page content, keyword is simply absent
> - **Needs Review** = the tool couldn't confirm it read *real* content in the first place — treat this as "unverified," not "absent"

---

## 📤 Output File Columns

Matches the PDF tool's schema exactly, so both tools' outputs can be
handled by the same downstream process:

| Column | Description |
|---|---|
| `URL` | Original URL from your input file |
| `Keyword` | Keyword as entered in your input file |
| `Extraction Option` | Which engine produced the result: `Fast (HTTP)` or `Rendered (Browser)` |
| `URL_Status` | `3` = page loaded OK · `0` = error |
| `URL_Search_Status` | `Done`, or the specific failure reason |
| `Keyword_Status` | `3.0` once a keyword search actually ran; blank otherwise |
| `feature_name` | Keyword(s) that matched (or the raw keyword, if not found) |
| `feature_value` | ~120-character context snippet around the first match |
| `Keyword_Search_Status` | **Main result** — one of the 5 statuses above |

### Excel Output — 6 Sheets

| Sheet | Contents |
|---|---|
| **All Results** | Every row |
| **Found** | Keyword was found |
| **Not Found** | Page read, keyword absent |
| **Needs Review** | Content too thin even after rendering |
| **Blocked** | Bot detection / access denied |
| **Failed** | Could not be loaded |

---

## ⚙️ Settings Reference

| Setting | Default | Range | Description |
|---|---|---|---|
| **Fast-pass Workers** | 15 | 5 – 40 | Parallel plain-HTTP requests. This pass is cheap — push it high. |
| **Browser Workers** | 4 | 1 – 8 | Parallel headless-browser instances for the escalation pass. Keep this low — each one is a real browser process. |
| **Timeout per URL** | 20 s | 5 – 60 s | Max wait per page before giving up. |
| **Case-Sensitive Search** | OFF | ON / OFF | OFF: `ABC` matches `abc`. ON: exact case only. |

---

## 🔄 How the Two-Pass Engine Decides

```
For each URL:
  1. Plain HTTP GET + extract visible text
  2. Text looks real (long enough, no "enable JavaScript" placeholder,
     no bot-block phrases)?
       → search it now, done — "Fast (HTTP)"
  3. Otherwise, queue it for the browser pass
       → load in headless Chromium, click any known "expand" buttons
         for that vendor, wait for network to settle, re-read the text
       → search it now, done — "Rendered (Browser)"
```

This means a batch of mixed vendors resolves the easy pages almost
immediately and only pays the slower, heavier cost where a page actually
needs JavaScript to reveal its content.

---

## 💾 Auto-Save and Recovery

- Results saved to disk every **50 rows** (fast pass) / **20 rows**
  (render pass) during processing
- Survives: page refresh · internet drop · browser close · a crashed
  Playwright instance
- The **Saved Progress** panel in the sidebar lets you:
  - See how many rows are saved and when
  - Download saved data as CSV
  - Restore saved results to the Results tab
  - Clear the saved file after downloading

---

## ⚡ Performance Tips

- Leave **Fast-pass Workers** high (15–40) — it's just HTTP requests, cheap to parallelize
- Keep **Browser Workers** low (2–6) — each one launches a real Chromium instance
- If a specific vendor consistently lands in **Needs Review**, check the
  Guide tab — it likely needs an expand-selector added for that vendor
- Sites that block plain HTTP outright (e.g. via `robots.txt` or a WAF)
  will show up as **Failed** on the fast pass and get escalated
  automatically — that's expected, not a bug

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

**Python 3.10 or higher is required.**

After `pip install`, also run once:
```bash
playwright install chromium
```
