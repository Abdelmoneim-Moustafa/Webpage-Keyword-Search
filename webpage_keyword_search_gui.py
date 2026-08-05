"""
Webpage Keyword Search - Desktop GUI
=====================================
A simple Windows desktop app (Tkinter) wrapping the Playwright webpage
keyword search logic. Has an "Upload File" button, a Run button, a log
window, and a "Save Output" button. Packageable into a single .exe with
PyInstaller (see bottom of this file for the build command).

SETUP (one-time, on the machine you'll build/run on):
    pip install playwright pandas openpyxl
    playwright install chromium

RUN AS A SCRIPT (before building the exe, to test it works):
    python webpage_keyword_search_gui.py

BUILD AS A WINDOWS .EXE:
    pip install pyinstaller
    pyinstaller --onefile --noconsole --name WebpageKeywordSearch webpage_keyword_search_gui.py
    -> the .exe will be in the dist/ folder

    IMPORTANT: Playwright's browser binary is NOT bundled by PyInstaller
    automatically. After building, on the target machine you still need to
    run once:
        playwright install chromium
    (or ship the Playwright browser cache folder alongside the exe - see
    the note near the bottom of this file for the exact path).
"""

import asyncio
import re
import threading
import time
import traceback
from pathlib import Path

import pandas as pd
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from playwright.async_api import async_playwright, TimeoutError as PWTimeout

# --------------------------------------------------------------------------
# Same tunables as the CLI version - adjust per vendor as you learn each
# site's structure. See the comments in webpage_keyword_search.py for
# details on why these exist.
# --------------------------------------------------------------------------

EXPAND_SELECTORS_BY_HOST = {
    "phoenixcontact.com": ["text=Expand all", "button:has-text('Expand all')"],
    "festo.com": ["button:has-text('Show more')", "text=Show all"],
}

NAV_TIMEOUT_MS = 30000
POST_LOAD_WAIT_MS = 2500
MIN_BODY_TEXT_CHARS = 400
BLOCK_PAGE_SIGNS = [
    "access denied", "are you a human", "captcha", "unusual traffic",
    "request blocked", "403 forbidden", "pardon our interruption",
]
SNIPPET_RADIUS = 60


# --------------------------------------------------------------------------
# Core logic (same as the CLI tool)
# --------------------------------------------------------------------------

def host_of(url: str) -> str:
    m = re.search(r"https?://([^/]+)/?", url)
    return m.group(1).lower() if m else ""


def expand_selectors_for(url: str):
    host = host_of(url)
    for key, selectors in EXPAND_SELECTORS_BY_HOST.items():
        if key in host:
            return selectors
    return []


def search_keywords(text, keyword_field, case_sensitive):
    keywords = [k.strip() for k in str(keyword_field).split("|") if k.strip()]
    haystack = text if case_sensitive else text.lower()
    matched, missing = [], []
    total_count = 0
    first_snippet = ""

    for kw in keywords:
        needle = kw if case_sensitive else kw.lower()
        count = haystack.count(needle)
        if count > 0:
            matched.append(kw)
            total_count += count
            if not first_snippet:
                idx = haystack.find(needle)
                start = max(0, idx - SNIPPET_RADIUS)
                end = min(len(text), idx + len(needle) + SNIPPET_RADIUS)
                first_snippet = text[start:end].replace("\n", " ").strip()
        else:
            missing.append(kw)

    status = "Found" if matched else "Not Found"
    return status, total_count, first_snippet, "|".join(matched), "|".join(missing)


async def fetch_page_text(context, url):
    page = await context.new_page()
    try:
        try:
            await page.goto(url, timeout=NAV_TIMEOUT_MS, wait_until="domcontentloaded")
        except PWTimeout:
            return "Failed to load page", "", "Navigation timeout"
        except Exception as e:
            return "Failed to load page", "", f"Navigation error: {e}"

        try:
            await page.wait_for_load_state("networkidle", timeout=NAV_TIMEOUT_MS)
        except PWTimeout:
            pass

        for sel in expand_selectors_for(url):
            try:
                loc = page.locator(sel).first
                if await loc.is_visible(timeout=1500):
                    await loc.click(timeout=1500)
                    await page.wait_for_timeout(500)
            except Exception:
                pass

        await page.wait_for_timeout(POST_LOAD_WAIT_MS)
        body_text = await page.evaluate("document.body ? document.body.innerText : ''") or ""
        lowered = body_text.lower()

        if any(s in lowered for s in BLOCK_PAGE_SIGNS):
            return "Blocked / Access Denied", body_text, "Block-page phrase detected"
        if len(body_text.strip()) < MIN_BODY_TEXT_CHARS:
            return "Content did not fully render", body_text, (
                f"Body text only {len(body_text.strip())} chars after wait."
            )
        return None, body_text, ""
    finally:
        await page.close()


async def process_row(context, url, keyword_field, case_sensitive, sem):
    async with sem:
        load_status, body_text, notes = await fetch_page_text(context, url)
        if load_status is not None:
            return {
                "URL": url, "Keyword": keyword_field,
                "Keyword_Search_Status": load_status, "Match Count": 0,
                "Snippet": "", "Matched Keywords": "",
                "Missing Keywords": str(keyword_field), "Notes": notes,
            }
        status, count, snippet, matched, missing = search_keywords(
            body_text, keyword_field, case_sensitive
        )
        return {
            "URL": url, "Keyword": keyword_field,
            "Keyword_Search_Status": status, "Match Count": count,
            "Snippet": snippet, "Matched Keywords": matched,
            "Missing Keywords": missing, "Notes": "",
        }


async def run_job(df, workers, case_sensitive, headless, progress_cb, row_cb):
    results = []
    sem = asyncio.Semaphore(workers)

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
        )
        tasks = [
            process_row(context, str(r["URL"]).strip(), r["Keyword"], case_sensitive, sem)
            for _, r in df.iterrows()
        ]
        total = len(tasks)
        for i, coro in enumerate(asyncio.as_completed(tasks), start=1):
            res = await coro
            results.append(res)
            row_cb(res)
            progress_cb(i, total)
        await browser.close()
    return results


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Webpage Keyword Search")
        self.geometry("760x560")
        self.resizable(True, True)

        self.input_path = tk.StringVar()
        self.output_path = tk.StringVar()
        self.workers = tk.IntVar(value=4)
        self.case_sensitive = tk.BooleanVar(value=False)
        self.headless = tk.BooleanVar(value=True)
        self.results = []
        self.df = None
        self.worker_thread = None

        self._build_ui()

    def _build_ui(self):
        pad = {"padx": 8, "pady": 6}

        frm_top = ttk.Frame(self)
        frm_top.pack(fill="x", **pad)

        ttk.Button(frm_top, text="Upload File (Excel/CSV)", command=self.on_upload).pack(side="left")
        ttk.Label(frm_top, textvariable=self.input_path).pack(side="left", padx=10)

        frm_opts = ttk.Frame(self)
        frm_opts.pack(fill="x", **pad)

        ttk.Label(frm_opts, text="Concurrent tabs:").pack(side="left")
        ttk.Spinbox(frm_opts, from_=1, to=10, width=4, textvariable=self.workers).pack(side="left", padx=(4, 16))

        ttk.Checkbutton(frm_opts, text="Case sensitive", variable=self.case_sensitive).pack(side="left", padx=(0, 16))
        ttk.Checkbutton(frm_opts, text="Run headless (uncheck to watch the browser)",
                         variable=self.headless).pack(side="left")

        frm_run = ttk.Frame(self)
        frm_run.pack(fill="x", **pad)

        self.run_btn = ttk.Button(frm_run, text="Run Search", command=self.on_run)
        self.run_btn.pack(side="left")

        self.save_btn = ttk.Button(frm_run, text="Save Output As...", command=self.on_save, state="disabled")
        self.save_btn.pack(side="left", padx=10)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", **pad)

        self.status_var = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status_var).pack(anchor="w", padx=8)

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

    def on_upload(self):
        path = filedialog.askopenfilename(
            title="Select input file",
            filetypes=[("Excel/CSV files", "*.xlsx *.xls *.csv"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            df = pd.read_csv(path) if path.lower().endswith(".csv") else pd.read_excel(path)
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

    def on_run(self):
        if self.df is None or self.df.empty:
            messagebox.showwarning("No file", "Upload a file first.")
            return
        if self.worker_thread and self.worker_thread.is_alive():
            messagebox.showinfo("Already running", "A job is already in progress.")
            return

        self.results = []
        self.progress["value"] = 0
        self.progress["maximum"] = len(self.df)
        self.run_btn.config(state="disabled")
        self.save_btn.config(state="disabled")
        self.status_var.set("Running...")
        self.log_line("Starting job...")

        self.worker_thread = threading.Thread(target=self._run_in_thread, daemon=True)
        self.worker_thread.start()

    def _run_in_thread(self):
        try:
            df = self.df
            results = asyncio.run(run_job(
                df,
                workers=max(1, self.workers.get()),
                case_sensitive=self.case_sensitive.get(),
                headless=self.headless.get(),
                progress_cb=self._on_progress,
                row_cb=self._on_row_done,
            ))
            self.results = results
            self.after(0, self._on_job_finished, None)
        except Exception:
            err = traceback.format_exc()
            self.after(0, self._on_job_finished, err)

    def _on_progress(self, done, total):
        self.after(0, lambda: self.progress.configure(value=done))

    def _on_row_done(self, res):
        line = f"{res['URL']} -> {res['Keyword_Search_Status']}"
        self.after(0, self.log_line, line)

    def _on_job_finished(self, error):
        self.run_btn.config(state="normal")
        if error:
            self.status_var.set("Job failed - see log.")
            self.log_line("ERROR:\n" + error)
            messagebox.showerror("Job failed", "See the log window for details.")
            return
        self.status_var.set(f"Done. {len(self.results)} rows processed.")
        self.log_line("Job finished.")
        self.save_btn.config(state="normal")
        # auto-suggest save immediately so nothing is lost
        self.on_save()

    def on_save(self):
        if not self.results:
            messagebox.showwarning("Nothing to save", "Run a search first.")
            return
        path = filedialog.asksaveasfilename(
            title="Save output as",
            defaultextension=".xlsx",
            filetypes=[("Excel file", "*.xlsx"), ("CSV file", "*.csv")],
        )
        if not path:
            return
        out_df = pd.DataFrame(self.results)
        if path.lower().endswith(".csv"):
            out_df.to_csv(path, index=False)
        else:
            out_df.to_excel(path, index=False)
        self.output_path.set(path)
        self.log_line(f"Saved output to {path}")
        messagebox.showinfo("Saved", f"Output saved to:\n{path}")


if __name__ == "__main__":
    App().mainloop()

# --------------------------------------------------------------------------
# Notes on the .exe build
# --------------------------------------------------------------------------
# 1. pip install pyinstaller
# 2. pyinstaller --onefile --noconsole --name WebpageKeywordSearch webpage_keyword_search_gui.py
# 3. The exe appears in dist/WebpageKeywordSearch.exe
# 4. Playwright's Chromium binary lives outside the exe, typically at:
#       C:\Users\<you>\AppData\Local\ms-playwright\
#    On any machine you run the exe on (including a coworker's PC), that
#    folder needs to exist - either by running `playwright install chromium`
#    once on that machine, or by copying the ms-playwright folder over
#    alongside the exe and setting the environment variable
#    PLAYWRIGHT_BROWSERS_PATH to point at it before launching.
