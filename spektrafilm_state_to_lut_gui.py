#!/usr/bin/env python3
"""
Small GUI wrapper for spektrafilm_state_to_lut.py.

Place this file beside:
  - spektrafilm_state_to_lut.py
  - spektrafilm_mklut.py

Run from the same conda/miniforge environment that can import Spektrafilm:
  python spektrafilm_state_to_lut_gui.py
"""

from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_NAME = "Spektrafilm State LUT Generator"


def app_folder() -> Path:
    try:
        return Path(__file__).resolve().parent
    except Exception:
        return Path.cwd()


APP_DATA_FOLDER = app_folder() / "App Data"
CONFIG_PATH = APP_DATA_FOLDER / "state_lut_gui_config.json"
DEFAULT_ENGINE = app_folder() / "spektrafilm_state_to_lut.py"
DEFAULT_OUTPUT_FOLDER = app_folder() / "Generated LUTs"


def maybe_enable_dnd(root: tk.Tk, callback):
    """Enable drag/drop if tkinterdnd2 is installed. Otherwise return False."""
    try:
        from tkinterdnd2 import DND_FILES  # type: ignore
        root.drop_target_register(DND_FILES)
        root.dnd_bind("<<Drop>>", callback)
        return True
    except Exception:
        return False


def clean_drop_path(raw: str) -> str:
    raw = raw.strip()
    if raw.startswith("{") and raw.endswith("}"):
        raw = raw[1:-1]
    # tkinterdnd2 can pass multiple files separated by spaces. For this app,
    # use the first JSON-looking file if possible.
    parts = []
    current = ""
    in_brace = False
    for ch in raw:
        if ch == "{":
            in_brace = True
            current = ""
        elif ch == "}":
            in_brace = False
            parts.append(current)
            current = ""
        elif ch == " " and not in_brace:
            if current:
                parts.append(current)
                current = ""
        else:
            current += ch
    if current:
        parts.append(current)
    for part in parts:
        if part.lower().endswith(".json"):
            return part
    return parts[0] if parts else raw


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_NAME)
        self.geometry("940x760")
        self.minsize(820, 640)

        self.log_queue: queue.Queue[str | tuple] = queue.Queue()
        self.worker: threading.Thread | None = None

        self.python_var = tk.StringVar(value=sys.executable)
        self.engine_var = tk.StringVar(value=str(DEFAULT_ENGINE))
        self.state_var = tk.StringVar(value=str(app_folder() / "gui_state.json"))
        self.outdir_var = tk.StringVar(value=str(DEFAULT_OUTPUT_FOLDER))
        self.filename_var = tk.StringVar(value="")

        self.format_var = tk.StringVar(value="clf")
        self.size_var = tk.StringVar(value="medium")
        self.cube_size_var = tk.StringVar(value="64")
        self.ocio_bakelut_var = tk.StringVar(value="ociobakelut")
        self.input_mode_var = tk.StringVar(value="aces-ap0")
        self.output_mode_var = tk.StringVar(value="lut-default")
        self.compressed_var = tk.BooleanVar(value=False)
        self.report_detail_var = tk.BooleanVar(value=True)
        self.open_folder_var = tk.BooleanVar(value=True)

        self.input_gain_var = tk.StringVar(value="0.0")
        self.black_offset_var = tk.StringVar(value="0.0")

        self._load_config()
        self._build_ui()
        self.dnd_enabled = maybe_enable_dnd(self, self._handle_drop)
        self._set_dnd_note()
        self.after(100, self._drain_log_queue)

    def _load_config(self):
        try:
            data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception:
            return
        for key, var in {
            "python": self.python_var,
            "engine": self.engine_var,
            "state": self.state_var,
            "outdir": self.outdir_var,
            "format": self.format_var,
            "size": self.size_var,
            "cube_size": self.cube_size_var,
            "ocio_bakelut": self.ocio_bakelut_var,
            "input_mode": self.input_mode_var,
            "output_mode": self.output_mode_var,
            "input_gain": self.input_gain_var,
            "black_offset": self.black_offset_var,
        }.items():
            if key in data:
                var.set(str(data[key]))
        for key, var in {
            "compressed": self.compressed_var,
            "report_detail": self.report_detail_var,
            "open_folder": self.open_folder_var,
        }.items():
            if key in data:
                var.set(bool(data[key]))

    def _save_config(self):
        APP_DATA_FOLDER.mkdir(parents=True, exist_ok=True)
        data = {
            "python": self.python_var.get(),
            "engine": self.engine_var.get(),
            "state": self.state_var.get(),
            "outdir": self.outdir_var.get(),
            "format": self.format_var.get(),
            "size": self.size_var.get(),
            "cube_size": self.cube_size_var.get(),
            "ocio_bakelut": self.ocio_bakelut_var.get(),
            "input_mode": self.input_mode_var.get(),
            "output_mode": self.output_mode_var.get(),
            "compressed": self.compressed_var.get(),
            "report_detail": self.report_detail_var.get(),
            "open_folder": self.open_folder_var.get(),
            "input_gain": self.input_gain_var.get(),
            "black_offset": self.black_offset_var.get(),
        }
        CONFIG_PATH.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _build_ui(self):
        root = ttk.Frame(self, padding=14)
        root.pack(fill="both", expand=True)

        ttk.Label(root, text=APP_NAME, font=("Helvetica", 18, "bold")).pack(anchor="w")
        self.dnd_note = ttk.Label(root, text="", foreground="#555")
        self.dnd_note.pack(anchor="w", pady=(4, 12))

        file_frame = ttk.LabelFrame(root, text="Files", padding=10)
        file_frame.pack(fill="x", pady=(0, 10))
        self._path_row(file_frame, "Spektrafilm gui_state.json", self.state_var, self._choose_state, 0)
        self._path_row(file_frame, "Output folder", self.outdir_var, self._choose_outdir, 1)
        self._path_row(file_frame, "Engine script", self.engine_var, self._choose_engine, 2)
        self._path_row(file_frame, "Python executable", self.python_var, self._choose_python, 3)
        self._path_row(file_frame, "OCIO ociobakelut", self.ocio_bakelut_var, self._choose_ocio_bakelut, 4)
        file_frame.columnconfigure(1, weight=1)

        opts = ttk.LabelFrame(root, text="LUT settings", padding=10)
        opts.pack(fill="x", pady=(0, 10))
        self._combo_row(opts, "Format", self.format_var, ["clf", "cube"], 0, 0)
        self._combo_row(opts, "CLF sample size", self.size_var, ["small", "medium", "large", "huge"], 0, 2)
        ttk.Label(opts, text="CUBE bake size").grid(row=1, column=0, sticky="w", pady=5)
        ttk.Entry(opts, textvariable=self.cube_size_var, width=12).grid(row=1, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(opts, text="Used only for CUBE. 64 is recommended.", foreground="#555").grid(row=1, column=2, columnspan=2, sticky="w", pady=5)
        self._combo_row(opts, "Input policy", self.input_mode_var, ["aces-ap0", "rec2020", "srgb", "prophoto", "state"], 2, 0)
        self._combo_row(opts, "Output policy", self.output_mode_var, ["lut-default", "state"], 2, 2)
        ttk.Checkbutton(opts, text="Compressed CLFZ", variable=self.compressed_var).grid(row=3, column=1, sticky="w", pady=5)
        ttk.Checkbutton(opts, text="Detailed report", variable=self.report_detail_var).grid(row=3, column=3, sticky="w", pady=5)
        ttk.Label(opts, text="Input gain EV").grid(row=4, column=0, sticky="w", pady=5)
        ttk.Entry(opts, textvariable=self.input_gain_var, width=12).grid(row=4, column=1, sticky="w", padx=8, pady=5)
        ttk.Label(opts, text="Output black offset").grid(row=4, column=2, sticky="w", pady=5)
        ttk.Entry(opts, textvariable=self.black_offset_var, width=12).grid(row=4, column=3, sticky="w", padx=8, pady=5)
        for c in range(4):
            opts.columnconfigure(c, weight=1)

        output = ttk.LabelFrame(root, text="Output filename", padding=10)
        output.pack(fill="x", pady=(0, 10))
        ttk.Label(output, text="Filename override").grid(row=0, column=0, sticky="w")
        ttk.Entry(output, textvariable=self.filename_var).grid(row=0, column=1, sticky="ew", padx=8)
        ttk.Label(output, text="Leave blank to let the engine create a name from the state.", foreground="#555").grid(row=1, column=1, sticky="w", pady=(4, 0))
        output.columnconfigure(1, weight=1)

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(0, 10))
        self.generate_btn = ttk.Button(buttons, text="Generate LUT", command=self._start_generate)
        self.generate_btn.pack(side="left")
        ttk.Button(buttons, text="Dry-run report", command=self._start_dry_run).pack(side="left", padx=8)
        ttk.Button(buttons, text="Open output folder", command=self._open_output_folder).pack(side="left")
        ttk.Checkbutton(buttons, text="Open folder after generate", variable=self.open_folder_var).pack(side="left", padx=14)

        log_frame = ttk.LabelFrame(root, text="Report / Log", padding=8)
        log_frame.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_frame, wrap="word", height=18)
        self.log_text.pack(fill="both", expand=True)

    def _set_dnd_note(self):
        if getattr(self, "dnd_enabled", False):
            self.dnd_note.configure(text="Drop a Spektrafilm gui_state.json onto this window, or select one below.")
        else:
            self.dnd_note.configure(text="Select a Spektrafilm gui_state.json below. Drag/drop is enabled automatically if tkinterdnd2 is installed.")

    def _path_row(self, parent, label, variable, command, row):
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=4)
        ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8, pady=4)
        ttk.Button(parent, text="Choose…", command=command).grid(row=row, column=2, sticky="e", pady=4)

    def _combo_row(self, parent, label, variable, values, row, col):
        ttk.Label(parent, text=label).grid(row=row, column=col, sticky="w", pady=5)
        combo = ttk.Combobox(parent, textvariable=variable, values=values, state="readonly", width=18)
        combo.grid(row=row, column=col + 1, sticky="w", padx=8, pady=5)
        return combo

    def _choose_state(self):
        path = filedialog.askopenfilename(title="Choose Spektrafilm gui_state.json", initialdir=str(app_folder()), filetypes=[("JSON files", "*.json"), ("All files", "*.*")])
        if path:
            self.state_var.set(path)

    def _choose_outdir(self):
        path = filedialog.askdirectory(title="Choose output folder", initialdir=self.outdir_var.get() or str(DEFAULT_OUTPUT_FOLDER))
        if path:
            self.outdir_var.set(path)

    def _choose_engine(self):
        path = filedialog.askopenfilename(title="Choose spektrafilm_state_to_lut.py", initialdir=str(app_folder()), filetypes=[("Python scripts", "*.py"), ("All files", "*.*")])
        if path:
            self.engine_var.set(path)

    def _choose_python(self):
        path = filedialog.askopenfilename(title="Choose Python executable", initialdir=str(Path(sys.executable).parent), filetypes=[("Python executable", "python*"), ("All files", "*.*")])
        if path:
            self.python_var.set(path)

    def _choose_ocio_bakelut(self):
        path = filedialog.askopenfilename(title="Choose ociobakelut executable", initialdir=str(Path(sys.executable).parent), filetypes=[("All files", "*.*")])
        if path:
            self.ocio_bakelut_var.set(path)

    def _handle_drop(self, event):
        path = clean_drop_path(event.data)
        if path:
            self.state_var.set(path)
            self._log(f"Selected state file from drop: {path}\n")

    def _validate(self) -> bool:
        issues = []
        for label, value in [
            ("Python executable", self.python_var.get()),
            ("Engine script", self.engine_var.get()),
            ("State file", self.state_var.get()),
        ]:
            if not Path(value).expanduser().exists():
                issues.append(f"{label} not found:\n{value}")
        try:
            float(self.input_gain_var.get())
            float(self.black_offset_var.get())
            cube_size = int(self.cube_size_var.get())
            if cube_size < 2:
                raise ValueError("CUBE bake size must be at least 2")
        except Exception:
            issues.append("Input gain/output black offset must be numbers, and CUBE bake size must be an integer >= 2.")
        if self.compressed_var.get() and self.format_var.get() != "clf":
            issues.append("Compressed CLFZ only applies when Format is clf.")
        if issues:
            messagebox.showerror("Setup issue", "\n\n".join(issues))
            return False
        return True

    def _build_command(self, dry_run: bool) -> list[str]:
        cmd = [
            self.python_var.get(),
            self.engine_var.get(),
            "--state", self.state_var.get(),
            "--format", self.format_var.get(),
            "--size", self.size_var.get(),
            "--cube-size", self.cube_size_var.get(),
            "--ocio-bakelut", self.ocio_bakelut_var.get(),
            "--input-mode", self.input_mode_var.get(),
            "--output-mode", self.output_mode_var.get(),
            "--input-gain", self.input_gain_var.get(),
            "--output-black-offset", self.black_offset_var.get(),
            "--outdir", self.outdir_var.get(),
        ]
        filename = self.filename_var.get().strip()
        if filename:
            outdir = Path(self.outdir_var.get()).expanduser()
            cmd += ["-o", str(outdir / filename)]
        if self.compressed_var.get():
            cmd.append("--compressed")
        if self.report_detail_var.get():
            cmd.append("--report-detail")
        if dry_run:
            cmd.append("--dry-run-report")
        return cmd

    def _start_generate(self):
        self._start(dry_run=False)

    def _start_dry_run(self):
        self._start(dry_run=True)

    def _start(self, dry_run: bool):
        if self.worker and self.worker.is_alive():
            messagebox.showinfo("Running", "A command is already running.")
            return
        if not self._validate():
            return
        try:
            self._save_config()
        except Exception as exc:
            self._log(f"Could not save config: {exc}\n")
        Path(self.outdir_var.get()).expanduser().mkdir(parents=True, exist_ok=True)
        cmd = self._build_command(dry_run=dry_run)
        self.generate_btn.configure(state="disabled")
        self._log("\n" + "Dry-run report" if dry_run else "\nGenerating LUT")
        self._log("\n" + " ".join(f'\"{x}\"' if " " in x else x for x in cmd) + "\n")
        self.worker = threading.Thread(target=self._run_command, args=(cmd, dry_run), daemon=True)
        self.worker.start()

    def _run_command(self, cmd: list[str], dry_run: bool):
        try:
            proc = subprocess.run(cmd, text=True, capture_output=True, cwd=str(Path(self.engine_var.get()).expanduser().parent))
            if proc.stdout:
                self.log_queue.put(proc.stdout)
            if proc.stderr:
                self.log_queue.put(proc.stderr)
            if proc.returncode == 0:
                self.log_queue.put("\nFinished successfully.\n")
                if not dry_run and self.open_folder_var.get():
                    self.log_queue.put(("OPEN", self.outdir_var.get()))
            else:
                self.log_queue.put(f"\nCommand failed with exit code {proc.returncode}.\n")
        except Exception as exc:
            self.log_queue.put(f"\nError: {exc}\n")
        finally:
            self.log_queue.put(("ENABLE",))

    def _drain_log_queue(self):
        try:
            while True:
                item = self.log_queue.get_nowait()
                if isinstance(item, tuple) and item and item[0] == "ENABLE":
                    self.generate_btn.configure(state="normal")
                elif isinstance(item, tuple) and item and item[0] == "OPEN":
                    self._open_path(Path(item[1]).expanduser())
                else:
                    self._log(str(item))
        except queue.Empty:
            pass
        self.after(100, self._drain_log_queue)

    def _log(self, text: str):
        self.log_text.insert("end", text)
        if not text.endswith("\n"):
            self.log_text.insert("end", "\n")
        self.log_text.see("end")

    def _open_output_folder(self):
        self._open_path(Path(self.outdir_var.get()).expanduser())

    def _open_path(self, path: Path):
        try:
            subprocess.run(["open", str(path)], check=False)
        except Exception as exc:
            self._log(f"Could not open {path}: {exc}\n")


if __name__ == "__main__":
    app = App()
    app.mainloop()
