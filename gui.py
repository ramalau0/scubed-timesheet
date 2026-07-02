"""
S-Cubed Timesheet — desktop GUI
Wraps timesheet_bot.py in a simple Tkinter window.
"""
import asyncio
import io
import os
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext, ttk
from datetime import datetime

# When packaged with PyInstaller, redirect Playwright's browser lookup to a
# persistent location in the user's home directory instead of the temp bundle dir.
if getattr(sys, 'frozen', False) and 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path.home() / '.ms-playwright')

from timesheet_bot import (
    AuthRequired,
    ids_configured,
    load_catalog,
    run,
    week_ending_for,
    working_days,
    EMPLOYEE_ID,
)


def _default_playwright_browsers_dir() -> Path:
    """Playwright's own default cache dir when PLAYWRIGHT_BROWSERS_PATH isn't set."""
    if sys.platform == 'win32':
        base = Path(os.environ.get('LOCALAPPDATA', Path.home() / 'AppData' / 'Local'))
    elif sys.platform == 'darwin':
        base = Path.home() / 'Library' / 'Caches'
    else:
        base = Path(os.environ.get('XDG_CACHE_HOME', Path.home() / '.cache'))
    return base / 'ms-playwright'


class RedirectText(io.StringIO):
    """Captures print() output and forwards it to a callback on the main thread."""

    def __init__(self, callback):
        super().__init__()
        self._callback = callback

    def write(self, s):
        if s and s != "\n":
            self._callback(s)

    def flush(self):
        pass


class TimesheetApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("S-Cubed Timesheet")
        self.root.geometry("640x520")
        self.root.resizable(False, False)

        self._build_ui()
        self._refresh_status()
        # On Mac/Linux: install Chromium before allowing any actions (Windows uses system Edge)
        if sys.platform != "win32":
            self._set_busy(True)
            self.root.after(200, self._ensure_browser)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        # Status + week info
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var, font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

        today = datetime.today()
        week_end = week_ending_for(today)
        days = working_days(week_end)
        week_label = f"Week:  {days[0].strftime('%a %d %b')} – {days[-1].strftime('%a %d %b %Y')}"
        ttk.Label(outer, text=week_label, foreground="gray").pack(anchor="w", pady=(4, 14))

        # Buttons
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill=tk.X, pady=(0, 12))

        self.btn_preview = ttk.Button(btn_row, text="Preview", width=14, command=self._on_preview)
        self.btn_preview.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_submit = ttk.Button(btn_row, text="Submit Timesheet", width=18, command=self._on_submit)
        self.btn_submit.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_setup = ttk.Button(btn_row, text="First-time Setup", width=18, command=self._on_setup)
        self.btn_setup.pack(side=tk.LEFT)

        # Progress indicator
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(0, 8))

        # Log output
        self.log = scrolledtext.ScrolledText(
            outer, height=20, font=("Courier", 9), state=tk.DISABLED, wrap=tk.WORD
        )
        self.log.pack(fill=tk.BOTH, expand=True)

    # ── Status ────────────────────────────────────────────────────────────────

    def _refresh_status(self):
        catalog = load_catalog()
        if catalog or ids_configured():
            emp = f"Employee {EMPLOYEE_ID}" if EMPLOYEE_ID else "catalog ready"
            self.status_var.set(f"✅  Ready — {emp}")
        else:
            self.status_var.set("⚠️   Not configured — click First-time Setup")

    # ── Browser setup ─────────────────────────────────────────────────────────

    def _ensure_browser(self):
        """Install Playwright Chromium if missing. Blocks buttons until done."""
        # Frozen builds set PLAYWRIGHT_BROWSERS_PATH explicitly (see top of file).
        # Source runs don't, so fall back to Playwright's actual per-OS cache dir —
        # not the frozen-build path — otherwise this reinstalls Chromium every launch.
        browser_path = Path(os.environ.get('PLAYWRIGHT_BROWSERS_PATH') or _default_playwright_browsers_dir())
        # Check if any chromium_headless_shell directory already exists
        existing = list(browser_path.glob('chromium_headless_shell-*'))
        if existing:
            self._set_busy(False)
            return

        self._append("Installing browser (first time only, please wait)...")

        def worker():
            try:
                # In a frozen PyInstaller bundle the driver lives in _MEIPASS.
                # There's no standalone "playwright" launcher script — the
                # driver is invoked as `node cli.js ...`.
                if getattr(sys, 'frozen', False):
                    driver_root = Path(sys._MEIPASS) / 'playwright' / 'driver'
                    node = driver_root / ('node.exe' if sys.platform == 'win32' else 'node')
                    cli = driver_root / 'package' / 'cli.js'
                    cmd = [str(node), str(cli), 'install', 'chromium']
                else:
                    from playwright._impl._driver import compute_driver_executable
                    node, cli = compute_driver_executable()
                    cmd = [node, cli, 'install', 'chromium']

                result = subprocess.run(
                    cmd,
                    capture_output=True, text=True,
                )
                if result.returncode == 0:
                    self._log_from_thread("Browser installed. Ready.")
                else:
                    self._log_from_thread(f"⚠️  Browser install failed:\n{result.stderr[:400]}")
            except Exception as exc:
                self._log_from_thread(f"⚠️  Browser install error: {exc}")
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    # ── Log helpers ───────────────────────────────────────────────────────────

    def _append(self, text: str):
        """Append text to the log widget (must be called from main thread)."""
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text if text.endswith("\n") else text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _log_from_thread(self, text: str):
        """Thread-safe log append via Tkinter's after()."""
        self.root.after(0, lambda t=text: self._append(t))

    # ── Button state ──────────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (self.btn_preview, self.btn_submit, self.btn_setup):
            btn.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    # ── Command runner ────────────────────────────────────────────────────────

    def _run(self, command: str):
        self._set_busy(True)
        self._append(f"\n── {command.upper()} {'─' * (40 - len(command))}")

        def worker():
            redirect = RedirectText(self._log_from_thread)
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = redirect
            sys.stderr = redirect
            try:
                asyncio.run(run(headless=True, command=command, target=None))
            except AuthRequired as exc:
                self._log_from_thread(f"[AUTH] {exc} — opening browser…")
                try:
                    asyncio.run(run(headless=False, command=command, target=None))
                except Exception as e2:
                    self._log_from_thread(f"Error: {e2}")
            except Exception as exc:
                self._log_from_thread(f"Error: {exc}")
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self.root.after(0, self._done)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self):
        self._set_busy(False)
        self._refresh_status()

    # ── Button handlers ───────────────────────────────────────────────────────

    def _on_preview(self):
        self._run("preview")

    def _on_submit(self):
        self._run("create")

    def _on_setup(self):
        self._run("discover_all")


def main():
    root = tk.Tk()
    TimesheetApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
