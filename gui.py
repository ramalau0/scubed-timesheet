"""
S-Cubed Timesheet — desktop GUI
Wraps timesheet_bot.py in a simple Tkinter window.
"""
import asyncio
import io
import os
import random
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, scrolledtext, ttk
from datetime import datetime

# When packaged with PyInstaller, redirect Playwright's browser lookup to a
# persistent location in the user's home directory instead of the temp bundle dir.
if getattr(sys, 'frozen', False) and 'PLAYWRIGHT_BROWSERS_PATH' not in os.environ:
    os.environ['PLAYWRIGHT_BROWSERS_PATH'] = str(Path.home() / '.ms-playwright')

import timesheet_bot
from timesheet_bot import (
    AuthRequired,
    ids_configured,
    load_catalog,
    run,
    week_ending_for,
    working_days,
    write_env,
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
        self.root.geometry("720x680")
        self.root.resizable(True, True)
        self.root.minsize(600, 500)
        self._busy = False
        self._last_command = None
        self._save_result = None  # "success", "failed", or None
        self.root.protocol("WM_DELETE_WINDOW", self._on_close_request)

        self._build_ui()
        self._refresh_status()
        self._set_busy(True)
        self.root.after(200, self._ensure_browser)

    def _build_ui(self):
        outer = ttk.Frame(self.root, padding=16)
        outer.pack(fill=tk.BOTH, expand=True)

        # ── Title & instructions ─────────────────────────────────────────
        title_frame = ttk.Frame(outer)
        title_frame.pack(fill=tk.X, pady=(0, 4))
        ttk.Label(title_frame, text="S-Cubed Timesheet Assistant",
                  font=("TkDefaultFont", 14, "bold")).pack(anchor="w")

        instructions = (
            "This tool creates draft timesheet entries on S-Cubed from your "
            "Outlook calendar and git history. Drafts are saved but NOT "
            "submitted for approval — you must review and submit them "
            "yourself in S-Cubed."
        )
        instr_label = ttk.Label(outer, text=instructions, wraplength=680,
                                foreground="gray", justify=tk.LEFT)
        instr_label.pack(anchor="w", pady=(0, 10))

        # ── Status ───────────────────────────────────────────────────────
        self.status_var = tk.StringVar()
        ttk.Label(outer, textvariable=self.status_var,
                  font=("TkDefaultFont", 11, "bold")).pack(anchor="w")

        # ── Week info ────────────────────────────────────────────────────
        today = datetime.today()
        week_end = week_ending_for(today)
        days = working_days(week_end)
        week_label = (f"Current week:  {days[0].strftime('%a %d %b')} – "
                      f"{days[-1].strftime('%a %d %b %Y')}")
        ttk.Label(outer, text=week_label, foreground="gray").pack(
            anchor="w", pady=(4, 8))

        # ── Settings section ─────────────────────────────────────────────
        settings_frame = ttk.LabelFrame(outer, text="Settings", padding=8)
        settings_frame.pack(fill=tk.X, pady=(0, 10))

        # Employee ID row
        emp_row = ttk.Frame(settings_frame)
        emp_row.pack(fill=tk.X, pady=(0, 6))
        ttk.Label(emp_row, text="Employee ID:").pack(side=tk.LEFT)
        self.emp_var = tk.StringVar(
            value=str(EMPLOYEE_ID) if EMPLOYEE_ID else "")
        self.emp_entry = ttk.Entry(emp_row, textvariable=self.emp_var, width=12)
        self.emp_entry.pack(side=tk.LEFT, padx=(8, 4))
        ttk.Button(emp_row, text="Save", width=6,
                   command=self._on_save_employee_id).pack(side=tk.LEFT)
        emp_hint = ("Auto-detected during setup. If S-Cubed shows the wrong "
                    "name on your timesheets, correct this number here.")
        ttk.Label(emp_row, text=emp_hint, foreground="gray",
                  wraplength=380, justify=tk.LEFT).pack(
                      side=tk.LEFT, padx=(8, 0))

        # Work folder row
        workdir_row = ttk.Frame(settings_frame)
        workdir_row.pack(fill=tk.X, pady=(0, 0))
        self.workdir_var = tk.StringVar()
        ttk.Label(workdir_row, text="Work folder:").pack(side=tk.LEFT)
        ttk.Label(workdir_row, textvariable=self.workdir_var,
                  foreground="gray").pack(side=tk.LEFT, padx=(8, 0))
        ttk.Button(workdir_row, text="Change...", width=10,
                   command=self._on_change_workdir).pack(side=tk.RIGHT)
        self._refresh_workdir_label()

        # ── How to use section ───────────────────────────────────────────
        steps_frame = ttk.LabelFrame(outer, text="How to use", padding=8)
        steps_frame.pack(fill=tk.X, pady=(0, 10))

        steps = [
            ("Step 1 — First-time Setup",
             "Run once to discover your employee ID, clients, and projects "
             "from S-Cubed. A browser window will open for you to log in."),
            ("Step 2 — Preview",
             "See what your timesheet entries will look like before saving. "
             "Shows calendar events, hours, and comments for each day."),
            ("Step 3 — Save Draft",
             "Creates draft entries on S-Cubed for the current week. "
             "Does NOT submit for approval — review in S-Cubed first."),
        ]
        for title, desc in steps:
            step_frame = ttk.Frame(steps_frame)
            step_frame.pack(fill=tk.X, pady=(0, 4))
            ttk.Label(step_frame, text=title,
                      font=("TkDefaultFont", 9, "bold")).pack(anchor="w")
            ttk.Label(step_frame, text=desc, foreground="gray",
                      wraplength=660, justify=tk.LEFT).pack(
                          anchor="w", padx=(16, 0))

        # ── Action buttons ───────────────────────────────────────────────
        btn_row = ttk.Frame(outer)
        btn_row.pack(fill=tk.X, pady=(0, 8))

        self.btn_setup = ttk.Button(
            btn_row, text="1. First-time Setup", width=20,
            command=self._on_setup)
        self.btn_setup.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_preview = ttk.Button(
            btn_row, text="2. Preview", width=14,
            command=self._on_preview)
        self.btn_preview.pack(side=tk.LEFT, padx=(0, 8))

        self.btn_submit = ttk.Button(
            btn_row, text="3. Save Draft", width=14,
            command=self._on_submit)
        self.btn_submit.pack(side=tk.LEFT)

        draft_note = ttk.Label(
            outer,
            text="Save Draft only creates entries — it does NOT submit "
                 "them for manager approval.",
            foreground="#b8860b", justify=tk.LEFT)
        draft_note.pack(anchor="w", pady=(0, 6))

        # ── Progress indicator ───────────────────────────────────────────
        self.progress = ttk.Progressbar(outer, mode="indeterminate")
        self.progress.pack(fill=tk.X, pady=(0, 6))

        # ── Log output ───────────────────────────────────────────────────
        log_label = ttk.Label(outer, text="Activity log:",
                              font=("TkDefaultFont", 9, "bold"))
        log_label.pack(anchor="w", pady=(0, 2))
        self.log = scrolledtext.ScrolledText(
            outer, height=14, font=("Courier", 9), state=tk.DISABLED,
            wrap=tk.WORD)
        self.log.pack(fill=tk.BOTH, expand=True)

    # ── Employee ID ──────────────────────────────────────────────────────

    def _on_save_employee_id(self):
        val = self.emp_var.get().strip()
        if not val:
            messagebox.showwarning("Employee ID",
                                   "Please enter your employee ID number.")
            return
        try:
            int(val)
        except ValueError:
            messagebox.showwarning("Employee ID",
                                   "Employee ID must be a number.")
            return
        write_env({"EMPLOYEE_ID": val})
        self._refresh_status()
        self._append(f"Employee ID updated to {val}.")

    # ── Work folder ──────────────────────────────────────────────────────

    def _refresh_workdir_label(self):
        self.workdir_var.set(str(timesheet_bot.WORK_DIR))

    def _on_change_workdir(self):
        chosen = filedialog.askdirectory(
            title="Select your work projects folder",
            initialdir=str(timesheet_bot.WORK_DIR),
        )
        if chosen:
            write_env({"WORK_DIR": chosen})
            self._refresh_workdir_label()

    # ── Status ───────────────────────────────────────────────────────────

    def _refresh_status(self):
        emp_id = timesheet_bot.EMPLOYEE_ID
        catalog = load_catalog()
        if catalog or ids_configured():
            emp = f"Employee {emp_id}" if emp_id else "catalog ready"
            self.status_var.set(f"Ready — {emp}")
        else:
            self.status_var.set(
                "Not configured — click First-time Setup to get started")
        if emp_id:
            self.emp_var.set(str(emp_id))

    # ── Browser setup ────────────────────────────────────────────────────

    def _ensure_browser(self):
        """Install Playwright Chromium if missing. Blocks buttons until done."""
        browser_path = Path(
            os.environ.get('PLAYWRIGHT_BROWSERS_PATH')
            or _default_playwright_browsers_dir())
        existing = list(browser_path.glob('chromium_headless_shell-*'))
        if existing:
            self._set_busy(False)
            return

        self._append(
            "Installing browser (first time only, please wait)...")

        def worker():
            try:
                if getattr(sys, 'frozen', False):
                    driver_root = (Path(sys._MEIPASS) / 'playwright'
                                   / 'driver')
                    node = driver_root / (
                        'node.exe' if sys.platform == 'win32' else 'node')
                    cli = driver_root / 'package' / 'cli.js'
                    cmd = [str(node), str(cli), 'install', 'chromium']
                else:
                    from playwright._impl._driver import (
                        compute_driver_executable)
                    node, cli = compute_driver_executable()
                    cmd = [node, cli, 'install', 'chromium']

                kwargs = {}
                if sys.platform == 'win32':
                    kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
                result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
                if result.returncode == 0:
                    self._log_from_thread("Browser installed. Ready.")
                else:
                    self._log_from_thread(
                        f"Browser install failed:\n{result.stderr[:400]}")
            except Exception as exc:
                self._log_from_thread(f"Browser install error: {exc}")
            finally:
                self.root.after(0, lambda: self._set_busy(False))

        threading.Thread(target=worker, daemon=True).start()

    # ── Log helpers ──────────────────────────────────────────────────────

    def _append(self, text: str):
        """Append text to the log widget (must be called from main thread)."""
        self.log.configure(state=tk.NORMAL)
        self.log.insert(tk.END, text if text.endswith("\n") else text + "\n")
        self.log.see(tk.END)
        self.log.configure(state=tk.DISABLED)

    def _log_from_thread(self, text: str):
        """Thread-safe log append via Tkinter's after()."""
        self.root.after(0, lambda t=text: self._append(t))

    # ── Button state ─────────────────────────────────────────────────────

    def _set_busy(self, busy: bool):
        self._busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (self.btn_preview, self.btn_submit, self.btn_setup):
            btn.configure(state=state)
        self.emp_entry.configure(state=state)
        if busy:
            self.progress.start(12)
        else:
            self.progress.stop()

    def _on_close_request(self):
        if self._busy:
            if not messagebox.askyesno(
                "Quit while busy?",
                "A task is still running — possibly a browser window "
                "waiting for you to log in. Quitting now will close "
                "that browser too.\n\nQuit anyway?",
            ):
                return
        self.root.destroy()

    # ── Command runner ───────────────────────────────────────────────────

    def _run(self, command: str):
        self._set_busy(True)
        self._last_command = command
        self._save_result = None
        self._append(f"\n{'=' * 56}")
        self._append(f"  {command.upper()}")
        self._append(f"{'=' * 56}")

        def _check_output(text: str):
            if command == "create" and self._save_result is None:
                if "Draft saved and verified" in text or "Draft save request accepted" in text:
                    self._save_result = "success"
                elif "Save failed" in text:
                    self._save_result = "failed"

        def worker():
            def log_and_check(text):
                _check_output(text)
                self._log_from_thread(text)

            redirect = RedirectText(log_and_check)
            old_stdout, old_stderr = sys.stdout, sys.stderr
            sys.stdout = redirect
            sys.stderr = redirect
            try:
                asyncio.run(
                    run(headless=True, command=command, target=None))
            except AuthRequired as exc:
                self._log_from_thread(
                    f"[AUTH] {exc} — opening browser for login...")
                try:
                    asyncio.run(
                        run(headless=False, command=command, target=None))
                except Exception as e2:
                    self._log_from_thread(f"Error: {e2}")
                    if command == "create":
                        self._save_result = "failed"
            except Exception as exc:
                self._log_from_thread(f"Error: {exc}")
                if command == "create":
                    self._save_result = "failed"
            finally:
                sys.stdout, sys.stderr = old_stdout, old_stderr
                self.root.after(0, self._done)

        threading.Thread(target=worker, daemon=True).start()

    def _done(self):
        self._set_busy(False)
        self._refresh_status()
        if self._last_command == "create" and self._save_result:
            if self._save_result == "success":
                self._show_celebration()
            else:
                self._show_failure()

    # ── Save result feedback ────────────────────────────────────────────

    def _show_failure(self):
        messagebox.showerror(
            "Save Failed",
            "Timesheet draft could not be saved.\n\n"
            "Check the activity log below for details.\n"
            "Common causes: wrong Employee ID, entries already\n"
            "exist for this week, or network issues.")

    def _show_celebration(self):
        overlay = tk.Canvas(
            self.root, highlightthickness=0, bg="black")
        overlay.place(relx=0, rely=0, relwidth=1, relheight=1)
        overlay.update_idletasks()
        w = overlay.winfo_width()
        h = overlay.winfo_height()

        overlay.create_text(
            w // 2, h // 2 - 30,
            text="Timesheet Saved!",
            font=("TkDefaultFont", 28, "bold"),
            fill="#00ff88", tags="msg")
        overlay.create_text(
            w // 2, h // 2 + 20,
            text="Your draft entries are on S-Cubed.\nReview and submit for approval in S-Cubed.",
            font=("TkDefaultFont", 12),
            fill="white", tags="msg", justify=tk.CENTER)

        colors = ["#ff6b6b", "#ffd93d", "#6bcb77", "#4d96ff",
                  "#ff6fff", "#00d2ff", "#ff9f43", "#ffffff"]
        particles = []
        for _ in range(80):
            x = random.randint(0, w)
            y = random.randint(-h, 0)
            size = random.randint(4, 10)
            color = random.choice(colors)
            vx = random.uniform(-3, 3)
            vy = random.uniform(1, 5)
            shape = random.choice(["rect", "oval"])
            if shape == "rect":
                pid = overlay.create_rectangle(
                    x, y, x + size, y + size * 0.6,
                    fill=color, outline="")
            else:
                pid = overlay.create_oval(
                    x, y, x + size, y + size,
                    fill=color, outline="")
            particles.append({
                "id": pid, "x": x, "y": y,
                "vx": vx, "vy": vy, "size": size,
                "spin": random.uniform(-0.1, 0.1)})

        frame = [0]

        def animate():
            if frame[0] > 120:
                overlay.destroy()
                return
            for p in particles:
                p["x"] += p["vx"]
                p["vy"] += 0.12
                p["y"] += p["vy"]
                p["vx"] *= 0.99
                dx, dy = p["vx"], p["vy"]
                overlay.move(p["id"], dx, dy)
            if frame[0] > 80:
                fade = max(0, (120 - frame[0]) / 40)
                grey = int(fade * 255)
                msg_color = f"#{grey:02x}{min(255, int(fade * 255)):02x}{grey:02x}"
                overlay.itemconfig("msg", fill=msg_color)
            frame[0] += 1
            overlay.after(25, animate)

        overlay.after(50, animate)
        overlay.bind("<Button-1>", lambda e: overlay.destroy())

    # ── Button handlers ──────────────────────────────────────────────────

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
