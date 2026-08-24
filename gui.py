"""
POCUS Ghost-Tracker - Graphical front end
=========================================

A Tkinter GUI wrapping parser.py and sessionizer.py unchanged - all detection
logic, rules.json interpretation, and session classification happen exactly
as they do from the command line. This file only handles: letting the user
pick log sources without manually building the logs/<vendor>/<device_id>/
folder structure, exposing the tunable parameters (idle gap, window size,
date range) as inputs instead of constants buried in sessionizer.py, running
the two pipeline stages in a background thread so the window stays
responsive, and showing progress/results.

Requires only the Python standard library (Tkinter ships with a normal
Python install on Windows/Mac; on Linux it may need a separate package,
e.g. `sudo apt install python3-tk`).
"""

import os
import sys
import json
import shutil
import tempfile
import platform
import subprocess
import threading
import queue
import zipfile
import urllib.request
import urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from parser import run_parser, load_rules as parser_load_rules
from sessionizer import run_sessionizer

DEFAULT_RULES_PATH = "rules.json"
# If your default branch isn't "main", update the branch segment below too.
DEFAULT_RULES_URL = "https://raw.githubusercontent.com/briancgreen/POCUS-Ghost-Tracker/main/rules.json"


# ---------------------------------------------------------------------------
# Staging: turn arbitrary user-picked folders/ZIPs into the logs/<vendor>/
# <device_id>/ structure parser.py already expects, with no changes needed
# to parser.py itself.
# ---------------------------------------------------------------------------

def _is_flat(folder):
    """True if `folder` contains only files at its top level (no subfolders)."""
    entries = os.listdir(folder)
    return all(os.path.isfile(os.path.join(folder, e)) for e in entries) if entries else True


def stage_sources(sources, staging_root, progress=print):
    """
    sources: list of (vendor, source_path, label) tuples, source_path a
    folder or .zip, label a user-supplied device name (empty string = derive
    one automatically).

    A flat folder/zip (files only at top level) becomes ONE device. If a
    label was given, it's used as the device_id; otherwise the folder/zip
    name is used, same as before. A folder/zip containing only subfolders
    becomes MULTIPLE devices, one per subfolder - the label is ignored in
    this case (each subfolder name is already a distinct device identifier,
    same convention as the existing logs/<vendor>/<device_id>/ layout), and
    a message is logged so the user isn't left wondering why their typed
    label didn't appear.

    Returns nothing; populates staging_root in place for run_parser to read.
    """
    extract_tmp = tempfile.mkdtemp(prefix="ghosttracker_zip_")
    try:
        for vendor, source_path, label in sources:
            vendor_dir = os.path.join(staging_root, vendor)
            os.makedirs(vendor_dir, exist_ok=True)

            if source_path.lower().endswith(".zip"):
                zip_name = os.path.splitext(os.path.basename(source_path))[0]
                dest = os.path.join(extract_tmp, zip_name)
                os.makedirs(dest, exist_ok=True)
                progress(f"  Extracting {os.path.basename(source_path)}...")
                with zipfile.ZipFile(source_path) as zf:
                    zf.extractall(dest)
                # A zip often wraps its contents in one top-level folder -
                # unwrap that so flat/nested detection below sees the real content.
                inner = os.listdir(dest)
                if len(inner) == 1 and os.path.isdir(os.path.join(dest, inner[0])):
                    dest = os.path.join(dest, inner[0])
                source_root = dest
                default_device_name = zip_name
            else:
                source_root = source_path
                default_device_name = os.path.basename(os.path.normpath(source_path))

            if _is_flat(source_root):
                device_name = label.strip() if label and label.strip() else default_device_name
                device_dir = os.path.join(vendor_dir, device_name)
                shutil.copytree(source_root, device_dir, dirs_exist_ok=True)
                progress(f"  {vendor} | {source_path} -> device \"{device_name}\"")
            else:
                if label and label.strip():
                    progress(f"  Note: \"{label}\" ignored for {source_path} - it contains multiple "
                              f"subfolders, so each subfolder name is used as its own device label instead.")
                for entry in os.listdir(source_root):
                    entry_path = os.path.join(source_root, entry)
                    if os.path.isdir(entry_path):
                        device_dir = os.path.join(vendor_dir, entry)
                        shutil.copytree(entry_path, device_dir, dirs_exist_ok=True)
                        progress(f"  {vendor} | {entry_path} -> device \"{entry}\"")
    finally:
        shutil.rmtree(extract_tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# Main application
# ---------------------------------------------------------------------------

class GhostTrackerApp:
    def __init__(self, root):
        self.root = root
        root.title("POCUS Ghost-Tracker")
        root.geometry("820x720")

        self.rules_path = tk.StringVar(value=DEFAULT_RULES_PATH)
        self.rules_url = tk.StringVar(value=DEFAULT_RULES_URL)
        self.output_folder = tk.StringVar(value=os.path.abspath("ghosttracker_output"))
        self.sources = []  # list of (vendor, path)

        self.session_gap = tk.StringVar(value="5")
        self.window_days = tk.StringVar(value="5")
        self.use_date_range = tk.BooleanVar(value=False)
        self.date_start = tk.StringVar(value="")
        self.date_end = tk.StringVar(value="")

        self.msg_queue = queue.Queue()
        self.task_running = False
        self.last_csv_path = None

        self._build_ui()
        self._load_vendors()
        self.root.after(100, self._poll_queue)

    # -- UI construction -----------------------------------------------

    def _build_ui(self):
        pad = {"padx": 8, "pady": 4}

        # --- Rules file ---
        rules_frame = ttk.LabelFrame(self.root, text="Rules file")
        rules_frame.pack(fill="x", **pad)

        ttk.Entry(rules_frame, textvariable=self.rules_path, width=50).grid(row=0, column=0, sticky="we", **pad)
        ttk.Button(rules_frame, text="Browse...", command=self._browse_rules).grid(row=0, column=1, **pad)
        ttk.Entry(rules_frame, textvariable=self.rules_url, width=50).grid(row=1, column=0, sticky="we", **pad)
        ttk.Button(rules_frame, text="Download Latest from GitHub", command=self._download_rules).grid(row=1, column=1, **pad)
        rules_frame.columnconfigure(0, weight=1)

        # --- Add log source ---
        src_frame = ttk.LabelFrame(self.root, text="Log sources")
        src_frame.pack(fill="x", **pad)

        ttk.Label(src_frame, text="Vendor:").grid(row=0, column=0, **pad)
        self.vendor_combo = ttk.Combobox(src_frame, state="readonly", width=16)
        self.vendor_combo.grid(row=0, column=1, **pad)

        ttk.Label(src_frame, text="Device label:").grid(row=0, column=2, **pad)
        self.device_label = tk.StringVar(value="")
        ttk.Entry(src_frame, textvariable=self.device_label, width=20).grid(row=0, column=3, **pad)
        ttk.Label(src_frame, text="(leave blank to use the folder/zip name)", foreground="gray").grid(row=1, column=2, columnspan=2, sticky="w", padx=8)

        ttk.Button(src_frame, text="Add Folder...", command=self._add_folder).grid(row=0, column=4, **pad)
        ttk.Button(src_frame, text="Add ZIP...", command=self._add_zip).grid(row=0, column=5, **pad)

        self.source_list = tk.Listbox(src_frame, height=6)
        self.source_list.grid(row=2, column=0, columnspan=6, sticky="we", **pad)
        ttk.Button(src_frame, text="Remove Selected", command=self._remove_source).grid(row=3, column=0, sticky="w", **pad)
        src_frame.columnconfigure(1, weight=1)

        # --- Parameters ---
        param_frame = ttk.LabelFrame(self.root, text="Parameters")
        param_frame.pack(fill="x", **pad)

        ttk.Label(param_frame, text="Idle gap (minutes):").grid(row=0, column=0, sticky="e", **pad)
        ttk.Entry(param_frame, textvariable=self.session_gap, width=8).grid(row=0, column=1, sticky="w", **pad)

        ttk.Label(param_frame, text="Window size (days):").grid(row=0, column=2, sticky="e", **pad)
        ttk.Entry(param_frame, textvariable=self.window_days, width=8).grid(row=0, column=3, sticky="w", **pad)

        ttk.Checkbutton(param_frame, text="Restrict to date range", variable=self.use_date_range).grid(row=1, column=0, sticky="w", **pad)
        ttk.Label(param_frame, text="Start (YYYY-MM-DD):").grid(row=1, column=1, sticky="e", **pad)
        ttk.Entry(param_frame, textvariable=self.date_start, width=12).grid(row=1, column=2, sticky="w", **pad)
        ttk.Label(param_frame, text="End (YYYY-MM-DD):").grid(row=1, column=3, sticky="e", **pad)
        ttk.Entry(param_frame, textvariable=self.date_end, width=12).grid(row=1, column=4, sticky="w", **pad)

        ttk.Label(param_frame, text="Output folder:").grid(row=2, column=0, sticky="e", **pad)
        ttk.Entry(param_frame, textvariable=self.output_folder, width=50).grid(row=2, column=1, columnspan=3, sticky="we", **pad)
        ttk.Button(param_frame, text="Browse...", command=self._browse_output).grid(row=2, column=4, **pad)

        # --- Actions ---
        action_frame = ttk.Frame(self.root)
        action_frame.pack(fill="x", **pad)

        self.parse_btn = ttk.Button(action_frame, text="1. Parse Logs", command=self._run_parse)
        self.parse_btn.pack(side="left", **pad)
        self.sessionize_btn = ttk.Button(action_frame, text="2. Sessionize Parsed Logs", command=self._run_sessionize)
        self.sessionize_btn.pack(side="left", **pad)
        self.open_csv_btn = ttk.Button(action_frame, text="Open Device Summary", command=self._open_csv, state="disabled")
        self.open_csv_btn.pack(side="left", **pad)

        # --- Progress log ---
        log_frame = ttk.LabelFrame(self.root, text="Progress")
        log_frame.pack(fill="both", expand=True, **pad)
        self.log_text = scrolledtext.ScrolledText(log_frame, state="disabled", height=20)
        self.log_text.pack(fill="both", expand=True, **pad)

    # -- Vendor list from rules.json ------------------------------------

    def _load_vendors(self):
        try:
            rules = parser_load_rules(self.rules_path.get())
            vendors = sorted(rules.get("_vendor_aliases", {}).values())
            vendors = sorted(set(vendors))
            self.vendor_combo["values"] = vendors
            if vendors:
                self.vendor_combo.current(0)
        except Exception as e:
            self._log(f"Could not load vendors from {self.rules_path.get()}: {e}")

    def _browse_rules(self):
        path = filedialog.askopenfilename(title="Select rules.json", filetypes=[("JSON files", "*.json")])
        if path:
            self.rules_path.set(path)
            self._load_vendors()

    def _download_rules(self):
        url = self.rules_url.get().strip()
        if "<your-username>" in url:
            messagebox.showwarning("Set the URL first", "Edit the rules.json URL field to point at your actual GitHub repo before downloading.")
            return
        try:
            self._log(f"Downloading rules.json from {url} ...")
            with urllib.request.urlopen(url, timeout=15) as resp:
                data = resp.read()
            parsed = json.loads(data)  # validate before overwriting anything
            if "_vendor_aliases" in parsed:
                del parsed["_vendor_aliases"]  # defensive: shouldn't be in source file anyway
            dest = self.rules_path.get() or DEFAULT_RULES_PATH
            with open(dest, "wb") as f:
                f.write(data)
            self._log(f"Saved latest rules.json to {dest}")
            self._load_vendors()
        except urllib.error.URLError as e:
            messagebox.showerror("Download failed", f"Could not reach GitHub:\n{e}")
        except json.JSONDecodeError as e:
            messagebox.showerror("Download failed", f"Downloaded file wasn't valid JSON, existing rules.json was left untouched:\n{e}")
        except Exception as e:
            messagebox.showerror("Download failed", str(e))

    # -- Source management ------------------------------------------------

    def _add_folder(self):
        vendor = self.vendor_combo.get()
        if not vendor:
            messagebox.showwarning("Pick a vendor", "Select a vendor before adding a source.")
            return
        path = filedialog.askdirectory(title=f"Select log folder for {vendor}")
        if path:
            label = self.device_label.get().strip()
            self.sources.append((vendor, path, label))
            shown_label = label if label else "(auto - folder name, or subfolder names if multiple devices)"
            self.source_list.insert("end", f"{vendor}  |  {path}  |  label: {shown_label}")
            self.device_label.set("")

    def _add_zip(self):
        vendor = self.vendor_combo.get()
        if not vendor:
            messagebox.showwarning("Pick a vendor", "Select a vendor before adding a source.")
            return
        path = filedialog.askopenfilename(title=f"Select ZIP for {vendor}", filetypes=[("ZIP files", "*.zip")])
        if path:
            label = self.device_label.get().strip()
            self.sources.append((vendor, path, label))
            shown_label = label if label else "(auto - zip name, or subfolder names if multiple devices)"
            self.source_list.insert("end", f"{vendor}  |  {path}  |  label: {shown_label}")
            self.device_label.set("")

    def _remove_source(self):
        sel = self.source_list.curselection()
        if not sel:
            return
        idx = sel[0]
        self.source_list.delete(idx)
        del self.sources[idx]

    def _browse_output(self):
        path = filedialog.askdirectory(title="Select output folder")
        if path:
            self.output_folder.set(path)

    # -- Logging / threading ------------------------------------------------

    def _log(self, message):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", str(message) + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _poll_queue(self):
        try:
            while True:
                message = self.msg_queue.get_nowait()
                if message == "__DONE_PARSE__":
                    self._set_running(False)
                    messagebox.showinfo("Parse complete", "Log parsing finished. See the progress log for details.")
                elif message == "__DONE_SESSIONIZE__":
                    self._set_running(False)
                    self.open_csv_btn.configure(state="normal")
                    messagebox.showinfo("Sessionizing complete", "Sessionizing finished. Click 'Open Device Summary' to view results.")
                elif isinstance(message, tuple) and message[0] == "__ERROR__":
                    self._set_running(False)
                    messagebox.showerror("Error", message[1])
                else:
                    self._log(message)
        except queue.Empty:
            pass
        self.root.after(100, self._poll_queue)

    def _set_running(self, running):
        self.task_running = running
        state = "disabled" if running else "normal"
        self.parse_btn.configure(state=state)
        self.sessionize_btn.configure(state=state)

    # -- Pipeline actions ------------------------------------------------

    def _run_parse(self):
        if self.task_running:
            return
        if not self.sources:
            messagebox.showwarning("No sources", "Add at least one log source before parsing.")
            return

        self._set_running(True)
        self._log("=== Starting parse ===")
        threading.Thread(target=self._parse_worker, daemon=True).start()

    def _parse_worker(self):
        staging_root = tempfile.mkdtemp(prefix="ghosttracker_logs_")
        try:
            def progress(msg):
                self.msg_queue.put(msg)

            progress("Staging selected sources...")
            stage_sources(self.sources, staging_root, progress)

            parsed_dir = os.path.join(self.output_folder.get(), "parsed_events")
            os.makedirs(parsed_dir, exist_ok=True)

            run_parser(
                log_root=staging_root,
                output_root=parsed_dir,
                rules_path=self.rules_path.get(),
                progress=progress,
            )
            progress("=== Parse complete ===")
            self.msg_queue.put("__DONE_PARSE__")
        except Exception as e:
            self.msg_queue.put(("__ERROR__", f"Parsing failed: {e}"))
        finally:
            # Never leave raw log copies (potential PHI) sitting on disk longer than needed.
            shutil.rmtree(staging_root, ignore_errors=True)

    def _run_sessionize(self):
        if self.task_running:
            return

        try:
            gap = float(self.session_gap.get())
            window = int(self.window_days.get())
        except ValueError:
            messagebox.showerror("Invalid parameters", "Idle gap and window size must be numbers.")
            return

        date_start = self.date_start.get().strip() or None
        date_end = self.date_end.get().strip() or None
        if not self.use_date_range.get():
            date_start = date_end = None

        parsed_dir = os.path.join(self.output_folder.get(), "parsed_events")
        if not os.path.isdir(parsed_dir):
            messagebox.showwarning("Nothing parsed yet", "Run 'Parse Logs' first - no parsed events found in the output folder.")
            return

        self._set_running(True)
        self._log("=== Starting sessionizing ===")
        threading.Thread(
            target=self._sessionize_worker,
            args=(parsed_dir, gap, window, date_start, date_end),
            daemon=True,
        ).start()

    def _sessionize_worker(self, parsed_dir, gap, window, date_start, date_end):
        try:
            def progress(msg):
                self.msg_queue.put(msg)

            rows, csv_path = run_sessionizer(
                input_root=parsed_dir,
                output_root=self.output_folder.get(),
                rules_path=self.rules_path.get(),
                session_gap_minutes=gap,
                window_days=window,
                date_start=date_start,
                date_end=date_end,
                progress=progress,
            )
            self.last_csv_path = csv_path
            progress("=== Sessionizing complete ===")
            self.msg_queue.put("__DONE_SESSIONIZE__")
        except Exception as e:
            self.msg_queue.put(("__ERROR__", f"Sessionizing failed: {e}"))

    def _open_csv(self):
        path = self.last_csv_path or os.path.join(self.output_folder.get(), "device_summary.csv")
        if not os.path.isfile(path):
            messagebox.showwarning("Not found", f"No device_summary.csv found at:\n{path}")
            return
        system = platform.system()
        try:
            if system == "Windows":
                os.startfile(path)
            elif system == "Darwin":
                subprocess.run(["open", path])
            else:
                subprocess.run(["xdg-open", path])
        except Exception as e:
            messagebox.showerror("Could not open file", str(e))


if __name__ == "__main__":
    root = tk.Tk()
    app = GhostTrackerApp(root)
    root.mainloop()
