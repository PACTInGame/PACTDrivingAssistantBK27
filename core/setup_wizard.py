"""
Setup Wizard for LFS Assistant Add-on.
Guides the user through first-time configuration of LFS (cfg.txt, InSim autostart).
"""

import os
import re
import threading
import tkinter as tk
from tkinter import filedialog, messagebox

import psutil

# --- Constants ---

FIRST_RUN_FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '.setup_done')

DEFAULT_LFS_CFG_PATH = r"C:\LFS\cfg.txt"

REQUIRED_CFG_SETTINGS = {
    "OutSim Mode": "2",
    "OutSim Delay": "1",
    "OutSim IP": "127.0.0.1",
    "OutSim Port": "29998",
    "OutSim ID": "0",
    "OutSim Opts": "1ff",
    "OutGauge Mode": "2",
    "OutGauge Delay": "1",
    "OutGauge IP": "127.0.0.1",
    "OutGauge Port": "30000",
    "OutGauge ID": "0",
}

INSIM_AUTOEXEC_LINE = "/insim 29999"


def is_first_run() -> bool:
    """Check whether the setup has already been completed."""
    flag = os.path.normpath(FIRST_RUN_FLAG_FILE)
    if not os.path.isfile(flag):
        return True
    try:
        with open(flag, 'r', encoding='utf-8') as f:
            return f.read().strip().lower() != 'true'
    except OSError:
        return True


def mark_setup_done():
    """Write the flag file so the wizard is not shown again."""
    flag = os.path.normpath(FIRST_RUN_FLAG_FILE)
    os.makedirs(os.path.dirname(flag), exist_ok=True)
    with open(flag, 'w', encoding='utf-8') as f:
        f.write('true')


def _is_lfs_running() -> bool:
    for proc in psutil.process_iter(['name']):
        try:
            if proc.info['name'] and proc.info['name'].lower() == 'lfs.exe':
                return True
        except (psutil.AccessDenied, psutil.NoSuchProcess):
            continue
    return False


# ---------------------------------------------------------------------------
# cfg.txt helpers
# ---------------------------------------------------------------------------

def _read_cfg(path: str) -> list[str]:
    with open(path, 'r', encoding='utf-8') as f:
        return f.readlines()


def _write_cfg(path: str, lines: list[str]):
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


def apply_cfg_settings(cfg_path: str):
    """
    Modify cfg.txt so that all REQUIRED_CFG_SETTINGS are present with the
    correct values. Existing lines are updated in-place; missing keys are
    appended at the end.
    """
    lines = _read_cfg(cfg_path)
    remaining_keys = dict(REQUIRED_CFG_SETTINGS)  # keys still to handle

    new_lines: list[str] = []
    for line in lines:
        stripped = line.rstrip('\n').rstrip('\r')
        matched = False
        for key in list(remaining_keys.keys()):
            # Match lines like "OutSim Mode 2" — key at start, then whitespace, then value
            pattern = rf'^{re.escape(key)}\s+'
            if re.match(pattern, stripped):
                new_lines.append(f"{key} {remaining_keys[key]}\n")
                del remaining_keys[key]
                matched = True
                break
        if not matched:
            new_lines.append(line if line.endswith('\n') else line + '\n')

    # Append any settings that were not already present
    for key, value in remaining_keys.items():
        new_lines.append(f"{key} {value}\n")

    _write_cfg(cfg_path, new_lines)


def add_insim_autoexec(lfs_dir: str):
    # TODO also add /exec to automatically start the assistant on lfs launch!
    """Append '/insim 29999' to data/script/autoexec.lfs if not already present."""
    autoexec_path = os.path.join(lfs_dir, 'data', 'script', 'autoexec.lfs')
    os.makedirs(os.path.dirname(autoexec_path), exist_ok=True)

    existing = ""
    if os.path.isfile(autoexec_path):
        with open(autoexec_path, 'r', encoding='utf-8') as f:
            existing = f.read()

    if INSIM_AUTOEXEC_LINE in existing:
        return  # already present

    with open(autoexec_path, 'a', encoding='utf-8') as f:
        # Ensure we start on a new line
        if existing and not existing.endswith('\n'):
            f.write('\n')
        f.write(INSIM_AUTOEXEC_LINE + '\n')


# ---------------------------------------------------------------------------
# Tkinter Setup Wizard
# ---------------------------------------------------------------------------

class SetupWizard:
    """A simple, step-by-step Tkinter wizard for first-time LFS configuration."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title("LFS Assistant – First-Time Setup")
        self.root.geometry("520x260")
        self.root.resizable(False, False)

        self.cfg_path: str | None = None
        self.lfs_dir: str | None = None  # parent of cfg.txt
        self._poll_id: str | None = None

        # Main status label
        self.status_label = tk.Label(
            self.root, text="Checking your LFS configuration…",
            font=("Segoe UI", 12), wraplength=480, justify="center"
        )
        self.status_label.pack(pady=(30, 10))

        # Secondary / instruction label
        self.info_label = tk.Label(
            self.root, text="", font=("Segoe UI", 10),
            wraplength=480, justify="center", fg="#555"
        )
        self.info_label.pack(pady=(0, 10))

        # Path display
        self.path_var = tk.StringVar()
        self.path_label = tk.Label(
            self.root, textvariable=self.path_var,
            font=("Consolas", 9), fg="#336699"
        )
        self.path_label.pack(pady=(0, 5))

        # Button area (dynamic)
        self.button_frame = tk.Frame(self.root)
        self.button_frame.pack(pady=(10, 20))

        # Kick off the wizard flow
        self.root.after(300, self._step_check_lfs_running)

    # --- Steps ---

    def _step_check_lfs_running(self):
        """Step: Make sure LFS is not running (cfg.txt must not be locked)."""
        if _is_lfs_running():
            self.status_label.config(
                text="LFS is currently running."
            )
            self.info_label.config(
                text="You need to close LFS to run this setup.\nWaiting for you to close LFS…"
            )
            self._poll_lfs_closed()
        else:
            self._step_find_cfg()

    def _poll_lfs_closed(self):
        """Poll every 2 s until LFS is no longer running."""
        if _is_lfs_running():
            self._poll_id = self.root.after(2000, self._poll_lfs_closed)
        else:
            self._poll_id = None
            self.info_label.config(text="")
            self._step_find_cfg()

    def _step_find_cfg(self):
        """Step: Locate cfg.txt automatically or let the user browse."""
        self.status_label.config(text="Looking for LFS installation…")
        self.info_label.config(text="")
        self._clear_buttons()

        if os.path.isfile(DEFAULT_LFS_CFG_PATH):
            self.cfg_path = DEFAULT_LFS_CFG_PATH
            self.lfs_dir = os.path.dirname(DEFAULT_LFS_CFG_PATH)
            self.status_label.config(text="LFS installation found!")
            self.path_var.set(self.cfg_path)
            self.root.after(400, self._step_show_apply_button)
        else:
            self.status_label.config(text="LFS installation not found.")
            self.info_label.config(text="Please select your cfg.txt manually.")
            btn = tk.Button(
                self.button_frame, text="Browse…", width=18,
                command=self._browse_cfg
            )
            btn.pack()

    def _browse_cfg(self):
        path = filedialog.askopenfilename(
            title="Select cfg.txt",
            filetypes=[("LFS Config", "cfg.txt"), ("All files", "*.*")]
        )
        if path and os.path.isfile(path):
            self.cfg_path = path
            self.lfs_dir = os.path.dirname(path)
            self.path_var.set(self.cfg_path)
            self.status_label.config(text="LFS installation found!")
            self.info_label.config(text="")
            self._clear_buttons()
            self.root.after(400, self._step_show_apply_button)

    def _step_show_apply_button(self):
        """Step: Show button to check/apply cfg.txt changes."""
        self._clear_buttons()
        self.info_label.config(text="")
        btn = tk.Button(
            self.button_frame, text="Check and change cfg.txt", width=26,
            command=self._confirm_apply_cfg
        )
        btn.pack()

    def _confirm_apply_cfg(self):
        """Ask for confirmation, then apply."""
        proceed = messagebox.askyesno(
            "Confirm",
            "Changing OutGauge and OutSim configuration in cfg.txt now. Proceed?"
        )
        if proceed:
            try:
                apply_cfg_settings(self.cfg_path)
                self._step_ask_insim_autostart()
            except Exception as e:
                messagebox.showerror("Error", f"Failed to update cfg.txt:\n{e}")

    def _step_ask_insim_autostart(self):
        """Step: Ask whether to auto-enable InSim on LFS start."""
        self._clear_buttons()
        self.status_label.config(text="cfg.txt updated successfully!")
        self.path_var.set("")

        result = messagebox.askyesno(
            "Auto-enable InSim",
            "Do you want to automatically enable InSim when you start LFS?\n\n"
            "If you select 'No', you will need to do that manually every time "
            "using '/insim 29999'."
        )
        if result:
            try:
                add_insim_autoexec(self.lfs_dir)
            except Exception as e:
                messagebox.showerror("Error", f"Failed to update autoexec.lfs:\n{e}")
        self._step_done()

    def _step_done(self):
        """Final step: inform user and let them close the window."""
        self._clear_buttons()
        self.status_label.config(text="Alright, you are all set!")
        self.info_label.config(text="Close this window to start the LFS Assistant.")
        btn = tk.Button(
            self.button_frame, text="Close", width=14,
            command=self.root.destroy
        )
        btn.pack()
        mark_setup_done()

    # --- Helpers ---

    def _clear_buttons(self):
        for widget in self.button_frame.winfo_children():
            widget.destroy()

    def run(self):
        """Block until the wizard window is closed."""
        self.root.mainloop()


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def run_setup_if_needed() -> bool:
    """
    If this is the first run, show the setup wizard (blocking).
    Returns True if setup was performed, False if it was skipped.
    """
    if not is_first_run():
        return False

    wizard = SetupWizard()
    wizard.run()
    return True