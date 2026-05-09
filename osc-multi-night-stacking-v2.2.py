#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, 
# or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
# See <https://www.gnu.org/licenses/>.
"""
Multi-Night Stacking for Siril 1.4 (PyQt6) — with Siril console integration (sirilpy) - version 2.1

What's inside:
- JSON projects (persist drizzle options + 2-pass flag); new project starts with one session.
- Prepare Working Directory (symlink→hardlink→copy) with logs + Siril console progress.
- Master Library or per-session master overrides (bias/dark/flat); OSC-first pipeline.
- Drizzle workflow per Siril 1.4 docs:
    * Drizzle ON: NO -debayer during calibrate; register (-layer=0 [+ -2pass]); seqapplyreg (-scale, -drizzle, -pixfrac, -kernel); stack r_*.
    * Drizzle OFF: include -debayer in calibrate; register (-layer=0 [+ -2pass]); stack r_*.
- Global stack options (rej / wrej / mean + sigma low/high), sigma controls hide for Mean.
- SSF: requires 1.4.0, setcompress 0, setfindstar reset (start & end), final close.
- Final save to <project_slug>_final.fit and auto-open in Siril.
- Remove Session (config+name+data) and Remove Data (All Sessions) (data only).
- Guarded session switching to prevent file list cross-contamination.
- NEW: Abort Run (graceful stop of siril-cli).
- Compression support
- 32-bit output
- Pack Sequence feature for > 2048 open files
- Mosaic feature
"""
from __future__ import annotations

import json, os, platform, re, shutil, subprocess, sys, signal, time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from PyQt6 import QtCore, QtGui, QtWidgets

from collections import Counter
try:
    from astropy.io import fits
except Exception:
    fits = None  # We'll skip preflight if astropy isn't available

from pathlib import Path

# ---------- Quick Start (embedded markdown + dialog) ----------
QUICK_START_MD = r"""
### Setup Steps

1. **Create or Load a Project**
   - Click **New Project** → choose your target root folder
   - Check or uncheck features and options
   - Add Sessions and Panels (for mosaic) as needed
   - Add LIGHTS, FLATS, DARKS, BIAS frames to Sessions or Panels as needed
   - The app auto-detects all session folders (Session 1, Session 2, …) if they were previously created.  
   - Check or uncheck features and options as needed
2. **Prepare Working Directory (Symlink/copy Files)**
   - Inside each `Session X` folder.    
   - **Why:** Siril reads and writes intermediate calibrated and registered FITS frames inside this directory.  
   - Each session will therefore have:

        ```
        Session 1/process/
        Session 2/process/
       ...
       ```
3. **Generate Siril Script**  
   - Confirm features and option settings.
   - Click **Build Siril Script**
   - The application writes its `.ssf` script file into the *working directory*   

4. **Run Siril Script**

   - Click the **Run Siril Script** button in the main window.  
   - The application automatically executes the configured **`run_project.ssf`** Siril script via the Siril Python API or uses the **`siril-cli`** as a backup
   - Progress and script output appear in the console log panel in Siril and in a log file in the *working directory*
   - When finished, your combined stack is saved automatically as **`[ProjectName]_final.fit`** and loaded into Siril
"""

class QuickStartDialog(QtWidgets.QDialog):
    """Dismissible, laptop-friendly dialog that renders the embedded Markdown."""
    def __init__(self, markdown_text: str, parent=None):
        super().__init__(parent)
        font = self.font()
        font.setPointSizeF(font.pointSizeF() * 1.2)  # 1.2x → ~20 % larger text
        self.setFont(font)
        self.setWindowTitle("Quick Start Instructions")
        self.setModal(True)
        self.setSizeGripEnabled(True)

        # Size tuned for 1080p; scrollable for smaller screens
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        w = min(1000, int(avail.width() * 0.75))
        h = min(720,  int(avail.height() * 0.72))
        self.resize(max(720, w), max(540, h))

        layout = QtWidgets.QVBoxLayout(self)

        self.viewer = QtWidgets.QTextBrowser(self)
        self.viewer.setOpenExternalLinks(True)
        self.viewer.setReadOnly(True)
        # Slightly larger headings and comfy spacing
        self.viewer.document().setDefaultStyleSheet("""
            h1,h2,h3 { margin: 0.4em 0 0.3em; }
            p, li { line-height: 1.35; }
            ul, ol { margin: 0.3em 0 0.8em 1.2em; }
            code, pre { font-family: Consolas, 'Courier New', monospace; }
        """)
        # Use Markdown if available (Qt 6 supports it)
        try:
            self.viewer.setMarkdown(markdown_text)
        except Exception:
            self.viewer.setPlainText(markdown_text)
        layout.addWidget(self.viewer, 1)

        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch(1)
        close_btn = QtWidgets.QPushButton("Close")
        close_btn.setDefault(True)
        close_btn.clicked.connect(self.accept)
        btn_row.addWidget(close_btn)
        layout.addLayout(btn_row)

        # Keyboard shortcuts
        QtGui.QShortcut(QtGui.QKeySequence("Escape"), self, activated=self.reject)

def to_path_or_none(s):
    """Return Path(s) if s is a non-empty string, else None."""
    return Path(s) if s else None

def _resolve_cal_paths(project, sess, panel=None):
    """
    Decide which calibration frames (bias, dark-flat, dark, flat) to use
    for a given session/panel combination.
    Returns (md, mf, mb, mdf) as Optional[Path].
    Priority: panel override → session override → master library → None.
    """
    def P(x):
        """Return Path(x) if non-empty string/path-like, else None."""
        return Path(x) if x else None

    use_lib = bool(getattr(project, "use_master_library", False))

    # Session-level overrides
    s_dark     = getattr(sess, "master_dark", None)
    s_flat     = getattr(sess, "master_flat", None)
    s_bias     = getattr(sess, "master_bias", None)
    # accept both spellings
    s_darkflat = getattr(sess, "master_dark_flat", None)
    if not s_darkflat:
        s_darkflat = getattr(sess, "master_darkflat", None)

    # Panel-level overrides (if mosaic)
    p_dark = p_flat = p_bias = p_darkflat = None
    if panel is not None:
        p_dark     = getattr(panel, "master_dark", None)
        p_flat     = getattr(panel, "master_flat", None)
        p_bias     = getattr(panel, "master_bias", None)
        p_darkflat = getattr(panel, "master_darkflat", None)

    # Library masters
    lib_dark      = getattr(project, "lib_master_dark", None)
    lib_flat      = getattr(project, "lib_master_flat", None)
    lib_bias      = getattr(project, "lib_master_bias", None)
    lib_darkflat  = getattr(project, "lib_master_darkflat", None)

    def choose(one, two, three):
        # order: panel → session → library (only if library use enabled)
        if one: return P(one)
        if two: return P(two)
        if use_lib and three: return P(three)
        return None

    md  = choose(p_dark,     s_dark,     lib_dark)
    mf  = choose(p_flat,     s_flat,     lib_flat)
    mb  = choose(p_bias,     s_bias,     lib_bias)
    mdf = choose(p_darkflat, s_darkflat, lib_darkflat)

    return md, mf, mb, mdf

def _warn(L, msg: str):
    L.append(f"# WARN: {msg}")

def modal_geometry(filepaths):
    sizes = []
    for fp in filepaths:
        try:
            with fits.open(fp, memmap=False) as hdul:
                h = hdul[0].header
                sizes.append((int(h.get("NAXIS1", 0)), int(h.get("NAXIS2", 0))))
        except Exception:
            continue
    if not sizes:
        return None, Counter()
    c = Counter(sizes)
    return c.most_common(1)[0][0], c

def preflight_geometry(lights_dir: Path, process_dir: Path,
                       master_dark: Optional[Path], master_flat: Optional[Path],
                       logger=print):
    """Move non-matching geometry lights out of the way and warn on master mismatches."""
    if fits is None:
        logger("Geometry preflight skipped (astropy not available).")
        return

    # We convert with -out=../process, so check in process_dir
    light_files = sorted((process_dir).glob("light_*.fit"))
    if not light_files:
        logger("No converted lights found for geometry preflight.")
        return

    (W, H), counts = modal_geometry(light_files)
    if W is None:
        logger("Could not read geometry from lights.")
        return

    # Park outliers
    reject_dir = process_dir / "_mismatch_geometry"
    moved = 0
    for fp in light_files:
        try:
            with fits.open(fp, memmap=False) as hdul:
                w = int(hdul[0].header.get("NAXIS1", 0))
                h = int(hdul[0].header.get("NAXIS2", 0))
            if (w, h) != (W, H):
                reject_dir.mkdir(exist_ok=True)
                fp.rename(reject_dir / fp.name)
                moved += 1
        except Exception:
            continue

    if moved:
        logger(f"[preflight] Moved {moved} outlier light(s) to {reject_dir} "
               f"(kept modal geometry {W}x{H}; counts={dict(counts)})")
    else:
        logger(f"[preflight] All lights share geometry {W}x{H} (counts={dict(counts)})")

    # Check masters vs modal geometry
    def geom_of(fp: Optional[Path]):
        if not fp or not fp.exists():
            return None
        try:
            with fits.open(fp, memmap=False) as hdul:
                return (int(hdul[0].header.get("NAXIS1", 0)),
                        int(hdul[0].header.get("NAXIS2", 0)))
        except Exception:
            return None

    md_g = geom_of(master_dark)
    mf_g = geom_of(master_flat)
    if md_g and md_g != (W, H):
        logger(f"[preflight] WARNING: master dark {master_dark.name} is {md_g[0]}x{md_g[1]} "
               f"but lights are {W}x{H}")
    if mf_g and mf_g != (W, H):
        logger(f"[preflight] WARNING: master flat {master_flat.name} is {mf_g[0]}x{mf_g[1]} "
               f"but lights are {W}x{H}")

# Optional: Siril Python API
try:
    import sirilpy as s
except Exception:
    s = None

FRAME_TYPES = ["lights", "bias", "darks", "flats", "dark_flats"]

# =========================
# New: Panel dataclass
# =========================
@dataclass
class Panel:
    panel_id: str = "A1"
    description: str = ""
    lights: List[str] = field(default_factory=list)
    bias: List[str] = field(default_factory=list)
    darks: List[str] = field(default_factory=list)
    flats: List[str] = field(default_factory=list)
    dark_flats: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "Panel":
        return Panel(**d)

# -----------------------------
# Data Models
# -----------------------------

@dataclass
class Session:
    name: str
    lights: List[str] = field(default_factory=list)
    bias: List[str] = field(default_factory=list)
    darks: List[str] = field(default_factory=list)
    flats: List[str] = field(default_factory=list)
    dark_flats: List[str] = field(default_factory=list)

    master_bias: Optional[str] = None
    master_dark: Optional[str] = None
    master_flat: Optional[str] = None
    master_dark_flat: Optional[str] = None

    work_subdir: Optional[str] = None

    # Panels belonging to this session
    panels: List[Panel] = field(default_factory=list)

    def to_dict(self) -> Dict:
        d = asdict(self)
        d["panels"] = [p.to_dict() for p in self.panels]
        return d

    @staticmethod
    def from_dict(d: Dict) -> "Session":
        panels = [Panel.from_dict(x) for x in d.get("panels", [])]
        d2 = dict(d)
        d2["panels"] = panels
        return Session(**d2)

@dataclass
class Project:

    name: str = "Untitled Project"
    project_file: Optional[str] = None
    working_dir: Optional[str] = None

    use_master_library: bool = True
    siril_cli_path: Optional[str] = None
    force_cli: bool = False

    # UI prefs
    remember_window_size: bool = False
    window_w: Optional[int] = None
    window_h: Optional[int] = None

    # Image metadata (inferred)
    frame_width: int = 0
    frame_height: int = 0

    # Drizzle (global)
    drizzle_enabled: bool = False
    drizzle_scaling: float = 1.0      # 0.1 – 3.0
    drizzle_pixfrac: float = 1.0      # 0.0 – 1.0
    drizzle_kernel: str = "square"    # point|turbo|square|gaussian|lanczos2|lanczos3

    # 2-pass registration (default False)
    two_pass: bool = False
    compress_intermediates: bool = False  # lossless FITS tile compression for intermediates
    # 32-bit output for final light stack
    stack_32bit: bool = False

    # Global stacking options
    stack_method: str = "rej"     # "rej", "wrej", "mean"
    reject_sigma_low: float = 3.0
    reject_sigma_high: float = 3.0

    # Pack sequences
    pack_sequences_mode: str = "off"  # off | fitseq | ser | auto
    pack_threshold: int = 2000        # used only when mode == "auto"

    sessions: List[Session] = field(default_factory=list)

    # --- Mosaic (project-level) ---
    mosaic_enabled: bool = False
    mosaic_grid_rows: int = 1
    mosaic_grid_cols: int = 1
    mosaic_overlap_percent: int = 5

    # Reference & geometry
    mosaic_global_reference: Optional[str] = None   # e.g. "Session 1 / Frame 45" or a path
    mosaic_canvas_scale: float = 1.0                # 0.25–4.0
    mosaic_registration_mode: str = "Two-pass"      # "Two-pass" | "One-pass"

    # Mosaic-stage stacking method (independent of per-panel method)
    mosaic_stack_method: str = "mean"               # "mean" | "wrej" | "rej" | "median"

    # Normalization / blending
    panel_background_extraction: bool = True
    mosaic_maximize_framing: bool = True          # apply max framing in Phase 2 (seqapplyreg) and stack -maximize
    mosaic_overlap_norm: bool = True              # normalize on overlaps during mosaic stacking
    mosaic_feather_px: int = 50
    link_feather_to_overlap: bool = False
    # Drizzle scope (mosaic): apply drizzle during per-panel registration (Phase 1)
    mosaic_drizzle_per_panel: bool = False

    # Panels: simple links of { "session": "Session 1", "panel_id": "A1", "description": "" }
    panels: List[Dict[str, str]] = field(default_factory=list)

    allow_uncalibrated: bool = False  # NEW: allow building/running with no cals (warn)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "project_file": self.project_file,
            "working_dir": self.working_dir,
            "use_master_library": self.use_master_library,
            "siril_cli_path": self.siril_cli_path,
            "force_cli": bool(getattr(self, "force_cli", False)),

            "drizzle_enabled": self.drizzle_enabled,
            "drizzle_scaling": self.drizzle_scaling,
            "drizzle_pixfrac": self.drizzle_pixfrac,
            "drizzle_kernel": self.drizzle_kernel,

            "two_pass": self.two_pass,
            "compress_intermediates": self.compress_intermediates,

            "stack_method": self.stack_method,
            "reject_sigma_low": self.reject_sigma_low,
            "reject_sigma_high": self.reject_sigma_high,
            "stack_32bit": self.stack_32bit,  # <-- add
            "pack_sequences_mode": self.pack_sequences_mode,
            "pack_threshold": int(self.pack_threshold),            
            "sessions": [s.to_dict() for s in self.sessions],
            # --- Mosaic ---
            "mosaic_enabled": self.mosaic_enabled,
            "mosaic_grid_rows": int(self.mosaic_grid_rows),
            "mosaic_grid_cols": int(self.mosaic_grid_cols),
            "mosaic_overlap_percent": int(self.mosaic_overlap_percent),

            "mosaic_global_reference": self.mosaic_global_reference,
            "mosaic_canvas_scale": float(self.mosaic_canvas_scale),
            "mosaic_registration_mode": self.mosaic_registration_mode,
            "mosaic_stack_method": self.mosaic_stack_method,

            "panel_background_extraction": self.panel_background_extraction,
            "mosaic_maximize_framing": self.mosaic_maximize_framing,
            "mosaic_overlap_norm": self.mosaic_overlap_norm,
            "mosaic_feather_px": int(self.mosaic_feather_px),
            "link_feather_to_overlap": bool(self.link_feather_to_overlap),

            "mosaic_drizzle_per_panel": self.mosaic_drizzle_per_panel,
            "_ui_mosaic_auto_grid": bool(getattr(self, "_ui_mosaic_auto_grid", False)),
            "_ui_name_scheme": int(getattr(self, "_ui_name_scheme", 0)),
            "_ui_grid_scope": int(getattr(self, "_ui_grid_scope", 0)),

            "panels": list(self.panels),

            "remember_window_size": bool(self.remember_window_size),
            "window_w": int(self.window_w) if self.window_w else None,
            "window_h": int(self.window_h) if self.window_h else None,
            "allow_uncalibrated": bool(self.allow_uncalibrated),  # NEW
        }

    @staticmethod
    def from_dict(d: Dict) -> "Project":
        p = Project()
        p.name = d.get("name", "Untitled Project")
        p.project_file = d.get("project_file")
        p.working_dir = d.get("working_dir")
        p.use_master_library = d.get("use_master_library", True)
        p.allow_uncalibrated = bool(d.get("allow_uncalibrated", False))  # NEW
        p.siril_cli_path = d.get("siril_cli_path")
        p.force_cli = bool(d.get("force_cli", False))

        p.drizzle_enabled = d.get("drizzle_enabled", False)
        p.drizzle_scaling = float(d.get("drizzle_scaling", 1.0))
        p.drizzle_pixfrac = float(d.get("drizzle_pixfrac", 1.0))
        p.drizzle_kernel = d.get("drizzle_kernel", "square")

        p.two_pass = bool(d.get("two_pass", True))
        p.compress_intermediates = bool(d.get("compress_intermediates", False))

        p.stack_method = d.get("stack_method", "rej")
        p.reject_sigma_low = float(d.get("reject_sigma_low", 3.0))
        p.reject_sigma_high = float(d.get("reject_sigma_high", 3.0))
        p.stack_32bit = bool(d.get("stack_32bit", False))  # <-- add
        p.pack_sequences_mode = (d.get("pack_sequences_mode") or "off").lower()
        p.pack_threshold = int(d.get("pack_threshold", 2000))
        p.sessions = [Session.from_dict(x) for x in d.get("sessions", [])]

        # --- Mosaic ---
        p.mosaic_enabled          = bool(d.get("mosaic_enabled", False))
        p.mosaic_grid_rows        = int(d.get("mosaic_grid_rows", 1))
        p.mosaic_grid_cols        = int(d.get("mosaic_grid_cols", 1))
        p.mosaic_overlap_percent  = int(d.get("mosaic_overlap_percent", 5))

        p.mosaic_global_reference = d.get("mosaic_global_reference") or None
        p.mosaic_canvas_scale     = float(d.get("mosaic_canvas_scale", 1.0))
        p.mosaic_registration_mode= d.get("mosaic_registration_mode", "Two-pass")
        p.mosaic_stack_method     = p.stack_method

        p.panel_background_extraction = bool(d.get("panel_background_extraction", False))
        p.mosaic_maximize_framing = bool(d.get("mosaic_maximize_framing", True))
        p.mosaic_overlap_norm     = bool(d.get("mosaic_overlap_norm", False))
        p.mosaic_feather_px       = int(d.get("mosaic_feather_px", 50))
        p.link_feather_to_overlap = bool(d.get("link_feather_to_overlap", False))

        p.mosaic_drizzle_per_panel= bool(d.get("mosaic_drizzle_per_panel", False))

        # UI-only state for Mosaic grid naming and auto-manage toggle
        p._ui_mosaic_auto_grid = bool(d.get("_ui_mosaic_auto_grid", False))
        p._ui_name_scheme = int(d.get("_ui_name_scheme", 0))
        p._ui_grid_scope = int(d.get("_ui_grid_scope", 0))

        p.panels = list(d.get("panels", []))

        p.remember_window_size = bool(d.get("remember_window_size", False))
        p.window_w = d.get("window_w")
        p.window_h = d.get("window_h")
        if p.window_w is not None: p.window_w = int(p.window_w)
        if p.window_h is not None: p.window_h = int(p.window_h)

        return p

# -----------------------------
# Utilities
# -----------------------------

def set_comp_if_needed(L, comp_state_ref, desired):
    """
    Keep Siril compression state consistent.
    comp_state_ref is an int you keep in your generator (0/1).
    """
    try:
        current = comp_state_ref[0]
    except TypeError:
        # allow passing an int by value (no mutation), we still emit correct setcompress
        current = comp_state_ref
    if current != desired:
        L.append(f"setcompress {int(desired)}")
        try:
            comp_state_ref[0] = desired
        except Exception:
            pass

def safe_slug(name: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", str(name or "project")).strip("_") or "project"

def siril_arg(p: str) -> str:
    """Return a Siril-friendly path (POSIX slashes), no quotes, keep extension."""
    return Path(p).as_posix()

def safe_link_or_copy(src: Path, dst: Path) -> Tuple[bool, str]:
    """Try symlink -> hardlink -> copy."""
    try:
        dst.parent.mkdir(parents=True, exist_ok=True)
        if dst.exists():
            return True, f"[prepare] Exists: {dst}"
        # Try symlink
        if platform.system() == "Windows":
            os.symlink(src, dst)  # may require admin/dev mode
        else:
            dst.symlink_to(src)
        return True, f"[prepare] Symlinked: {dst} -> {src}"
    except Exception:
        try:
            os.link(src, dst)
            return True, f"[prepare] Hardlinked: {dst} -> {src}"
        except Exception:
            try:
                shutil.copy2(src, dst)
                return True, f"[prepare] Copied: {dst} from {src}"
            except Exception as e_copy:
                return False, f"[prepare] Failed: {src} -> {dst}: {e_copy}"

def find_siril_cli(explicit: Optional[str]) -> Optional[str]:
    if explicit and Path(explicit).exists():
        return explicit
    from shutil import which
    for c in ("siril-cli", "siril-cli.exe", "siril", "siril.exe"):
        p = which(c)
        if p: return p
    if platform.system() == "Windows":
        for p in (r"C:\Program Files\Siril\bin\siril-cli.exe",
                  r"C:\Program Files\Siril\siril-cli.exe",
                  r"C:\Program Files (x86)\Siril\bin\siril-cli.exe",
                  r"C:\Program Files (x86)\Siril\siril-cli.exe"):
            if Path(p).exists(): return p
    return None

def get_siril_version(siril_path: str) -> Optional[Tuple[int, int, int]]:
    """
    Return (major, minor, patch) for siril-cli, or None on failure.

    This is used only for the drizzle version guard and for logging – it does
    NOT try to compare siril-cli against the Siril GUI version.
    """
    try:
        out = subprocess.check_output(
            [siril_path, "-v"],
            text=True,
            stderr=subprocess.STDOUT,
            timeout=10,
        )
    except Exception:
        return None

    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    if not m:
        return None

    return int(m.group(1)), int(m.group(2)), int(m.group(3))

def _read_fits_size_quick(path: str) -> tuple[int, int] | tuple[None, None]:
    """Read NAXIS1/NAXIS2 from a FITS file header without external deps.
    Returns (width, height) or (None, None) if unavailable/invalid."""
    try:
        with open(path, "rb") as f:
            # Read a few header blocks (each 2880 bytes = 36 cards of 80 bytes)
            data = f.read(2880 * 4)
        if not data:
            return (None, None)
        naxis1 = naxis2 = None
        # Iterate 80-char cards
        for i in range(0, len(data), 80):
            card = data[i:i+80]
            try:
                s = card.decode("ascii", "ignore")
            except Exception:
                continue
            key = s[:8].strip()
            if key == "END":
                break
            if key in ("NAXIS1", "NAXIS2"):
                # Expect a ' = ' then a number; be tolerant of spacing
                # Example: 'NAXIS1  =                3008'
                eq = s.find("=")
                if eq != -1:
                    val_part = s[eq+1:].strip()
                    # strip comment if present
                    if "/" in val_part:
                        val_part = val_part.split("/", 1)[0].strip()
                    try:
                        val = int(val_part)
                        if key == "NAXIS1": naxis1 = val
                        else: naxis2 = val
                    except Exception:
                        pass
            # quick exit if both found
            if naxis1 is not None and naxis2 is not None:
                return (naxis1, naxis2)
        return (naxis1, naxis2)
    except Exception:
        return (None, None)


def _find_any_light_path(p) -> str | None:
    """Return an existing light FITS path from the project config, or None."""
    sessions = getattr(p, "sessions", []) or []
    for sess in sessions:
        # panel-level lists first (mosaic)
        for pan in getattr(sess, "panels", []) or []:
            for fp in getattr(pan, "lights", []) or []:
                if fp and Path(fp).is_file():
                    return fp
        # session-level fallback (non-mosaic)
        for fp in getattr(sess, "lights", []) or []:
            if fp and Path(fp).is_file():
                return fp
    return None

# -----------------------------
# Siril console bridge
# -----------------------------

class SirilConsoleBridge:
    """Wrapper to log & update progress in Siril UI via sirilpy (if available)."""
    def __init__(self):
        self.iface = None
        if s is not None:
            try:
                self.iface = s.SirilInterface(); self.iface.connect()
            except Exception:
                self.iface = None
    @property
    def connected(self) -> bool: return self.iface is not None
    def log(self, text: str, color=None):
        try:
            if self.iface:
                if color is not None:
                    self.iface.log(text, color)
                else:
                    self.iface.log(text)
        except Exception:
            pass
    def progress(self, message: str, fraction: float):
        try:
            if self.iface: self.iface.update_progress(message, max(0.0, min(1.0, float(fraction))))
        except Exception: pass
    def progress_reset(self):
        try:
            if self.iface: self.iface.reset_progress()
        except Exception: pass

class _ProcReader(QtCore.QThread):
    """
    Reads a subprocess stdout line-by-line on a background thread and
    streams batched lines via a signal, while writing to a single log file handle.
    """
    got_lines = QtCore.pyqtSignal(list)       # emits List[str]
    finished_ok = QtCore.pyqtSignal(int)      # emits returncode

    def __init__(self, proc: subprocess.Popen, log_path: Path, siril_bridge=None, parent=None):
        super().__init__(parent)
        self.proc = proc
        self.log_path = log_path
        self.siril = siril_bridge
        self._stop = False
        self._last_progress_emit = 0.0
        self._last_pct = -1.0

    def stop(self):
        self._stop = True

    def run(self):
        buf = []
        t0 = time.monotonic()
        try:
            with open(self.log_path, "a", encoding="utf-8", newline="") as lf:
                while not self._stop:
                    line = self.proc.stdout.readline()
                    if not line:
                        # EOF or process ended; flush any remaining buffer
                        if buf:
                            self.got_lines.emit(buf)
                            for ln in buf:
                                lf.write(ln)
                            buf.clear()
                        break

                    buf.append(line)

                    # Batch flush every ~120ms or if buffer grows large
                    now = time.monotonic()
                    if (now - t0) >= 0.12 or len(buf) >= 128:
                        self.got_lines.emit(buf)
                        for ln in buf:
                            lf.write(ln)
                        buf.clear()
                        t0 = now
        finally:
            # ensure any remaining buffered lines are emitted/written
            if buf:
                try:
                    self.got_lines.emit(buf)
                    with open(self.log_path, "a", encoding="utf-8", newline="") as lf2:
                        for ln in buf:
                            lf2.write(ln)
                except Exception:
                    pass

        # Wait on process and emit final code
        self.proc.wait()
        try:
            self.finished_ok.emit(self.proc.returncode)
        except Exception:
            pass

# -----------------------------
# Siril Script Builder (Siril 1.4)
# -----------------------------

class SirilCommandBuilder:
    def __init__(self, project: Project): self.project = project

    def _bias_arg(self, sess: Session) -> Optional[str]:
        if sess.master_bias: return siril_arg(sess.master_bias)
        if self.project.use_master_library: return "$defbias"
        return None

    def _dark_arg(self, sess: Session) -> Optional[str]:
        if sess.master_dark: return siril_arg(sess.master_dark)
        if self.project.use_master_library: return "$defdark"
        return None

    def _drizzle_flat_arg(self, p: Project, sess: Session, produced_flat: bool) -> str:
        if produced_flat:
            return " -flat=pp_flat_stacked"
        if getattr(sess, "master_flat", None):
            return f" -flat={siril_arg(sess.master_flat)}"
        if p.use_master_library:
            return " -flat=$defflat"
        return ""

    def _stack_cmd(self, seq_name: str, *, norm="addscale", out="stacked",
                rgb_equal=False, output_norm=True, nonorm=False, use_32b=False) -> str:
        ui  = (self.project.stack_method or "rej").lower()
        lo  = float(self.project.reject_sigma_low or 3.0)
        hi  = float(self.project.reject_sigma_high or 3.0)

        # Reuse the same mapping used everywhere else
        parts, _ = map_stack_method(
            {"rej":"Rejection","wrej":"Winsorized Rejection","mean":"Mean","median":"Median"}.get(ui, "Rejection"),
            lo, hi
        )

        opts = []
        if nonorm:     opts.append("-nonorm")
        else:          opts.append(f"-norm={norm}")
        if output_norm:opts.append("-output_norm")
        if rgb_equal:  opts.append("-rgb_equal")
        if use_32b:    opts.append("-32b")

        return " ".join(["stack", seq_name] + parts + opts + [f"-out={out}"])

    def build(self) -> str:
        """
        Build a Siril .ssf for non-mosaic projects.
        (Mosaic projects are delegated to _build_mosaic_phase1().)
        """
        p = self.project
        if not p.working_dir:
            raise ValueError("Working directory is not set.")
        work = Path(p.working_dir).resolve()

        # Mosaic path is handled separately
        if getattr(p, "mosaic_enabled", False):
            return self._build_mosaic_phase1()

        L: list[str] = []
        L.append("#!Siril script generated by multi-night-stacking.py")
        L.append("requires 1.4.0")
        L.append("setfindstar reset")
        L.append("")
        L.append(f'cd "{work.as_posix()}"')
        L.append("")
        L.append("# Use Master Library: " + ("enabled (configured in Siril preferences)." if p.use_master_library else "disabled (using per-session overrides if provided)."))
        L.append(f"# Allow uncalibrated runs: {'YES' if p.allow_uncalibrated else 'NO'}")
        if p.drizzle_enabled:
            L.append(f"# Drizzle: ON (Scaling={p.drizzle_scaling:g}, PixFrac={p.drizzle_pixfrac:g}, Kernel={p.drizzle_kernel})")
        else:
            L.append("# Drizzle: OFF")
        L.append(f"# 2-pass registration: {'ON' if p.two_pass else 'OFF'}")
        L.append(f"# Global stack method: {p.stack_method} (low={p.reject_sigma_low:g}, high={p.reject_sigma_high:g})")
        L.append("")

        # Compression control
        want_fz = bool(p.compress_intermediates)
        comp_state = [-1]
        def set_comp(val: int):
            set_comp_if_needed(L, comp_state, val)
        set_comp(1 if want_fz else 0)

        # Pack Sequence decisions (lights only)
        mode = (p.pack_sequences_mode or "off").lower()  # off|fitseq|ser|auto
        pack_thresh = int(getattr(p, "pack_threshold", 2000))

        def _count_frames(dirpath: Path) -> int:
            if not dirpath.exists():
                return 0
            n = 0
            for pat in ("*.fit", "*.fits", "*.fit.fz", "*.fits.fz"):
                n += sum(1 for _ in dirpath.glob(pat))
            return n

        session_roots: list[Path] = []
        lights_counts: list[int] = []
        total_lights = 0
        for sess in p.sessions:
            root = (work / (sess.work_subdir or sess.name)).resolve()
            session_roots.append(root)
            n = _count_frames(root / "lights")
            lights_counts.append(n)
            total_lights += n

        force_pack_lights = (mode == "auto" and total_lights >= pack_thresh)

        def _pack_flag_for_lights(sess_index: int) -> str:
            if mode in ("fitseq", "ser"):
                return f" -{mode}"
            if mode == "auto":
                if force_pack_lights or lights_counts[sess_index] >= pack_thresh:
                    return " -fitseq"
            return ""

        # Track where pp_light sequences live for global merge/stack
        pp_seqs: list[tuple[str, str]] = []
        produced_flat: dict[str, bool] = {}

        any_session = False

        # ---------- Per-session phase: build masters, lights prep, calibrate ----------
        for i, sess in enumerate(p.sessions):
            sess_root   = (work / (sess.work_subdir or sess.name)).resolve()
            bias_dir    = (sess_root / "bias").resolve()
            darks_dir   = (sess_root / "darks").resolve()
            flats_dir   = (sess_root / "flats").resolve()
            dflats_dir  = (sess_root / "dark_flats").resolve()   # <-- correct folder
            lights_dir  = (sess_root / "lights").resolve()
            process_dir = (sess_root / "process").resolve()

            L.append(f"# ---------------- Session: {sess.name} ----------------")

            # Decide if we have *raw* calibration frames to build masters
            have_raw_bias   = bias_dir.exists()   and any(bias_dir.glob("*.fit*"))
            have_raw_dflats = dflats_dir.exists() and any(dflats_dir.glob("*.fit*"))
            have_raw_darks  = darks_dir.exists()  and any(darks_dir.glob("*.fit*"))
            have_raw_flats  = flats_dir.exists()  and any(flats_dir.glob("*.fit*"))
            have_lights     = lights_dir.exists() and any(lights_dir.glob("*.fit*"))

            # Build master BIAS (no normalization)
            if have_raw_bias:
                L.append(f'cd "{bias_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert bias -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')
                L.append(self._stack_cmd("bias", norm="none", out="bias_stacked",
                                         rgb_equal=False, output_norm=False, nonorm=True))
                L.append("cd .."); L.append("")

            # Build master DARK FLAT (no normalization)
            if have_raw_dflats:
                L.append(f'cd "{dflats_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert darkflat -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')
                L.append(self._stack_cmd("darkflat", norm="none", out="df_stacked",
                                         rgb_equal=False, output_norm=False, nonorm=True))
                L.append("cd .."); L.append("")

            # Build master DARK (no normalization)
            if have_raw_darks:
                L.append(f'cd "{darks_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert dark -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')
                L.append(self._stack_cmd("dark", norm="none", out="dark_stacked",
                                         rgb_equal=False, output_norm=False, nonorm=True))
                L.append("cd .."); L.append("")

            # Build pp_flat_stacked if raw flats exist
            made_pp_flat = False
            if have_raw_flats:
                # 1) Convert flats into process/
                L.append(f'cd "{flats_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert flat -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')

                # 2) Choose how to calibrate flats: prefer a dark-flat, else a bias, else library $defbias
                _md, _mf, _mb, _mdf = _resolve_cal_paths(self.project, sess, panel=None)

                # Builder only logs; validator already prompted the user
                if not (_mdf or _mb or p.use_master_library):
                    _warn(L, f"{sess.name}: calibrating flats WITHOUT bias/dark-flat (validator already warned)")

                flat_cal_parts = ["calibrate", "flat"]
                if _mdf:
                    # Dark-flat provided: in Siril this is passed with -dark= (it’s just a dark matched to flat exposure)
                    flat_cal_parts.append(f'-dark={_mdf.as_posix()}')
                elif _mb:
                    flat_cal_parts.append(f'-bias={_mb.as_posix()}')
                elif p.use_master_library:
                    # Best-effort library bias; Siril exposes $defbias, not a dark-flat variable
                    flat_cal_parts.append("-bias=$defbias")

                # No CFA/equalize flags for flat calibration
                L.append(" ".join(flat_cal_parts))

                # 3) Now we have pp_flat_*.fit — stack them to a master
                L.append(self._stack_cmd("pp_flat", norm="mul", out="pp_flat_stacked",
                                        rgb_equal=False, output_norm=False))
                L.append("cd .."); L.append("")
                made_pp_flat = True
                produced_flat[sess.name] = True

            # Lights → process (with packing if chosen)
            if not have_lights:
                L.append("# (No lights found in this session.)")
                L.append("")
                continue

            L.append(f'cd "{lights_dir.as_posix()}"')
            L.append("setext fit")
            L.append(f"convert light{_pack_flag_for_lights(i)} -out=../process")
            L.append(f'cd "{process_dir.as_posix()}"')

            # Resolve effective masters (panel=None → session→library)
            md, mf, *_ = _resolve_cal_paths(self.project, sess, panel=None)

            # Geometry preflight (parks odd sizes; warns if masters mismatch)
            preflight_geometry(
                lights_dir=lights_dir,
                process_dir=process_dir,
                master_dark=md,
                master_flat=mf,
                logger=print,
            )
            # --- Calibration availability (respect Master Library and session-built flats) ---
            use_lib = bool(getattr(p, "use_master_library", False))

            # What do we actually have available?
            has_dark = bool(md) or use_lib
            has_flat = bool(mf) or bool(made_pp_flat) or use_lib

            if not has_dark and not has_flat:
                _warn(L, f"Session {sess.name}: running lights WITHOUT dark/flat (validator already warned)")
            elif not has_dark:
                _warn(L, f"Session {sess.name}: no dark for lights (validator already warned)")
            elif not has_flat:
                _warn(L, f"Session {sess.name}: no flat for lights (validator already warned)")

            # --- Build safe 'calibrate light' for OSC, honoring library & pp_flat ---
            parts = ["calibrate", "light"]

            # DARK
            if md:
                parts.append(f'-dark={md.as_posix()}')
                use_dark_cc = True
            elif use_lib:
                parts.append("-dark=$defdark")
                use_dark_cc = True
            else:
                use_dark_cc = False  # truly no dark

            # FLAT
            flat_used = False
            if made_pp_flat:
                parts.append('-flat=pp_flat_stacked')
                flat_used = True
            elif mf:
                parts.append(f'-flat={mf.as_posix()}')
                flat_used = True
            elif use_lib:
                parts.append("-flat=$defflat")
                flat_used = True

            # OSC flags
            parts.append("-cfa")
            if use_dark_cc:
                parts.append("-cc=dark")
            if flat_used and not p.drizzle_enabled:
                parts.append("-equalize_cfa")
            if not p.drizzle_enabled:
                parts.append("-debayer")

            L.append(" ".join(parts))
            L.append("")

            pp_seqs.append((process_dir.as_posix(), "pp_light"))
            any_session = True

        # ---------- Global register/stack ----------
        if not (any_session and pp_seqs):
            L.append("# No sessions with usable lights were found to stack.")
            L.append("")
            L.append("setfindstar reset")
            # Do not close here; leave Siril’s viewer state alone
            # L.append("close")
            return "\n".join(L)

        L.append("# ---------------- Global Registration & Stacking ----------------")
        base_dir, _ = pp_seqs[0]
        L.append(f'cd "{base_dir}"')

        reg_flags    = " -layer=0" + (" -2pass" if p.two_pass else "")
        drizzle_args = f" -drizzle -scale={p.drizzle_scaling:g} -pixfrac={p.drizzle_pixfrac:g} -kernel={p.drizzle_kernel}"

        L.append("setfindstar")

        if len(pp_seqs) == 1:
            # Single session
            sess = p.sessions[0]
            reg_target = "pp_light"

            if p.drizzle_enabled and not p.two_pass:
                # Drizzle fast-path (no seqapplyreg) – add -flat as weight if available
                flat_opt = ""
                if produced_flat.get(sess.name, False):
                    flat_opt = " -flat=pp_flat_stacked"
                elif p.use_master_library:
                    flat_opt = " -flat=$defflat"

                L.append(f"register {reg_target}{reg_flags}{drizzle_args}{flat_opt}")
                L.append(self._stack_cmd(
                    "r_pp_light", norm="addscale", out="final_stacked",
                    rgb_equal=True, output_norm=True, use_32b=p.stack_32bit,
                ))
            else:
                # Non-drizzle OR drizzle+2pass (needs seqapplyreg)
                L.append(f"register {reg_target}{reg_flags}")
                if p.drizzle_enabled:
                    L.append(f"seqapplyreg {reg_target}{drizzle_args}")
                    L.append(self._stack_cmd(
                        "r_pp_light", norm="addscale", out="final_stacked",
                        rgb_equal=True, output_norm=True, use_32b=p.stack_32bit,
                    ))
                else:
                    if p.two_pass:
                        L.append(f"seqapplyreg {reg_target}")
                    L.append(self._stack_cmd(
                        "r_pp_light", norm="addscale", out="final_stacked",
                        rgb_equal=True, output_norm=True, use_32b=p.stack_32bit,
                    ))
        else:
            # Multi-session: merge -> register/stack
            inputs = " ".join([f'"{folder}/{seq}"' for (folder, seq) in pp_seqs])
            L.append(f"merge {inputs} all_sessions")

            if p.drizzle_enabled and not p.two_pass:
                L.append(f"register all_sessions{reg_flags}{drizzle_args}")
                L.append(self._stack_cmd(
                    "r_all_sessions", norm="addscale", out="final_stacked",
                    rgb_equal=True, output_norm=True, use_32b=p.stack_32bit,
                ))
            else:
                L.append(f"register all_sessions{reg_flags}")
                if p.drizzle_enabled:
                    L.append(f"seqapplyreg all_sessions{drizzle_args}")
                    L.append(self._stack_cmd(
                        "r_all_sessions", norm="addscale", out="final_stacked",
                        rgb_equal=True, output_norm=True, use_32b=p.stack_32bit,
                    ))
                else:
                    if p.two_pass:
                        L.append("seqapplyreg all_sessions")
                    L.append(self._stack_cmd(
                        "r_all_sessions", norm="addscale", out="final_stacked",
                        rgb_equal=True, output_norm=True, use_32b=p.stack_32bit,
                    ))

        # Final copy/open
        proj_slug = safe_slug(self.project.name)
        L.append('# Copy final image to the project working directory')
        want_fz = bool(getattr(p, "compress_intermediates", False))
        if want_fz:
            L.append("load final_stacked.fit.fz")
            set_comp(0)  # save uncompressed final to project root
        else:
            L.append("load final_stacked.fit")
        L.append("mirrorx -bottomup")
        L.append(f'save "../../{proj_slug}_final.fit"')
        L.append(f'# Final output: ../../{proj_slug}_final.fit')
        # L.append(f'cd "{base_dir}"')

        # Footer
        L.append("")
        L.append("setfindstar reset")
        # Do not close; this leaves the final image open in Siril
        # L.append("close")
        return "\n".join(L)

    def _build_mosaic_phase1(self) -> str:
        """
        Phase 1 for mosaic projects:
        - Per panel: convert → preflight → calibrate (OSC) → register (panel-local)
        - Outputs r_pp_light sequences per panel for downstream mosaic assembly (phase 2).
        """
        p = self.project
        # Drizzle can be enabled either via the main Drizzle section or via Mosaic "Drizzle per panel" scope.
        drizzle_any = bool(getattr(p, "drizzle_enabled", False) or getattr(p, "mosaic_drizzle_per_panel", False) )
        # Phase 1 (per-panel) drizzle should trigger when either main drizzle is enabled or "Drizzle per panel" is selected.
        drizzle_panel = bool(getattr(p, "drizzle_enabled", False) or getattr(p, "mosaic_drizzle_per_panel", False))
        # Collect registered sequences per panel across sessions
        panel_seq_map: dict[str, list[str]] = {}
        if not getattr(p, "mosaic_enabled", False):
            # Fallback to non-mosaic if toggled off
            return self.build()

        if not p.working_dir:
            raise ValueError("Working directory is not set.")
        work = Path(p.working_dir).resolve()

        L: list[str] = []
        L.append("#!Siril script generated by multi-night-stacking.py (Mosaic Phase 1)")
        L.append("requires 1.4.0")
        L.append("setfindstar reset")
        L.append("")
        L.append(f'cd "{work.as_posix()}"')
        L.append("")
        L.append("# --- Mosaic settings ---")
        L.append("# Use Master Library: " + ("enabled (configured in Siril preferences)." if p.use_master_library else "disabled (panel/session overrides only)."))
        L.append(f"# Allow uncalibrated runs: {'YES' if p.allow_uncalibrated else 'NO'}")
        L.append("# NOTE: Mosaic Mode forces 'Pack sequences' OFF (Siril 1.4 cannot platesolve packed sequences).")
        if drizzle_any:
            L.append(f"# Drizzle: ON (Scaling={p.drizzle_scaling:g}, PixFrac={p.drizzle_pixfrac:g}, Kernel={p.drizzle_kernel})")
        else:
            L.append("# Drizzle: OFF")
        L.append(f"# 2-pass registration: {'ON' if p.two_pass else 'OFF'}")
        L.append("")

        # Compression control (avoid flapping)
        want_fz = bool(p.compress_intermediates)
        comp_state = -1
        def set_comp(val: int):
            nonlocal comp_state
            if comp_state != val:
                L.append(f"setcompress {val}")
                comp_state = val
        set_comp(1 if want_fz else 0)

        # Pack Sequence decisions (per-panel lights)
        mode = (p.pack_sequences_mode or "off").lower()  # off|fitseq|ser|auto
        # Siril 1.4 mosaic limitation: plate-solving doesn't work on FITSEQ/SER.
        # Force unpacked FITS for mosaic panel processing.
        mode = "off"        
        pack_thresh = int(getattr(p, "pack_threshold", 2000))

        def _count_frames(dirpath: Path) -> int:
            if not dirpath.exists():
                return 0
            n = 0
            for pat in ("*.fit", "*.fits", "*.fit.fz", "*.fits.fz"):
                n += sum(1 for _ in dirpath.glob(pat))
            return n

        # Collect panel roots for optional summary / future phases
        any_panel = False

        # NEW: collect per-panel finals to feed Phase 2
        produced: list[str] = []

        # -------- Iterate sessions -> panels --------
        for sess in p.sessions:
            sess_root = (work / (sess.work_subdir or sess.name)).resolve()
            panels = list(getattr(sess, "panels", []) or [])
            if not panels:
                # Some users organize panels as subfolders even without explicit panel objects.
                # If no panel objects, treat a single "panel" at session root.
                panels = [{"name": "Panel", "id": "Panel", "master_dark": None, "master_flat": None,
                        "master_bias": None, "master_darkflat": None}]

            L.append(f"# ------------- Session: {sess.name} (panels: {len(panels)}) -------------")

            # Precompute per-session threshold logic for AUTO packing
            # (we evaluate per panel too, but a session-level hint can help)
            session_pan_light_count = 0
            for _pan in panels:
                pname = getattr(_pan, "name", None) or getattr(_pan, "id", None) or "panel"
                pan_root = (sess_root / pname).resolve()
                session_pan_light_count += _count_frames(pan_root / "lights")

            def _pack_flag_for_panel(pan_root: Path) -> str:
                if mode in ("fitseq", "ser"):
                    return f" -{mode}"
                if mode == "auto":
                    # Prefer panel-level count; fall back to session aggregate
                    n = _count_frames((pan_root / "lights"))
                    if n >= pack_thresh or session_pan_light_count >= pack_thresh:
                        return " -fitseq"
                return ""

            for panel in panels:
                pid   = (getattr(panel, "panel_id", None)
                         or getattr(panel, "id", None)
                         or getattr(panel, "name", None)
                         or "panel")
                pname = getattr(panel, "name", None) or pid

                L.append(f"# ---- Panel {pid} in Session {sess.name} ----")

                pan_root   = (sess_root / pname).resolve()
                lights_dir = (pan_root / "lights").resolve()
                proc_dir   = (pan_root / "process").resolve()

                # Convert panel lights
                if not lights_dir.exists() or not any(lights_dir.glob("*.fit*")):
                    L.append(f'# (No lights found for panel {pid} in this session.)')
                    L.append("")
                    continue

                # --- Optional: build per-panel pp_flat_stacked when raw flats are provided ---
                made_pp_flat = False
                panel_flats = list(getattr(panel, "flats", []) or [])
                if panel_flats:
                    # Prefer an explicit flats_dir on the panel if you store it; otherwise use <panel>/flats
                    flats_dir_attr = getattr(panel, "flats_dir", "") or ""
                    flats_dir = Path(flats_dir_attr) if flats_dir_attr else (pan_root / "flats")

                    # 1) Convert flats into the panel's process folder
                    L.append(f'cd "{flats_dir.as_posix()}"')
                    L.append("setext fit")
                    L.append('convert flat -out=../process')
                    L.append(f'cd "{proc_dir.as_posix()}"')

                    # 2) Calibrate flats: prefer dark-flat, else bias, else library bias
                    md, mf, mb, mdf = _resolve_cal_paths(p, sess, panel=panel)

                    if not (mdf or mb or getattr(p, "use_master_library", False)):
                        _warn(L, f"{getattr(sess,'name','Session')} / "
                                f"{getattr(panel,'panel_id',getattr(panel,'id','Panel'))}: "
                                "calibrating flats WITHOUT bias/dark-flat (validator already warned)")

                    cal_flat = ["calibrate", "flat"]
                    if mdf:
                        cal_flat.append(f'-dark={Path(mdf).as_posix()}')
                    elif mb:
                        cal_flat.append(f'-bias={Path(mb).as_posix()}')
                    elif bool(getattr(p, "use_master_library", False)):
                        cal_flat.append("-bias=$defbias")
                    L.append(" ".join(cal_flat))

                    # 3) Stack calibrated flats to master
                    L.append(self._stack_cmd("pp_flat", norm="mul", out="pp_flat_stacked",
                                            rgb_equal=False, output_norm=False))
                    L.append("cd .."); L.append("")
                    made_pp_flat = True

                L.append(f'cd "{lights_dir.as_posix()}"')
                L.append("setext fit")
                L.append(f'convert light{_pack_flag_for_panel(pan_root)} -out=../process')
                L.append(f'cd "{proc_dir.as_posix()}"')

                # Resolve calibration paths (panel → session → library)
                md, mf, mb, mdf = _resolve_cal_paths(self.project, sess, panel=panel)

                # Geometry preflight
                preflight_geometry(
                    lights_dir=lights_dir,
                    process_dir=proc_dir,
                    master_dark=md,
                    master_flat=mf,
                    logger=print,
                )

                # Warnings (panel scope)
                if not md and not mf:
                    _warn(L, f"{sess.name} / {pid}: running lights WITHOUT dark/flat (validator already warned)")
                elif not md:
                    _warn(L, f"{sess.name} / {pid}: no dark for lights (validator already warned)")
                elif not mf:
                    _warn(L, f"{sess.name} / {pid}: no flat for lights (validator already warned)")

                # Build safe calibrate command (OSC) — honor Master Library like non-mosaic builder
                use_lib = bool(getattr(p, "use_master_library", False))
                parts = ["calibrate", "light"]

                # DARK
                use_dark_cc = False
                if md:
                    parts.append(f'-dark={md.as_posix()}')
                    use_dark_cc = True
                elif use_lib:
                    parts.append("-dark=$defdark")
                    use_dark_cc = True

                # FLAT
                flat_used = False
                if made_pp_flat:
                    parts.append('-flat=pp_flat_stacked')
                    flat_used = True                
                elif mf:
                    parts.append(f'-flat={mf.as_posix()}')
                    flat_used = True
                elif use_lib:
                    parts.append("-flat=$defflat")
                    flat_used = True
                # (If you later add per-panel pp_flat_stacked support, prefer it here.)

                # OSC flags
                parts.append("-cfa")
                if use_dark_cc:
                    parts.append("-cc=dark")
                if flat_used and not drizzle_panel:
                    parts.append("-equalize_cfa")
                if not drizzle_panel:
                    parts.append("-debayer")

                L.append(" ".join(parts))
                L.append("")

                # --- Background extraction (optional, controlled by "Panel Background Extraction") ---
                do_bkg = bool(getattr(p, "panel_background_extraction", False))
                seq_base = "bkg_pp_light" if do_bkg else "pp_light"

                if do_bkg:
                    L.append("# Panel background extraction enabled")
                    L.append("seqsubsky pp_light 1")  # -> bkg_pp_light_*.fit
                else:
                    L.append("# Panel background extraction disabled (using calibrated pp_light directly)")

                L.append("")

                # --- Plate-solve first frame to produce a distortion WCS file ---
                L.append("# Plate-solve to produce a distortion WCS for undistortion-aware registration")
                L.append(f"load {seq_base}_00001")
                L.append('parse $RA:ra$_$DEC:dec$')
                L.append("platesolve -force -disto=platesolve_data.wcs")
                L.append("")

                # --- Register the chosen sequence (with WCS undistortion) ---
                # Mosaic Registration Mode governs whether we run 1-pass or 2-pass registration.
                # Drizzle-per-panel ALWAYS forces 2-pass because drizzle output is generated via seqapplyreg.
                mosaic_two_pass = str(getattr(p, "mosaic_registration_mode", "")).lower().startswith("two")
                reg_two_pass = bool(mosaic_two_pass) or bool(getattr(p, "two_pass", False))

                if drizzle_panel:
                    drizzle_args = (
                        f" -drizzle -scale={p.drizzle_scaling:g}"
                        f" -pixfrac={p.drizzle_pixfrac:g}"
                        f" -kernel={p.drizzle_kernel}"
                    )
                    L.append(f"# Register panel {pid} sequence with WCS undistortion (2-pass) + drizzle")
                    # Siril CLI/script syntax: -2pass computes transforms only (no transformed images are generated).
                    # There is no '-noout' option for the 'register' command in SSF scripts.
                    L.append(f"register {seq_base} -disto=file platesolve_data.wcs -2pass")
                    L.append(f"seqapplyreg {seq_base}{drizzle_args}")
                elif reg_two_pass:
                    L.append(f"# Register panel {pid} sequence with WCS undistortion (2-pass)")
                    L.append(f"register {seq_base} -disto=file platesolve_data.wcs -2pass")
                    L.append(f"seqapplyreg {seq_base}")
                else:
                    L.append(f"# Register panel {pid} sequence with WCS undistortion")
                    L.append(f"register {seq_base} -disto=file platesolve_data.wcs")
                L.append("")

                # Instead of stacking now, remember this session’s registered seq for cross-session merge
                seq_dir = (proc_dir / f"r_{seq_base}").as_posix()   # seq_base is "bkg_pp_light" if BE on, else "pp_light"
                panel_seq_map.setdefault(pid, []).append(seq_dir)

                any_panel = True

        if not any_panel:
            L.append("# No panels with usable lights were found.")
            L.append("")
            L.append("setfindstar reset")
            # Do not close; nothing was produced anyway
            # L.append("close")
            return "\n".join(L)

        # --- End Mosaic Phase 1 (all panels calibrated, registered & stacked) ---
        # Begin Phase 2: stitch per-panel finals into the mosaic
        # Map UI selections to tokens/flags for the Phase 2 helper
        two_pass = (str(getattr(p, "mosaic_registration_mode", "")).lower().startswith("two"))
        mosaic_method_token = (getattr(p, "stack_method", "rej") or "rej").lower()
        sigma_lo = float(getattr(p, "reject_sigma_low", 3.0) or 3.0)
        sigma_hi = float(getattr(p, "reject_sigma_high", 3.0) or 3.0)
        feather_px = int(getattr(p, "mosaic_feather_px", 0) or 0)
        maximize_framing = bool(getattr(p, "mosaic_maximize_framing", True))
        overlap_norm = bool(getattr(p, "mosaic_overlap_norm", False))

        # ---- Phase 1B: cross-session merge and stack, per panel ----
        L.append(f"# ---- Phase 1B: cross-session merge and stack, per panel ----")
        L.append("")
        for pid, seqs in panel_seq_map.items():
            # Work in the first seq's process directory
            first_seq = Path(seqs[0])
            proc_dir = first_seq.parent  # .../Session X/<panel>/process
            L.append(f"# ---- Panel {pid} ----")
            L.append(f'cd "{proc_dir.as_posix()}"')
            # Ensure intermediate outputs (merge/register) respect the user compression setting.
            # Phase 1B temporarily disables compression for final per-panel stacks only.
            set_comp(1 if want_fz else 0)

            lo = float(getattr(p, "reject_sigma_low", 3.0) or 3.0)
            hi = float(getattr(p, "reject_sigma_high", 3.0) or 3.0)
            parts, _ = map_stack_method(getattr(p, "stack_method", "Winsorized Rejection"), lo, hi)

            if len(seqs) == 1:
                # Single session: no merge, no re-register — stack the existing registered seq
                seq_name = first_seq.name  # e.g. "r_bkg_pp_light" or "r_pp_light"
                out_name = f"{safe_slug(pid)}_final"
                cmd = ["stack", seq_name] + parts + ["-norm=addscale"]
                cmd += ["-rgb_equal", "-output_norm"]
                if bool(getattr(p, "stack_32bit", False)):
                    cmd.append("-32b")
                cmd.append(f"-out={out_name}")

                if want_fz:
                    set_comp(0)
                L.append(" ".join(cmd))
                if want_fz:
                    set_comp(1)
                L.append("")
                produced.append((proc_dir / f"{out_name}.fit").as_posix())

            else:
                # Multi-session: merge -> register -> stack
                merged_name = f"ALL_{pid}"
                merge_args = " ".join([f'"{s}"' for s in seqs])
                L.append(f"merge {merge_args} {merged_name}")

                # full-canvas registration (simple, no extra flags needed here)
                L.append(f"register {merged_name} -layer=0")
                out_name = f"{safe_slug(pid)}_final"
                cmd = ["stack", f"r_{merged_name}_"] + parts + ["-norm=addscale"]
                cmd += ["-rgb_equal", "-output_norm"]
                if bool(getattr(p, "stack_32bit", False)):
                    cmd.append("-32b")
                cmd.append(f"-out={out_name}")

                if want_fz:
                    set_comp(0)
                L.append(" ".join(cmd))
                if want_fz:
                    set_comp(1)
                L.append("")
                produced.append((proc_dir / f"{out_name}.fit").as_posix())

        # Keep using the same compression state tracker from Phase 1
        emit_phase2_mosaic(
            work=work,
            produced=produced,
            p=p,
            L=L,
            comp_state=comp_state,
            set_comp_if_needed=set_comp_if_needed,
            safe_slug=safe_slug,
            feather_px=feather_px,
            overlap_norm=overlap_norm,
        )

        return "\n".join(L)

def emit_phase2_mosaic(
    *,
    work,                        # Path to project working dir (Path)
    produced,                    # list[str] per-panel *_final.fit (some may be None/"")
    p,                           # project/config object
    L,                           # list[str] Siril commands buffer
    comp_state,                  # compression state tracker (e.g., [0] or 0)
    set_comp_if_needed,          # fn(L, comp_state_ref, desired_state)
    safe_slug,                   # fn(name)->safe slug
    feather_px,                  # int pixels for -feather
    overlap_norm                 # bool for -overlap_norm
):
    """
    Phase 2: WCS-based mosaic stitching.
    - Build a mosaic sequence by merging each panel's r_pp_light (already registered in Phase 1).
    - Plate-solve the merged sequence, then seqapplyreg with framing=max to place panels on a full canvas.
    - Optional background match (if enabled in p).
    - Stack to mosaic_final.fit, optionally resample, mirror + write {project}_final.fit at project root.
    """

    # Gather only the produced panel finals (skip any panels without output)
    finals = [f for f in (produced or []) if f]


    # Compression preference for intermediates (.fit.fz) in this run
    want_fz = bool(getattr(p, "compress_intermediates", False))
    # Phase 2 mosaic stitching is built from per-panel *_final.fit images, which are RGB (not mono/CFA).
    # Siril drizzle only works on mono/CFA sequences, so drizzle must be skipped here.
    drizzle_mosaic = False
    if getattr(p, "drizzle_enabled", False):
        L.append("# NOTE: Drizzle is enabled, but Phase 2 mosaic stitching uses RGB panel finals; drizzle is skipped in Phase 2.")

    L.append("# ---- Phase 2: Stitching panels into a mosaic ----")
    L.append("setcompress 0")
    if len(finals) == 0:
        L.append("# No panel finals were produced; nothing to stitch.")
        L.append("setfindstar reset")
        # Do not close; leave viewer state as-is
        # L.append("close")
        return

    if len(finals) == 1:
        # Single panel: just promote to mosaic_final and project root
        one = finals[0]
        L.append(f'cd "{Path(one).parent.as_posix()}"')
        L.append(f'load "{Path(one).as_posix()}"')
        L.append('save "mosaic_final.fit"')

        proj_slug = safe_slug(getattr(p, "name", "project"))
        L.append('load "mosaic_final.fit"')
        L.append('mirrorx -bottomup')
        final_abs = (work / f"{proj_slug}_final.fit").as_posix()
        L.append(f'save "{final_abs}"')
        L.append(f"# Final mosaic written to {final_abs}")
        L.append("setfindstar reset")
        # Do not close; leave mosaic_final displayed in Siril
        # L.append("close")
        return

    # Use the first panel's existing process directory as the working folder
    mosaic_dir = Path(finals[0]).parent
    L.append(f'cd "{mosaic_dir.as_posix()}"')
    L.append("# Using existing process directory for mosaic assembly.")

    # --- Build mosaic sequence directly from per-panel finals (no merge) ---
    # Optional: keep honoring the user’s Global Reference by reordering 'finals' first
    ref_sel = (getattr(p, "mosaic_global_reference", "") or "").strip()
    if ref_sel and not ref_sel.lower().startswith("bestframe"):
        try:
            sess_name, panel_id = [x.strip() for x in ref_sel.split("/", 1)]
            import re
            sess_slug = re.sub(r"[^A-Za-z0-9_.-]+", "_", sess_name)
            pid_slug  = re.sub(r"[^A-Za-z0-9_.-]+", "_", panel_id)
            wanted = f"{sess_slug}_{pid_slug}_final.fit".lower()
            match = next((f for f in finals if f.lower().endswith(wanted)), None)
            if match:
                finals = [match] + [f for f in finals if f != match]
                L.append(f'# Using "{ref_sel}" as global registration reference.')
            else:
                L.append(f'# NOTE: Global Reference "{ref_sel}" not found among panel finals; using first panel as reference.')
        except Exception:
            L.append(f'# NOTE: Could not parse Global Reference "{ref_sel}"; using first panel as reference.')
    else:
        L.append("# Using first panel as registration reference (BestFrame auto).")

    # Work inside the first panel's existing process directory (already exists)
    from pathlib import Path as _P
    L.append("# Build mosaic sequence from per-panel finals:")
    for i, fpath in enumerate(finals, start=1):
        L.append(f'load "{_P(fpath).as_posix()}"')
        L.append(f'save "mosaic_{i:05d}.fit"')
    L.append("")  # now we have a 'mosaic' sequence on disk

    # WCS solve and apply registration on the merged sequence
    L.append("seqplatesolve mosaic -force -nocache")
    maximize_framing = bool(getattr(p, "mosaic_maximize_framing", True))
    if maximize_framing:
        L.append("seqapplyreg mosaic -framing=max")
    else:
        L.append("seqapplyreg mosaic")
    L.append("")

    # Ensure final outputs are uncompressed

    lo = float(getattr(p, "reject_sigma_low", 3.0) or 3.0)
    hi = float(getattr(p, "reject_sigma_high", 3.0) or 3.0)
    parts, _ = map_stack_method(p.stack_method, lo, hi)

    cmd = ["stack", "r_mosaic_"]
    cmd += parts
    cmd += ["-norm=addscale"]
    if maximize_framing:
        cmd.append("-maximize")
    if int(feather_px or 0) > 0:
        cmd.append(f"-feather={int(feather_px)}")
    if overlap_norm:
        cmd.append("-overlap_norm")
    cmd += ["-rgb_equal", "-output_norm"]
    if bool(getattr(p, "stack_32bit", False)):
        cmd.append("-32b")
    cmd.append("-out=mosaic_final")

    L.append(" ".join(cmd))
    L.append("")

    # Optional canvas scaling
    try:
        canvas_scale = float(getattr(p, "mosaic_canvas_scale", 1.0) or 1.0)
    except Exception:
        canvas_scale = 1.0
    if abs(canvas_scale - 1.0) > 1e-6:
        L.append('load "mosaic_final.fit"')
        L.append(f"resample {canvas_scale:g}")
        L.append('save "mosaic_final_scaled.fit"')
        L.append("")

    # Promote mosaic final to project root and load it
    proj_slug = safe_slug(getattr(p, "name", "project"))
    L.append('load "mosaic_final.fit"')
    L.append('mirrorx -bottomup')
    final_abs = (work / f"{proj_slug}_final.fit").as_posix()
    L.append(f'save "{final_abs}"')
    L.append(f"# Final mosaic written to {final_abs}")

    L.append("setfindstar reset")
    # Do not close; keep the mosaic open in Siril
    # L.append("close")

def map_stack_method(ui_method: str, sigma_lo: float, sigma_hi: float):
    """
    Returns (parts, needs_sigmas).
    Examples:
      ["rej", "sigma", "3", "3"]  -> Sigma Rejection
      ["rej", "3", "3"]           -> Winsorized Rejection (default)
      ["mean", "none"]            -> Mean
      ["med"]                     -> Median
    """
    ui = (ui_method or "").strip().lower()

    # Accept either labels or old tokens, just in case
    if ui in ("sigma rejection", "sigma", "sigma clipping", "rej sigma", "rej_sigma"):
        return ["rej", "sigma", f"{sigma_lo:g}", f"{sigma_hi:g}"], True

    if ui in ("winsorized rejection", "winsorized", "rejection", "wrej", "rej winsorized", "rej_winsorized"):
        # Simple 'rej' uses Siril's default winsorized rejection
        return ["rej", f"{sigma_lo:g}", f"{sigma_hi:g}"], True

    if ui in ("mean",):
        return ["mean", "none"], False

    if ui in ("median", "med"):
        return ["med"], False

    # Fallback to winsorized rejection
    return ["rej", f"{sigma_lo:g}", f"{sigma_hi:g}"], True

# -----------------------------
# Qt Widgets
# -----------------------------

class FrameListWidget(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()
    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        title = "Biases" if title.lower()=="bias" else ("Dark Flats" if title.lower()=="dark_flats" else title)
        self.title = title

        self.list = QtWidgets.QListWidget()
        self.btn_add = QtWidgets.QPushButton("Add")
        self.btn_remove = QtWidgets.QPushButton("Remove")
        self.btn_clear = QtWidgets.QPushButton("Clear")

        btns = QtWidgets.QHBoxLayout()
        btns.addWidget(self.btn_add); btns.addWidget(self.btn_remove); btns.addWidget(self.btn_clear)

        layout = QtWidgets.QVBoxLayout(self)
        layout.addWidget(QtWidgets.QLabel(title))
        layout.addWidget(self.list)
        layout.addLayout(btns)

        self.btn_add.clicked.connect(self.add_files)
        self.btn_remove.clicked.connect(self.remove_selected)
        self.btn_clear.clicked.connect(self.clear_all)

    def add_files(self):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, f"Add {self.title}")
        for f in files: self.list.addItem(f)
        if files: self.changed.emit()

    def remove_selected(self):
        for it in self.list.selectedItems():
            self.list.takeItem(self.list.row(it))
        self.changed.emit()

    def clear_all(self):
        self.list.clear()
        self.changed.emit()

    def get_paths(self) -> List[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def set_paths(self, paths: List[str]):
        self.list.clear()
        for p in paths: self.list.addItem(p)

class MasterOverrideWidget(QtWidgets.QWidget):
    changed = QtCore.pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)

        self.ed_bias = QtWidgets.QLineEdit()
        self.ed_dark = QtWidgets.QLineEdit()
        self.ed_flat = QtWidgets.QLineEdit()
        self.ed_darkflat = QtWidgets.QLineEdit()

        self.btn_bias = QtWidgets.QPushButton("...")
        self.btn_dark = QtWidgets.QPushButton("...")
        self.btn_flat = QtWidgets.QPushButton("...")
        self.btn_darkflat = QtWidgets.QPushButton("...")

        grid = QtWidgets.QGridLayout(self)
        grid.addWidget(QtWidgets.QLabel("Master Bias"), 0, 0)
        grid.addWidget(self.ed_bias, 0, 1)
        grid.addWidget(self.btn_bias, 0, 2)

        grid.addWidget(QtWidgets.QLabel("Master Dark"), 1, 0)
        grid.addWidget(self.ed_dark, 1, 1)
        grid.addWidget(self.btn_dark, 1, 2)

        grid.addWidget(QtWidgets.QLabel("Master Flat"), 2, 0)
        grid.addWidget(self.ed_flat, 2, 1)
        grid.addWidget(self.btn_flat, 2, 2)

        grid.addWidget(QtWidgets.QLabel("Master Dark Flat"), 3, 0)
        grid.addWidget(self.ed_darkflat, 3, 1)
        grid.addWidget(self.btn_darkflat, 3, 2)

        self.btn_bias.clicked.connect(lambda: self.pick_file(self.ed_bias))
        self.btn_dark.clicked.connect(lambda: self.pick_file(self.ed_dark))
        self.btn_flat.clicked.connect(lambda: self.pick_file(self.ed_flat))
        self.btn_darkflat.clicked.connect(lambda: self.pick_file(self.ed_darkflat))

        for ed in (self.ed_bias, self.ed_dark, self.ed_flat, self.ed_darkflat):
            ed.textChanged.connect(self.changed)

    def pick_file(self, target: QtWidgets.QLineEdit):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Pick master file")
        if f:
            target.setText(f)
            self.changed.emit()

    def get_overrides(self) -> Dict[str, Optional[str]]:
        return {
            "master_bias": self.ed_bias.text() or None,
            "master_dark": self.ed_dark.text() or None,
            "master_flat": self.ed_flat.text() or None,
            "master_dark_flat": self.ed_darkflat.text() or None,
        }

    def set_overrides(self, d: Dict[str, Optional[str]]):
        self.ed_bias.setText(d.get("master_bias") or "")
        self.ed_dark.setText(d.get("master_dark") or "")
        self.ed_flat.setText(d.get("master_flat") or "")
        self.ed_darkflat.setText(d.get("master_dark_flat") or "")

class SessionEditor(QtWidgets.QWidget):
    """Right-hand session editor: boxed lists for frame types + per-session overrides."""
    changed = QtCore.pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)

        # --- Top: session meta ---
        self.ed_session_name = QtWidgets.QLineEdit("Session 1")
        self.ed_work_subdir  = QtWidgets.QLineEdit()
        meta_form = QtWidgets.QFormLayout()
        meta_form.addRow("Session Name", self.ed_session_name)
        meta_form.addRow("Working Subdir (optional)", self.ed_work_subdir)

        # --- Helper to create a boxed list group with Add/Remove/Clear ---
        def make_list_group(title: str):
            box = QtWidgets.QGroupBox(title)
            v = QtWidgets.QVBoxLayout(box)
            lst = QtWidgets.QListWidget()
            lst.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            v.addWidget(lst)
            hb = QtWidgets.QHBoxLayout()
            btn_add = QtWidgets.QPushButton("Add")
            btn_rm  = QtWidgets.QPushButton("Remove")
            btn_clr = QtWidgets.QPushButton("Clear")
            hb.addWidget(btn_add); hb.addWidget(btn_rm); hb.addWidget(btn_clr); hb.addStretch(1)
            v.addLayout(hb)
            return box, lst, btn_add, btn_rm, btn_clr

        # --- Boxed groups for each frame type ---
        self.grp_lights, self.lst_lights, self.bt_add_light, self.bt_rm_light, self.bt_clr_light = make_list_group("Lights")
        self.grp_biases, self.lst_biases, self.bt_add_bias, self.bt_rm_bias, self.bt_clr_bias     = make_list_group("Biases")
        self.grp_darks,  self.lst_darks,  self.bt_add_dark, self.bt_rm_dark, self.bt_clr_dark     = make_list_group("Darks")
        self.grp_flats,  self.lst_flats,  self.bt_add_flat, self.bt_rm_flat, self.bt_clr_flat     = make_list_group("Flats")
        self.grp_df,     self.lst_df,     self.bt_add_df,   self.bt_rm_df,   self.bt_clr_df       = make_list_group("Dark Flats")

        # --- Per-session Master Overrides (boxed form) ---
        self.grp_overrides = QtWidgets.QGroupBox("Per-session Master Overrides (optional)")
        ov_form = QtWidgets.QFormLayout(self.grp_overrides)

        def make_pick_row():
            le = QtWidgets.QLineEdit()
            btn = QtWidgets.QPushButton("…")
            row = QtWidgets.QHBoxLayout()
            row.addWidget(le, 1); row.addWidget(btn)
            w = QtWidgets.QWidget(); w.setLayout(row)
            return le, btn, w

        self.ed_master_bias, self.bt_pick_mbias, w_mb = make_pick_row()
        self.ed_master_dark, self.bt_pick_mdark, w_md = make_pick_row()
        self.ed_master_flat, self.bt_pick_mflat, w_mf = make_pick_row()
        self.ed_master_df,   self.bt_pick_mdf,   w_mdf= make_pick_row()

        ov_form.addRow("Master Bias",     w_mb)
        ov_form.addRow("Master Dark",     w_md)
        ov_form.addRow("Master Flat",     w_mf)
        ov_form.addRow("Master Dark Flat",w_mdf)

        # --- Lay out: meta at top, Lights (full width), then 2x2 grid, then overrides (full width) ---
        grid = QtWidgets.QGridLayout()
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)

        # Lights spans two columns
        grid.addWidget(self.grp_lights, 0, 0, 1, 2)
        # Biases / Darks row
        grid.addWidget(self.grp_biases, 1, 0, 1, 1)
        grid.addWidget(self.grp_darks,  1, 1, 1, 1)
        # Flats / Dark Flats row
        grid.addWidget(self.grp_flats,  2, 0, 1, 1)
        grid.addWidget(self.grp_df,     2, 1, 1, 1)
        # Overrides spans two columns
        grid.addWidget(self.grp_overrides, 3, 0, 1, 2)

        main = QtWidgets.QVBoxLayout(self)
        main.addLayout(meta_form)
        main.addLayout(grid)

        # --- Wiring ---
        # Adders
        self.bt_add_light.clicked.connect(lambda: self._add_files(self.lst_lights))
        self.bt_add_bias.clicked.connect(lambda: self._add_files(self.lst_biases))
        self.bt_add_dark.clicked.connect(lambda: self._add_files(self.lst_darks))
        self.bt_add_flat.clicked.connect(lambda: self._add_files(self.lst_flats))
        self.bt_add_df.clicked.connect(lambda: self._add_files(self.lst_df))

        # Removers
        self.bt_rm_light.clicked.connect(lambda: self._remove_selected(self.lst_lights))
        self.bt_rm_bias.clicked.connect(lambda: self._remove_selected(self.lst_biases))
        self.bt_rm_dark.clicked.connect(lambda: self._remove_selected(self.lst_darks))
        self.bt_rm_flat.clicked.connect(lambda: self._remove_selected(self.lst_flats))
        self.bt_rm_df.clicked.connect(lambda: self._remove_selected(self.lst_df))

        # Clearers
        self.bt_clr_light.clicked.connect(lambda: self._clear_all(self.lst_lights))
        self.bt_clr_bias.clicked.connect(lambda: self._clear_all(self.lst_biases))
        self.bt_clr_dark.clicked.connect(lambda: self._clear_all(self.lst_darks))
        self.bt_clr_flat.clicked.connect(lambda: self._clear_all(self.lst_flats))
        self.bt_clr_df.clicked.connect(lambda: self._clear_all(self.lst_df))

        # Override pickers
        self.bt_pick_mbias.clicked.connect(lambda: self._pick_file(self.ed_master_bias))
        self.bt_pick_mdark.clicked.connect(lambda: self._pick_file(self.ed_master_dark))
        self.bt_pick_mflat.clicked.connect(lambda: self._pick_file(self.ed_master_flat))
        self.bt_pick_mdf.clicked.connect(lambda: self._pick_file(self.ed_master_df))

        # Dirty tracking
        self.ed_session_name.textChanged.connect(self.changed.emit)
        self.ed_work_subdir.textChanged.connect(self.changed.emit)
        self.ed_master_bias.textChanged.connect(self.changed.emit)
        self.ed_master_dark.textChanged.connect(self.changed.emit)
        self.ed_master_flat.textChanged.connect(self.changed.emit)
        self.ed_master_df.textChanged.connect(self.changed.emit)

        for lst in (self.lst_lights, self.lst_biases, self.lst_darks, self.lst_flats, self.lst_df):
            m = lst.model()
            m.rowsInserted.connect(self._emit_changed)
            m.rowsRemoved.connect(self._emit_changed)
            m.modelReset.connect(self._emit_changed)  # important for .clear()
            lst.itemChanged.connect(self._emit_changed)

    @QtCore.pyqtSlot()
    def _emit_changed(self):
        self.changed.emit()

    # ---------- Utilities ----------
    def set_frame_groups_enabled(self, enabled: bool):
        """
        Enable/disable the frame list groups (Lights/Biases/Darks/Flats/Dark Flats)
        while leaving session metadata and master overrides editable.
        """
        for grp in (self.grp_lights, self.grp_biases, self.grp_darks, self.grp_flats, self.grp_df):
            grp.setEnabled(enabled)

    def _add_files(self, lst: QtWidgets.QListWidget):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add files")
        if not files:
            return
        for f in files:
            lst.addItem(f)
        self.changed.emit()

    def _remove_selected(self, lst: QtWidgets.QListWidget):
        for it in lst.selectedItems():
            row = lst.row(it)
            lst.takeItem(row)
        self.changed.emit()

    def _clear_all(self, lst: QtWidgets.QListWidget):
        lst.clear()
        self.changed.emit()

    def _pick_file(self, le: QtWidgets.QLineEdit):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Pick file")
        if f:
            le.setText(f)
            self.changed.emit()
 
    # ---------- Model sync ----------
    def from_session(self, s: "Session"):
        self.ed_session_name.setText(getattr(s, "name", "") or "")
        self.ed_work_subdir.setText(getattr(s, "work_subdir", "") or "")

        # accept plural or singular attribute names
        self._fill_list(self.lst_lights,     self._get_list(s, ("lights",)))
        self._fill_list(self.lst_biases,     self._get_list(s, ("biases", "bias")))
        self._fill_list(self.lst_darks,      self._get_list(s, ("darks", "dark")))
        self._fill_list(self.lst_flats,      self._get_list(s, ("flats", "flat")))
        self._fill_list(self.lst_df,         self._get_list(s, ("dark_flats", "dark_flat", "darkflat")))

        self.ed_master_bias.setText(getattr(s, "master_bias", "") or "")
        self.ed_master_dark.setText(getattr(s, "master_dark", "") or "")
        self.ed_master_flat.setText(getattr(s, "master_flat", "") or "")
        self.ed_master_df.setText(getattr(s, "master_dark_flat", "") or "")

    def to_session(self) -> "Session":
        # Name is required by your Session __init__
        name = self.ed_session_name.text().strip() or "Session"
        s = Session(name=name)

        # Optional subdir
        s.work_subdir = self.ed_work_subdir.text().strip() or None

        # Collect lists from UI
        lights     = self._items(self.lst_lights)
        biases     = self._items(self.lst_biases)
        darks      = self._items(self.lst_darks)
        flats      = self._items(self.lst_flats)
        dark_flats = self._items(self.lst_df)

        # Write both plural & singular for compatibility with the rest of your code
        s.lights = lights

        s.biases = biases
        s.bias   = biases

        s.darks  = darks
        s.dark   = darks

        s.flats  = flats
        s.flat   = flats

        s.dark_flats = dark_flats
        s.dark_flat  = dark_flats
        s.darkflat   = dark_flats  # in case older code referenced this

        # Per-session master overrides
        s.master_bias      = self.ed_master_bias.text().strip() or None
        s.master_dark      = self.ed_master_dark.text().strip() or None
        s.master_flat      = self.ed_master_flat.text().strip() or None
        s.master_dark_flat = self.ed_master_df.text().strip() or None

        return s


    # ---- helpers for plural/singular compatibility ----
    def _get_list(self, obj, names):
        """Return list from the first existing attribute in names; fall back to []."""
        for n in names:
            if hasattr(obj, n):
                v = getattr(obj, n)
                if v is None:
                    return []
                # ensure it's a list of strings
                return list(v)
        return []

    # helpers
    def _fill_list(self, lst: QtWidgets.QListWidget, paths: List[str]):
        lst.clear()
        for p in paths or []:
            lst.addItem(p)

    def _items(self, lst: QtWidgets.QListWidget) -> List[str]:
        return [lst.item(i).text() for i in range(lst.count())]
# =================================================================
# New: PanelEditor UI (embedded for single-file)
# =================================================================
class PanelEditor(QtWidgets.QWidget):
    """Right-hand editor for a single Panel's frame lists."""
    changed = QtCore.pyqtSignal()
    copy_cals_from_first_requested = QtCore.pyqtSignal()
    def __init__(self, parent=None):
        super().__init__(parent)

        self.ed_panel_id = QtWidgets.QLineEdit("A1")
        self.ed_desc = QtWidgets.QLineEdit()
        meta = QtWidgets.QFormLayout()
        meta.addRow("Panel ID", self.ed_panel_id)
        meta.addRow("Description", self.ed_desc)

        def make_group(title: str):
            box = QtWidgets.QGroupBox(title)
            v = QtWidgets.QVBoxLayout(box)
            lst = QtWidgets.QListWidget()
            lst.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.ExtendedSelection)
            v.addWidget(lst)
            h = QtWidgets.QHBoxLayout()
            add = QtWidgets.QPushButton("Add"); rm = QtWidgets.QPushButton("Remove"); clr = QtWidgets.QPushButton("Clear")
            h.addWidget(add); h.addWidget(rm); h.addWidget(clr); h.addStretch(1)
            v.addLayout(h)
            return box, lst, add, rm, clr

        self.grp_lights, self.lst_lights, self.bt_add_light, self.bt_rm_light, self.bt_clr_light = make_group("Lights")
        self.grp_biases, self.lst_biases, self.bt_add_bias, self.bt_rm_bias, self.bt_clr_bias = make_group("Biases")
        self.grp_darks,  self.lst_darks,  self.bt_add_dark, self.bt_rm_dark, self.bt_clr_dark = make_group("Darks")
        self.grp_flats,  self.lst_flats,  self.bt_add_flat, self.bt_rm_flat, self.bt_clr_flat = make_group("Flats")
        self.grp_df,     self.lst_df,     self.bt_add_df,   self.bt_rm_df,   self.bt_clr_df   = make_group("Dark Flats")

        grid = QtWidgets.QGridLayout()
        grid.addWidget(self.grp_lights, 0, 0, 1, 2)
        grid.addWidget(self.grp_biases, 1, 0)
        grid.addWidget(self.grp_darks,  1, 1)
        grid.addWidget(self.grp_flats,  2, 0)
        grid.addWidget(self.grp_df,     2, 1)

        # Button to copy calibration frames from the first panel in the session
        self._copy_source_panel_id: Optional[str] = None
        self.btn_copy_cals = QtWidgets.QPushButton()
        self.btn_copy_cals.setVisible(False)  # hidden until we know we can use it
        grid.addWidget(self.btn_copy_cals, 3, 0, 1, 2)

        main = QtWidgets.QVBoxLayout(self)
        main.addLayout(meta)
        main.addLayout(grid)

        # Wiring
        self.ed_panel_id.textChanged.connect(self.changed.emit)
        self.ed_desc.textChanged.connect(self.changed.emit)

        self.bt_add_light.clicked.connect(lambda: self._add_files(self.lst_lights))
        self.bt_add_bias.clicked.connect(lambda: self._add_files(self.lst_biases))
        self.bt_add_dark.clicked.connect(lambda: self._add_files(self.lst_darks))
        self.bt_add_flat.clicked.connect(lambda: self._add_files(self.lst_flats))
        self.bt_add_df.clicked.connect(lambda: self._add_files(self.lst_df))

        self.bt_rm_light.clicked.connect(lambda: self._remove_selected(self.lst_lights))
        self.bt_rm_bias.clicked.connect(lambda: self._remove_selected(self.lst_biases))
        self.bt_rm_dark.clicked.connect(lambda: self._remove_selected(self.lst_darks))
        self.bt_rm_flat.clicked.connect(lambda: self._remove_selected(self.lst_flats))
        self.bt_rm_df.clicked.connect(lambda: self._remove_selected(self.lst_df))

        self.bt_clr_light.clicked.connect(lambda: self._clear_all(self.lst_lights))
        self.bt_clr_bias.clicked.connect(lambda: self._clear_all(self.lst_biases))
        self.bt_clr_dark.clicked.connect(lambda: self._clear_all(self.lst_darks))
        self.bt_clr_flat.clicked.connect(lambda: self._clear_all(self.lst_flats))
        self.bt_clr_df.clicked.connect(lambda: self._clear_all(self.lst_df))

        for lst in (self.lst_lights, self.lst_biases, self.lst_darks, self.lst_flats, self.lst_df):
            m = lst.model()
            m.rowsInserted.connect(self._emit_changed)
            m.rowsRemoved.connect(self._emit_changed)
            m.modelReset.connect(self._emit_changed)
            lst.itemChanged.connect(self._emit_changed)

        self.btn_copy_cals.clicked.connect(self.copy_cals_from_first_requested)

    @QtCore.pyqtSlot()
    def _emit_changed(self):
        self.changed.emit()

    # Public API
    def from_panel(self, pan: Panel | None):
        for l in (self.lst_lights, self.lst_biases, self.lst_darks, self.lst_flats, self.lst_df):
            l.clear()
        if not pan:
            self.ed_panel_id.setText("")
            self.ed_desc.setText("")
            return
        self.ed_panel_id.setText(pan.panel_id or "")
        self.ed_desc.setText(pan.description or "")
        for lst, seq in (
            (self.lst_lights, pan.lights),
            (self.lst_biases, pan.bias),
            (self.lst_darks,  pan.darks),
            (self.lst_flats,  pan.flats),
            (self.lst_df,     pan.dark_flats),
        ):
            for x in seq:
                lst.addItem(x)

    def to_panel(self) -> Panel:
        pan = Panel()
        pan.panel_id   = self.ed_panel_id.text().strip() or "A1"
        pan.description= self.ed_desc.text().strip()
        pan.lights     = [self.lst_lights.item(i).text() for i in range(self.lst_lights.count())]
        pan.bias       = [self.lst_biases.item(i).text() for i in range(self.lst_biases.count())]
        pan.darks      = [self.lst_darks.item(i).text() for i in range(self.lst_darks.count())]
        pan.flats      = [self.lst_flats.item(i).text() for i in range(self.lst_flats.count())]
        pan.dark_flats = [self.lst_df.item(i).text() for i in range(self.lst_df.count())]
        return pan

    def set_copy_source_panel(self, panel_id: Optional[str], enabled: bool):
        """Update the label and enabled state of the 'copy calibration frames' button.

        panel_id:
            The panel ID of the source panel (typically the first panel in the session).
        enabled:
            Whether the action is logically available (e.g. mosaic is ON and there
            are at least two panels).
        """
        self._copy_source_panel_id = panel_id if panel_id else None

        if self._copy_source_panel_id:
            # Always show the button when we have a valid source panel,
            # but enable/disable it based on the `enabled` flag.
            self.btn_copy_cals.setText(
                f"Copy Calibration Frames from panel {self._copy_source_panel_id} to other panels"
            )
            self.btn_copy_cals.setVisible(True)
            self.btn_copy_cals.setEnabled(enabled)
        else:
            # No valid source panel → hide and disable the button
            self.btn_copy_cals.setVisible(False)
            self.btn_copy_cals.setEnabled(False)

    # Helpers
    def _add_files(self, lst: QtWidgets.QListWidget):
        files, _ = QtWidgets.QFileDialog.getOpenFileNames(self, "Add files")
        if not files:
            return
        for f in files:
            lst.addItem(f)
        self.changed.emit()

    def _remove_selected(self, lst: QtWidgets.QListWidget):
        for it in list(lst.selectedItems()):
            lst.takeItem(lst.row(it))
        self.changed.emit()

    def _clear_all(self, lst: QtWidgets.QListWidget):
        lst.clear()
        self.changed.emit()

    def set_frame_groups_enabled(self, enabled: bool):
        """
        Enable/disable the frame list groups (Lights/Biases/Darks/Flats/Dark Flats)
        while leaving panel metadata fields editable.
        """
        for grp in (self.grp_lights, self.grp_biases, self.grp_darks, self.grp_flats, self.grp_df):
            grp.setEnabled(enabled)

    def set_metadata_enabled(self, enabled: bool):
        """Enable/disable Panel metadata editing."""
        for w in (self.ed_panel_id, self.ed_desc):
            w.setEnabled(enabled)

class MosaicGraphicsView(QtWidgets.QGraphicsView):
    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self.setDragMode(QtWidgets.QGraphicsView.DragMode.ScrollHandDrag)
        self.setViewportUpdateMode(QtWidgets.QGraphicsView.ViewportUpdateMode.BoundingRectViewportUpdate)
        self._zoom = 0

    def wheelEvent(self, e: QtGui.QWheelEvent):
        # Smooth zoom on wheel
        factor = 1.15 if e.angleDelta().y() > 0 else (1/1.15)
        self.scale(factor, factor)
        e.accept()

class MosaicPreviewDialog(QtWidgets.QDialog):
    """
    Simple 2D grid preview of the mosaic layout with overlap, panel IDs,
    data status (empty/partial/full), and global reference highlight.
    """
    def __init__(self, *, rows:int, cols:int, overlap_pct:int,
                 name_scheme:int, global_ref_pid:Optional[str],
                 panel_status:Dict[str, Dict[str, int]],   # pid -> {"lights": n, "bias": n, ...}
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle("Preview Mosaic Layout")
        self.resize(820, 600)

        self.rows = max(1, int(rows))
        self.cols = max(1, int(cols))
        self.overlap = max(0.0, min(0.5, float(overlap_pct)/100.0))
        self.name_scheme = int(name_scheme)
        self.global_ref = global_ref_pid or None
        self.panel_status = panel_status or {}

        # Scene & view
        self.scene = QtWidgets.QGraphicsScene(self)
        self.view  = MosaicGraphicsView(self)
        self.view.setScene(self.scene)

        # Buttons row
        btn_copy = QtWidgets.QPushButton("Copy to Clipboard")
        btn_fit  = QtWidgets.QPushButton("Fit to View")
        btn_close= QtWidgets.QPushButton("Close")

        btns = QtWidgets.QHBoxLayout()
        btns.addStretch(1)
        btns.addWidget(btn_copy)
        btns.addWidget(btn_fit)
        btns.addWidget(btn_close)

        # Legend
        legend = QtWidgets.QLabel("Legend:  ■ Full (has lights & any cals)   ■ Partial (some frames)   ■ Empty")
        legend.setStyleSheet("color: #666;")
        legend.setAlignment(QtCore.Qt.AlignmentFlag.AlignLeft)

        lay = QtWidgets.QVBoxLayout(self)
        lay.addWidget(self.view, 1)
        lay.addWidget(legend)
        lay.addLayout(btns)

        btn_close.clicked.connect(self.accept)
        btn_fit.clicked.connect(self._fit)
        btn_copy.clicked.connect(self._copy)

        self._build_scene()
        self._fit()

    # --- helpers ---
    def _panel_id(self, r:int, c:int)->str:
        if self.name_scheme == 0:
            return f"{chr(ord('A')+r)}{c+1}"
        return f"R{r+1}C{c+1}"

    def _brush_for(self, pid:str)->QtGui.QBrush:
        st = self.panel_status.get(pid, {})
        total = sum(int(v) for v in st.values())
        lights = int(st.get("lights", 0))
        if total == 0:
            # empty
            pat = QtCore.Qt.BrushStyle.Dense6Pattern
            b = QtGui.QBrush(QtGui.QColor(200,200,200), pat)
            return b
        if lights > 0 and total > lights:
            # full (has lights + at least one cal type)
            return QtGui.QBrush(QtGui.QColor(120,170,255,180))
        # partial (some frames but maybe missing cals OR only lights)
        return QtGui.QBrush(QtGui.QColor(240,190,90,180))

    def _build_scene(self):
        self.scene.clear()
        tile_w = 200.0
        tile_h = 200.0
        step_x = tile_w * (1.0 - self.overlap)
        step_y = tile_h * (1.0 - self.overlap)

        thin_pen  = QtGui.QPen(QtGui.QColor(60,60,60)); thin_pen.setWidthF(1.0)
        ref_pen   = QtGui.QPen(QtGui.QColor(40,180,80)); ref_pen.setWidth(3)

        font = QtGui.QFont()
        font.setPointSize(11)
        font_bold = QtGui.QFont(font); font_bold.setBold(True)

        # Draw tiles
        for r in range(self.rows):
            for c in range(self.cols):
                pid = self._panel_id(r, c)
                x = c * step_x
                y = r * step_y

                rect_item = self.scene.addRect(x, y, tile_w, tile_h, thin_pen, self._brush_for(pid))

                # label (centered)
                label = self.scene.addText(pid, font_bold)
                br = label.boundingRect()
                label.setPos(x + tile_w/2 - br.width()/2, y + tile_h/2 - br.height()/2)

                # counts line under the label
                st = self.panel_status.get(pid, {})
                if st:
                    counts = " | ".join([f"L:{int(st.get('lights',0))}",
                                         f"F:{int(st.get('flats',0))}",
                                         f"B:{int(st.get('bias',0))}",
                                         f"D:{int(st.get('darks',0))}",
                                         f"DF:{int(st.get('dark_flats',0))}"])
                    sub = self.scene.addText(counts, font)
                    sub_br = sub.boundingRect()
                    sub.setPos(x + tile_w/2 - sub_br.width()/2, y + tile_h*0.62)

                # global ref highlight
                if self.global_ref and pid == self.global_ref:
                    self.scene.addRect(x, y, tile_w, tile_h, ref_pen)
                    star = self.scene.addText("★", font_bold)
                    star.setDefaultTextColor(QtGui.QColor(40,180,80))
                    star.setPos(x + tile_w - 22, y + 4)

        # Feather hints (overlap bands)
        if self.overlap > 0:
            alpha = 60
            band_col = QtGui.QColor(0,0,0,alpha)
            band_pen = QtGui.QPen(QtCore.Qt.PenStyle.NoPen)
            # vertical bands between columns
            for r in range(self.rows):
                for c in range(self.cols-1):
                    x = (c+1)*step_x
                    y = r*step_y
                    w = tile_w*self.overlap
                    self.scene.addRect(x, y, w, tile_h, band_pen, QtGui.QBrush(band_col))
            # horizontal bands between rows
            for r in range(self.rows-1):
                for c in range(self.cols):
                    x = c*step_x
                    y = (r+1)*step_y
                    h = tile_h*self.overlap
                    self.scene.addRect(x, y, tile_w, h, band_pen, QtGui.QBrush(band_col))

        # Canvas scale badge if not 1.0 is handled by caller subtitle (optional)

    def _fit(self):
        self.view.fitInView(self.scene.itemsBoundingRect(), QtCore.Qt.AspectRatioMode.KeepAspectRatio)

    def _copy(self):
        # Render scene to an image and put on clipboard
        rect = self.scene.itemsBoundingRect()
        img = QtGui.QImage(int(rect.width())+8, int(rect.height())+8, QtGui.QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(QtGui.QColor(255,255,255,0))
        painter = QtGui.QPainter(img)
        painter.setRenderHint(QtGui.QPainter.RenderHint.Antialiasing, True)
        self.scene.render(painter, QtCore.QRectF(img.rect()), rect.adjusted(-4, -4, 4, 4))
        painter.end()
        QtWidgets.QApplication.clipboard().setImage(img)

    def showEvent(self, e: QtGui.QShowEvent) -> None:
        super().showEvent(e)
        # Fit once after the widget is on screen so the viewport has real size.
        QtCore.QTimer.singleShot(0, self._fit)


class ProjectWidget(QtWidgets.QWidget):
    status_message = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = Project()
        self._dirty = False
        self._suspend_dirty = False
        self._loading_session = False
        self._loading_panel = False

        # Siril console bridge
        self.siril = SirilConsoleBridge()
        if self.siril.connected:
            self.siril.log(
                "[Multi-Night Stacking] Connected to Siril Python API.",
                s.LogColor.GREEN,
            )
            self.siril.log(
                "Support for the provided OSC Multi-Night Stacking script is provided "
                "by Roland Teague and not by the Siril developers.",
                s.LogColor.GREEN,
            )
            self.siril.log(
                "Please reach out to me on Facebook for any questions or open an "
                "issue report on Github to report any bugs.",
                s.LogColor.GREEN,
            )
            self.siril.log(
                "Facebook Profile: https://www.facebook.com/roland.teague.9/",
                s.LogColor.GREEN,
            )
            self.siril.log(
                "Github Repo: https://github.com/rolandet/siril-scripts",
                s.LogColor.GREEN,
            )
        else:
            print(
                "[Multi-Night Stacking] Support for this script is provided by Roland Teague."
                "Please reach out to Roland on Facebook for any questions or open an "
                "issue report on GitHub to report any bugs."
                "Facebook Profile: https://www.facebook.com/roland.teague.9/"
                "GitHub Repo: https://github.com/rolandet/siril-scripts"                
            )

        # Process bookkeeping
        self._proc: Optional[subprocess.Popen] = None
        self._run_timer: Optional[QtCore.QTimer] = None
        self._run_started_at: Optional["datetime"] = None
        self._run_siril_verstr: Optional[str] = None

        # ---------------- Top-level controls ----------------
        self.ed_name     = QtWidgets.QLineEdit(self.project.name)
        self.ed_workdir  = QtWidgets.QLineEdit()
        self.btn_workdir = QtWidgets.QPushButton("Browse…")

        self.cb_use_library = QtWidgets.QCheckBox("Use Siril Master Library (project-level)")
        self.cb_use_library.setChecked(True)

        self.cb_allow_uncal = QtWidgets.QCheckBox("Allow no calibration frames")  # NEW
        self.cb_allow_uncal.setChecked(False)       

        # New: Remember window size checkbox (configurable)
        self.cb_remember_size = QtWidgets.QCheckBox("Remember last window size")
        self.cb_remember_size.setChecked(False)

        # Drizzle controls
        self.cb_drizzle   = QtWidgets.QCheckBox("Enable Drizzle")
        self.lbl_scaling  = QtWidgets.QLabel("Scaling")
        self.spin_scaling = QtWidgets.QDoubleSpinBox()
        self.spin_scaling.setRange(0.1, 3.0); self.spin_scaling.setSingleStep(0.1); self.spin_scaling.setValue(1.0)

        self.lbl_pixfrac  = QtWidgets.QLabel("Pixel Fraction")
        self.spin_pixfrac = QtWidgets.QDoubleSpinBox()
        self.spin_pixfrac.setRange(0.0, 1.0); self.spin_pixfrac.setSingleStep(0.05); self.spin_pixfrac.setValue(1.0)

        self.lbl_kernel   = QtWidgets.QLabel("Kernel")
        self.cb_kernel    = QtWidgets.QComboBox()
        self.cb_kernel.addItems(["square", "point", "turbo", "gaussian", "lanczos2", "lanczos3"])

        self.cb_two_pass  = QtWidgets.QCheckBox("Use 2-pass registration")
        self.cb_two_pass.setToolTip("Computes transforms in pass #1, applies in pass #2.\nEnable for challenging datasets or drizzle if desired.")

        # Stacking controls (global)
        # Stacking controls (global)
        self.cb_stack_method = QtWidgets.QComboBox()
        self.cb_stack_method.addItems([
            "Winsorized Rejection",  # default
            "Sigma Rejection",
            "Mean",
            "Median"
        ])
        self.cb_stack_method.setCurrentIndex(0)

        # map UI index -> Siril token
        self._stack_method_map = {0: "rej", 1: "rej", 2: "wrej", 3: "mean", 4: "median"}
        self._stack_method_rev = {v: k for k, v in self._stack_method_map.items()}
        self.cb_stack_method.setCurrentIndex(0)  # default to Rejection

        self.lbl_sigma_low  = QtWidgets.QLabel("Sigma Low")
        self.dbl_sigma_low  = QtWidgets.QDoubleSpinBox()
        self.dbl_sigma_low.setRange(0.1, 10.0);  self.dbl_sigma_low.setSingleStep(0.1);  self.dbl_sigma_low.setValue(3.0)

        self.lbl_sigma_high = QtWidgets.QLabel("Sigma High")
        self.dbl_sigma_high = QtWidgets.QDoubleSpinBox()
        self.dbl_sigma_high.setRange(0.1, 10.0); self.dbl_sigma_high.setSingleStep(0.1); self.dbl_sigma_high.setValue(3.0)

        # Under Stacking
        self.cb_stack_32 = QtWidgets.QCheckBox("32-bit Output for Final Stack")
        self.cb_stack_32.setToolTip("Writes the final LIGHTS stack as 32-bit FITS (-32b).")
        self.cb_compress = QtWidgets.QCheckBox("Compress Intermediates (Lossless)")
        self.cb_compress.setToolTip("Use lossless FITS tile compression for intermediates to save disk space.")

        # siril-cli path
        self.ed_siril = QtWidgets.QLineEdit()
        self.btn_siril = QtWidgets.QPushButton("Find…")
        self.cb_force_cli = QtWidgets.QCheckBox("Force siril-cli (ignore Python API)")

        self.ed_siril.setToolTip(
            "Optional: Path to siril-cli.\n"
            "By default, when Siril is running and the Python API is connected, "
            "runs execute inside Siril.\n"
            "If 'Force siril-cli' is checked, the Run button will always use siril-cli."
        )
        self.btn_siril.setToolTip(
            "Browse for siril-cli. This is used when running outside of Siril, "
            "or when 'Force siril-cli' is enabled."
        )
        self.cb_force_cli.setToolTip(
            "If checked, always run via siril-cli even when the Siril Python API is available.\n"
            "Abort only works in CLI mode; for in-Siril runs, use Siril's Stop button."
        )

        # Sessions list + editor (existing)
        self.sessions_list       = QtWidgets.QListWidget()
        self.btn_add_sess        = QtWidgets.QPushButton("Add Session")
        self.btn_remove_sess     = QtWidgets.QPushButton("Remove Session")
        self.btn_dup_sess        = QtWidgets.QPushButton("Duplicate Session")
        self.btn_remove_data_all = QtWidgets.QPushButton("Remove Data (All Sessions)")
        self.btn_remove_data_all.setToolTip(
            "Delete all on-disk temporary data for every session in this project.\n"
            "Keeps the project configuration and session definitions in the UI.\n"
            "Use when you want to re-run processing from scratch without losing setup."
        )
        self.session_editor      = SessionEditor()

        # NEW: Panels list + buttons (per-session)
        self.lst_panels       = QtWidgets.QListWidget()
        self.btn_add_panel    = QtWidgets.QPushButton("Add Panel")
        self.btn_remove_panel = QtWidgets.QPushButton("Remove Panel")
        self.lst_panels.currentRowChanged.connect(self.load_selected_panel)

        # NEW: Panel editor (right tab)
        self.panel_editor = PanelEditor()
        self.panel_editor.changed.connect(self.update_current_panel)
        self.panel_editor.changed.connect(self.mark_dirty)
        self.panel_editor.copy_cals_from_first_requested.connect(self._on_copy_cals_from_first_panel)

        # Bottom actions
        self.btn_prepare     = QtWidgets.QPushButton("Prepare Working Directory (Symlink/Copy Files)")
        self.btn_prepare.setToolTip(
            "Creates the required temporary directory structure for the project.\n"
            "Copies/symlinks image files into per-session folders,\n"
            "and initializes log files. Must be run before building Siril scripts."
        )        
        self.btn_build_script= QtWidgets.QPushButton("Build Siril Script")
        self.btn_build_script.setToolTip(
            "Generates the Siril .ssf script for the project.\n"
            "Run this after preparing the working directory and defining sessions/panels."
        )        
        self.btn_run_siril   = QtWidgets.QPushButton("Run Siril Script")
        self.btn_run_siril.setToolTip(
            "Executes the created .ssf script for the project using the Siril Python API.\n"
            "Script execution will use the siril-cli as fallback or if forced.\n"
            "Run this only after building the Siril scripts."
        )        
        self.btn_abort       = QtWidgets.QPushButton("Abort Run")
        self.btn_abort.setEnabled(False)
        self.btn_abort.setToolTip(
            "Abort only works for CLI runs.\n"
            "For in-Siril runs started via the Python API, use Siril's Stop button."
        )

        # Run mode label
        self.lbl_run_mode = QtWidgets.QLabel("Run mode: not started")
        self.lbl_run_mode.setToolTip(
            "Shows whether the last run used the Siril Python API or siril-cli."
        )
        self.lbl_run_mode.setStyleSheet("color: #666666; font-style: italic;")

        # ---------------- Left column layout (with boxes) ----------------
        left_form = QtWidgets.QFormLayout()
        left_form.addRow("Project Name", self.ed_name)

        work_row = QtWidgets.QHBoxLayout()
        work_row.addWidget(self.ed_workdir, 1)
        work_row.addWidget(self.btn_workdir)
        left_form.addRow("Working Directory", work_row)
        row_lib = QtWidgets.QHBoxLayout()
        row_lib.addWidget(self.cb_use_library)
        row_lib.addSpacing(16)
        row_lib.addWidget(self.cb_allow_uncal)   # NEW
        row_lib.addSpacing(16)
        row_lib.addWidget(self.cb_remember_size)
        row_lib.addStretch(1)
        left_form.addRow("", row_lib)


        # DRIZZLE (boxed)
        drizzle_box  = QtWidgets.QGroupBox("Drizzle")
        drizzle_form = QtWidgets.QFormLayout(drizzle_box)
        drizzle_form.addRow("", self.cb_drizzle)
        drow = QtWidgets.QHBoxLayout()
        drow.addWidget(self.lbl_scaling);  drow.addWidget(self.spin_scaling)
        drow.addSpacing(12)
        drow.addWidget(self.lbl_pixfrac);  drow.addWidget(self.spin_pixfrac)
        drow.addSpacing(12)
        drow.addWidget(self.lbl_kernel);   drow.addWidget(self.cb_kernel)
        drow.addStretch(1)
        drizzle_form.addRow("", drow)
        drizzle_form.addRow("", self.cb_two_pass)

        # STACKING (boxed)
        stack_box  = QtWidgets.QGroupBox("Stacking")
        stack_form = QtWidgets.QFormLayout(stack_box)
        mrow = QtWidgets.QHBoxLayout()
        mrow.addWidget(QtWidgets.QLabel("Method"))
        mrow.addWidget(self.cb_stack_method)
        mrow.addStretch(1)
        stack_form.addRow("", mrow)
        srow = QtWidgets.QHBoxLayout()
        srow.addWidget(self.lbl_sigma_low);  srow.addWidget(self.dbl_sigma_low)
        srow.addSpacing(12)
        srow.addWidget(self.lbl_sigma_high); srow.addWidget(self.dbl_sigma_high)
        srow.addStretch(1)
        stack_form.addRow("", srow)
        optrow = QtWidgets.QHBoxLayout()
        optrow.addWidget(self.cb_stack_32)
        optrow.addSpacing(18)
        optrow.addWidget(self.cb_compress)
        optrow.addStretch(1)
        stack_form.addRow("", optrow)

        # --- Pack sequences controls (existing behaviour preserved) ---
        _pack_form = self.cb_stack_32.parentWidget().layout()
        row_widget = QtWidgets.QWidget(self)
        row = QtWidgets.QHBoxLayout(row_widget); row.setContentsMargins(0, 0, 0, 0)
        self.pack_label  = QtWidgets.QLabel("Pack sequences:", self)
        self.pack_mode   = QtWidgets.QComboBox(self)
        self.pack_mode.addItems(["Off", "FITSEQ", "SER", "Auto when > N"])
        self.pack_mode.setToolTip("Use FITSEQ/SER to avoid OS open-file limits on very large sequences")
        self.pack_thresh = QtWidgets.QSpinBox(self)
        self.pack_thresh.setRange(100, 10000); self.pack_thresh.setValue(2000)
        self.pack_thresh.setToolTip("Threshold in Auto mode (total light frames that triggers packing)")
        self._toggle_pack_thresh()
        self.pack_mode.currentIndexChanged.connect(self._toggle_pack_thresh)
        row.addWidget(self.pack_label); row.addSpacing(8); row.addWidget(self.pack_mode)
        row.addSpacing(12); row.addWidget(self.pack_thresh); row.addStretch(1)
        if isinstance(_pack_form, QtWidgets.QFormLayout):
            _pack_form.addRow(row_widget)
        else:
            _pack_form.addWidget(row_widget)

        # siril-cli path row
        sr = QtWidgets.QHBoxLayout()
        sr.addWidget(self.ed_siril, 1)
        sr.addWidget(self.btn_siril)
        left_form.addRow("siril-cli Path (optional)", sr)
        left_form.addRow(self.cb_force_cli)

        # Add Drizzle & Stacking boxes
        left_form.addRow(drizzle_box)
        left_form.addRow(stack_box)
        for gb in (drizzle_box, stack_box):
            gb.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                             QtWidgets.QSizePolicy.Policy.Fixed)

        # ========== NEW: MOSAIC Processing (boxed) ==========
        mosaic_box  = QtWidgets.QGroupBox("Mosaic Processing (Experimental)")
        mosaic_form = QtWidgets.QGridLayout(mosaic_box)

        self.chk_mosaic_enabled = QtWidgets.QCheckBox("Enable Mosaic Mode")
        mosaic_form.addWidget(self.chk_mosaic_enabled, 0, 0, 1, 4)

        mosaic_form.addWidget(QtWidgets.QLabel("Grid Layout:"), 1, 0)
        self.sp_mosaic_rows = QtWidgets.QSpinBox();  self.sp_mosaic_rows.setRange(1, 20)
        self.sp_mosaic_cols = QtWidgets.QSpinBox();  self.sp_mosaic_cols.setRange(1, 20)
        self.sp_mosaic_overlap = QtWidgets.QSpinBox(); self.sp_mosaic_overlap.setRange(0, 50); self.sp_mosaic_overlap.setSuffix(" %")
        gl = QtWidgets.QHBoxLayout()
        gl.addWidget(QtWidgets.QLabel("Rows"));    gl.addWidget(self.sp_mosaic_rows)
        gl.addSpacing(8)
        gl.addWidget(QtWidgets.QLabel("Columns")); gl.addWidget(self.sp_mosaic_cols)
        gl.addSpacing(8)
        gl.addWidget(QtWidgets.QLabel("Overlap")); gl.addWidget(self.sp_mosaic_overlap)
        glw = QtWidgets.QWidget(); glw.setLayout(gl)
        mosaic_form.addWidget(glw, 1, 1, 1, 3)

        mosaic_form.addWidget(QtWidgets.QLabel("Global Reference:"), 2, 0)
        self.cmb_mosaic_ref = QtWidgets.QComboBox(); self.cmb_mosaic_ref.setPlaceholderText("Select session/frame…")
        mosaic_form.addWidget(self.cmb_mosaic_ref, 2, 1, 1, 3)

        mosaic_form.addWidget(QtWidgets.QLabel("Canvas Scale:"), 3, 0)
        self.dsb_mosaic_scale = QtWidgets.QDoubleSpinBox(); self.dsb_mosaic_scale.setRange(0.25, 4.0); self.dsb_mosaic_scale.setSingleStep(0.05); self.dsb_mosaic_scale.setValue(1.0)
        mosaic_form.addWidget(self.dsb_mosaic_scale, 3, 1)

        mosaic_form.addWidget(QtWidgets.QLabel("Registration Mode:"), 3, 2)
        self.cmb_mosaic_reg = QtWidgets.QComboBox(); self.cmb_mosaic_reg.addItems(["Two-pass", "One-pass"])
        mosaic_form.addWidget(self.cmb_mosaic_reg, 3, 3)

        #mosaic_form.addWidget(QtWidgets.QLabel("Mosaic Stacking Method:"), 4, 0)
        #self.cmb_mosaic_stack = QtWidgets.QComboBox(); self.cmb_mosaic_stack.addItems(["Mean", "Winsorized", "Sigma", "Median"])
        #mosaic_form.addWidget(self.cmb_mosaic_stack, 4, 1, 1, 3)
        #self.cmb_mosaic_stack.setVisible(False)

        self.chk_mosaic_bg = QtWidgets.QCheckBox("Panel Background Extraction")
        self.chk_mosaic_bg.setToolTip(
            "If enabled, runs 'seqsubsky pp_light 1' for each panel right after calibration.\n"
            "This produces 'bkg_pp_light' and we then register/stack that sequence.\n"
            "Disable to register/stack the calibrated 'pp_light' sequence directly."
        )
        self.chk_mosaic_maximize = QtWidgets.QCheckBox("Maximize Framing")
        self.chk_mosaic_maximize.setToolTip(
            "When enabled, Siril preserves the full mosaic canvas during registration and stacking.\n"
            "This maps to: seqapplyreg -framing=max (Phase 2) and stack -maximize (Phase 1B/Phase 2)."
        )
        self.chk_mosaic_overlap_norm = QtWidgets.QCheckBox("Normalize on Overlaps")
        self.chk_mosaic_overlap_norm.setToolTip(
            "When enabled, Siril computes relative normalization using the regions where panels overlap.\n"
            "Useful for evening out background/brightness differences between panels."
        )
        mosaic_form.addWidget(self.chk_mosaic_bg, 5, 0, 1, 4)
        mosaic_form.addWidget(self.chk_mosaic_maximize, 7, 2, 1, 2)
        mosaic_form.addWidget(self.chk_mosaic_overlap_norm, 7, 0, 1, 2)

        mosaic_form.addWidget(QtWidgets.QLabel("Borders Feathering:"), 6, 0)
        self.sp_mosaic_feather = QtWidgets.QSpinBox(); self.sp_mosaic_feather.setRange(0, 500); self.sp_mosaic_feather.setSuffix(" px"); self.sp_mosaic_feather.setValue(50)
        mosaic_form.addWidget(self.sp_mosaic_feather, 6, 1)
        self.chk_link_feather = QtWidgets.QCheckBox("Link feather to Overlap %")
        self.chk_link_feather.setToolTip(
            "When enabled and Prepare Working Directory is run succesfully, Borders Feathering (px) is estimated from Overlap % and frame size.\n"
            "Disable to set feathering manually (default 50 px)."
        )
        mosaic_form.addWidget(self.chk_link_feather, 6, 2, 1, 2)

        self.chk_mosaic_drizzle_panel = QtWidgets.QCheckBox("Drizzle per panel")
        self.chk_mosaic_drizzle_panel.setToolTip(
            "Enable drizzle during per-panel registration.\n"
            "Selecting this option forces Two-pass registration (required for drizzle)."
        )
        mosaic_form.addWidget(self.chk_mosaic_drizzle_panel, 8, 0, 1, 2)

        # --- Grid-driven panel management ---
        self.chk_auto_grid = QtWidgets.QCheckBox("Auto-manage panels by grid (advanced)")
        self.chk_auto_grid.setToolTip(
            "Automatically create, name, and position panels using the grid dimensions "
            "and overlap percentage above.\n"
            "When disabled, panels can be added, removed, or repositioned manually."
        )
        self.btn_gen_grid  = QtWidgets.QPushButton("Generate panels from grid…")
        self.cmb_grid_scope = QtWidgets.QComboBox()
        self.cmb_grid_scope.addItems(["Selected session", "All sessions"])
        self.cmb_name_scheme = QtWidgets.QComboBox()
        self.cmb_name_scheme.addItems(["RowLetter+ColNumber (A1, B2…)", "R#C# (R1C2…)"])

        grid_line = QtWidgets.QHBoxLayout()
        grid_line.addWidget(self.btn_gen_grid)
        grid_line.addWidget(QtWidgets.QLabel("Scope:"))
        grid_line.addWidget(self.cmb_grid_scope)
        grid_line.addWidget(QtWidgets.QLabel("Names:"))
        grid_line.addWidget(self.cmb_name_scheme)

        mosaic_form.addWidget(self.chk_auto_grid, 8, 2, 1, 2)
        mosaic_form.addLayout(grid_line, 9, 0, 1, 4)

        self.btn_preview_mosaic = QtWidgets.QPushButton("Preview Mosaic Layout…")
        mosaic_form.addWidget(self.btn_preview_mosaic, 10, 0, 1, 4)

        left_form.addRow(mosaic_box)  # <<< placed between Stacking and Sessions

        # ========== Sessions + Panels side-by-side (boxed) ==========
        container = QtWidgets.QWidget()
        sp_h = QtWidgets.QHBoxLayout(container)

        # Sessions (existing)
        sessions_box = QtWidgets.QGroupBox("Sessions")
        sessions_box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                           QtWidgets.QSizePolicy.Policy.Preferred)
        sv = QtWidgets.QVBoxLayout(sessions_box)
        sv.addWidget(self.sessions_list, 1)
        sbtns = QtWidgets.QHBoxLayout()
        sbtns.addWidget(self.btn_add_sess)
        sbtns.addWidget(self.btn_remove_sess)
        sbtns.addWidget(self.btn_dup_sess)
        sbtns.addStretch(1)
        sbtns.addWidget(self.btn_remove_data_all)
        sv.addLayout(sbtns)

        # Panels (new)
        panels_box = QtWidgets.QGroupBox("Panels (per session)")
        self.grp_panels = panels_box
        panels_box.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred,
                         QtWidgets.QSizePolicy.Policy.Preferred)
        pv = QtWidgets.QVBoxLayout(panels_box)
        pv.addWidget(self.lst_panels, 1)
        pbtns = QtWidgets.QHBoxLayout()
        pbtns.addWidget(self.btn_add_panel)
        pbtns.addWidget(self.btn_remove_panel)
        pv.addLayout(pbtns)

        sp_h.addWidget(sessions_box, 1)
        sp_h.addWidget(panels_box, 1)
        left_form.addRow(container)

        # ---------------- Splitter (left/right) ----------------

        # 1) LEFT column content → put your existing left_form into a widget
        left_col = QtWidgets.QVBoxLayout()
        left_col.addLayout(left_form)

        left_container = QtWidgets.QWidget()
        left_container.setLayout(left_col)

        left_scroll = QtWidgets.QScrollArea()
        left_scroll.setWidgetResizable(True)
        left_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        left_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)  # NEW
        left_scroll.setWidget(left_container)
        # Make the left column comfortably wide
        left_scroll.setMinimumWidth(560)
        left_scroll.setMaximumWidth(780)

        # 2) RIGHT tabs must exist before we wrap them
        # (Make sure session_editor and panel_editor were created above this)
        self.right_tabs = QtWidgets.QTabWidget()
        self.right_tabs.addTab(self.session_editor, "Session")
        self.right_tabs.addTab(self.panel_editor, "Panel")

        right_scroll = QtWidgets.QScrollArea()
        right_scroll.setWidgetResizable(True)
        right_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        right_scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarPolicy.ScrollBarAsNeeded)  # NEW
        right_scroll.setWidget(self.right_tabs)

        # 3) Splitter
        splitter = QtWidgets.QSplitter()
        splitter.addWidget(left_scroll)   # LEFT
        splitter.addWidget(right_scroll)  # RIGHT

        # Favor the LEFT pane growth so it starts larger and stays roomy
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 0)
        self._init_splitter_sizes(splitter)

        splitter.setCollapsible(0, False)
        splitter.setCollapsible(1, False)
        splitter.setHandleWidth(6)

        # (then add splitter to your main layout as you already do)

        # ---------------- Bottom bar ----------------
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.btn_prepare)
        bottom.addWidget(self.btn_build_script)
        bottom.addWidget(self.lbl_run_mode)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run_siril)
        bottom.addWidget(self.btn_abort)

        # ---------------- Main layout ----------------
        main = QtWidgets.QVBoxLayout(self)
        main.addWidget(splitter)
        main.addLayout(bottom)
        self._set_initial_list_heights(rows=6)

        # ---------------- Wiring ----------------
        self.btn_workdir.clicked.connect(self.pick_workdir)
        self.btn_siril.clicked.connect(self.pick_siril)
        self.cb_remember_size.toggled.connect(self.mark_dirty)
        self.cb_force_cli.toggled.connect(self.mark_dirty)

        self.cb_drizzle.toggled.connect(self._toggle_drizzle_opts)
        self.cb_drizzle.toggled.connect(self.mark_dirty)
        self.spin_scaling.valueChanged.connect(self.mark_dirty)
        self.spin_pixfrac.valueChanged.connect(self.mark_dirty)
        self.cb_kernel.currentIndexChanged.connect(self.mark_dirty)
        self.cb_two_pass.toggled.connect(self.mark_dirty)

        self.cb_stack_method.currentIndexChanged.connect(self._toggle_sigma_by_method)
        self.cb_stack_method.currentIndexChanged.connect(self.mark_dirty)
        self.dbl_sigma_low.valueChanged.connect(self.mark_dirty)
        self.dbl_sigma_high.valueChanged.connect(self.mark_dirty)
        self.cb_stack_32.toggled.connect(self.mark_dirty)
        self.cb_compress.toggled.connect(self.mark_dirty)

        self.pack_mode.currentIndexChanged.connect(self.mark_dirty)
        self.pack_thresh.valueChanged.connect(self.mark_dirty)

        for w in (self.ed_name, self.ed_workdir, self.ed_siril):
            w.textChanged.connect(self.mark_dirty)
        self.cb_use_library.toggled.connect(self.mark_dirty)
        self.cb_allow_uncal.toggled.connect(self.mark_dirty)  # NEW

        self.btn_add_sess.clicked.connect(self.add_session)
        self.btn_remove_sess.clicked.connect(self.remove_session)
        self.btn_dup_sess.clicked.connect(self.duplicate_session)
        self.btn_remove_data_all.clicked.connect(self.remove_all_sessions_data)

        self.sessions_list.currentRowChanged.connect(self.load_selected_session)
        self.session_editor.changed.connect(self.update_current_session)
        self.session_editor.changed.connect(self.mark_dirty)

        self.btn_prepare.clicked.connect(self.prepare_working_dir)
        self.btn_build_script.clicked.connect(self.build_script)
        self.btn_run_siril.clicked.connect(self.run_siril)
        self.btn_abort.clicked.connect(self.abort_siril)

        # NEW: Mosaic & Panels signals
        self.chk_mosaic_enabled.toggled.connect(self._on_mosaic_toggled)
        self.chk_mosaic_enabled.toggled.connect(self.mark_dirty)
        self.sp_mosaic_rows.valueChanged.connect(self.mark_dirty)
        self.sp_mosaic_cols.valueChanged.connect(self.mark_dirty)
        #self.sp_mosaic_overlap.valueChanged.connect(self.mark_dirty)
        self.cmb_mosaic_ref.currentIndexChanged.connect(self.mark_dirty)
        self.dsb_mosaic_scale.valueChanged.connect(self.mark_dirty)
        self.cmb_mosaic_reg.currentIndexChanged.connect(self.mark_dirty)
        # Keep 2-pass UI locks consistent when user changes Mosaic registration mode
        self.cmb_mosaic_reg.currentIndexChanged.connect(self._sync_drizzle_two_pass_locks)
        # When overlap % changes, recompute feather if linked
        self.sp_mosaic_overlap.valueChanged.connect(self._on_overlap_pct_changed)
        # When link checkbox toggled, recompute once immediately
        self.chk_link_feather.toggled.connect(self._on_overlap_pct_changed)
        # Mark dirty when user changes feather manually
        self.sp_mosaic_feather.valueChanged.connect(self.mark_dirty)        
        self.chk_mosaic_bg.toggled.connect(self.mark_dirty)
        self.chk_mosaic_maximize.toggled.connect(self.mark_dirty)
        self.chk_mosaic_overlap_norm.toggled.connect(self.mark_dirty)
        #self.sp_mosaic_feather.valueChanged.connect(self.mark_dirty)
        self.chk_mosaic_drizzle_panel.toggled.connect(self.mark_dirty)
        self.chk_mosaic_drizzle_panel.toggled.connect(self._sync_drizzle_two_pass_locks)

        self.btn_add_panel.clicked.connect(self._on_add_panel)
        self.btn_remove_panel.clicked.connect(self._on_remove_panel)

        self.btn_gen_grid.clicked.connect(self._on_generate_panels_from_grid)
        self.sp_mosaic_rows.valueChanged.connect(self._on_grid_changed)
        self.sp_mosaic_cols.valueChanged.connect(self._on_grid_changed)
        self.chk_auto_grid.toggled.connect(self._on_grid_changed)
        self.btn_preview_mosaic.clicked.connect(self._on_preview_mosaic)

        # menu actions (used by MainWindow)
        self.action_new     = QtGui.QAction("New Project", self)
        self.action_open    = QtGui.QAction("Open Project…", self)
        self.action_save    = QtGui.QAction("Save", self)
        self.action_save_as = QtGui.QAction("Save As…", self)

        # Initialize from model
        self.refresh_from_model()
        if not self.project.sessions:
            self.project.sessions = [Session(name="Session 1")]
            self.refresh_from_model()
        # Default Working Directory to Siril's current dir (once) if empty
        if not (self.project.working_dir or self.ed_workdir.text()):
            self._suspend_dirty = True
            try:
                siril_dir = self._detect_siril_home_dir()
                if not siril_dir:
                    # fallback: user home
                    siril_dir = os.path.expanduser("~")
                self.ed_workdir.setText(siril_dir)
            finally:
                self._suspend_dirty = False

        self._toggle_drizzle_opts(self.project.drizzle_enabled)
        self._toggle_sigma_by_method(self.cb_stack_method.currentIndex())

    # ---------------- helpers ----------------
    def _detect_siril_home_dir(self) -> str | None:
        """
        Resolve a sensible default working directory:

        1) If we're running from Siril (sirilpy connected), ask the live Siril console for `pwd`.
        2) Otherwise, try `siril-cli` by running a tiny temp script that prints `pwd`.
        3) Fallback to os.getcwd(), then user home as a last resort.

        Returns a native-OS path string or None.
        """
        def _parse_pwd(out: str) -> str | None:
            if not out:
                return None
            # Use the last non-empty line; strip common noise.
            lines = [ln.strip() for ln in str(out).splitlines() if ln.strip()]
            if not lines:
                return None
            last = lines[-1]
            # Some builds prefix "CWD:"; also trim quotes.
            last = last.replace("CWD:", "").strip().strip('"').strip("'")
            # Normalize slashes and remove trailing separators
            last = os.path.normpath(last)
            return last if last else None

        # --- 1) Live Siril via sirilpy (best signal of the GUI's current dir) ---
        try:
            if getattr(self, "siril", None) and getattr(self.siril, "iface", None):
                out = self.siril.iface.cmd("pwd")
                cand = _parse_pwd(out)
                if cand and os.path.isdir(cand):
                    return cand
        except Exception:
            pass

        # --- 2) siril-cli fallback (works when running the app outside Siril) ---
        try:
            siril = find_siril_cli(getattr(self.project, "siril_cli_path", None))
            if siril and os.path.exists(siril):
                import tempfile, pathlib, subprocess
                with tempfile.TemporaryDirectory() as td:
                    td_path = pathlib.Path(td)
                    # Minimal script to print the current working directory and exit
                    ssf = td_path / "echo_pwd.ssf"
                    # `pwd` prints; we add a newline to be safe
                    ssf.write_text("pwd\n", encoding="utf-8")
                    # Run siril-cli against the temp script; cwd doesn’t matter much,
                    # but we prefer launching in the user home so we don’t get an odd install path.
                    try:
                        out = subprocess.check_output(
                            [siril, "-s", str(ssf)],
                            cwd=os.path.expanduser("~"),
                            stderr=subprocess.STDOUT,
                            text=True,
                            timeout=10
                        )
                    except Exception:
                        out = ""
                cand = _parse_pwd(out)
                if cand and os.path.isdir(cand):
                    return cand
        except Exception:
            pass

        # --- 3) Process CWD (if the app was launched from a project folder, this may be right) ---
        try:
            cand = os.getcwd()
            if cand and os.path.isdir(cand):
                return cand
        except Exception:
            pass

        # --- 4) User home as a last resort ---
        try:
            cand = os.path.expanduser("~")
            if cand and os.path.isdir(cand):
                return cand
        except Exception:
            pass

        if self.siril.connected:
            self.siril.log(f"[Init] Default working dir guess: {sir_dir or '(none)'}")

        return None

    def _set_initial_list_heights(self, rows: int = 6):
        """
        Set initial visible rows for the Sessions and Panels lists.
        On shorter screens, reduce rows to avoid pushing the window taller than the display.
        """
        # Detect available screen height (fallback to 1080 if unavailable)
        try:
            avail_h = QtGui.QGuiApplication.primaryScreen().availableGeometry().height()
        except Exception:
            avail_h = 1080

        # Adjust target rows for shorter screens
        # ~900px tall (common after taskbar on 1080p) -> 4 rows; <=800px -> 3 rows
        if avail_h <= 800:
            rows = min(rows, 3)
        elif avail_h <= 900:
            rows = min(rows, 4)

        def _min_height_for_rows(lst: QtWidgets.QListWidget, n_rows: int) -> int:
            # Try to get a real row height; if list is empty, estimate from font metrics
            rh = lst.sizeHintForRow(0)
            if rh <= 0:
                rh = lst.fontMetrics().height() + 8  # small padding fudge
            extra = lst.frameWidth() * 2 + 6        # borders/margins
            return rh * n_rows + extra

        # Keep row heights consistent
        self.sessions_list.setUniformItemSizes(True)
        self.lst_panels.setUniformItemSizes(True)

        # Apply minimum heights so ~rows items are visible without scrolling initially
        self.sessions_list.setMinimumHeight(_min_height_for_rows(self.sessions_list, rows))
        self.lst_panels.setMinimumHeight(_min_height_for_rows(self.lst_panels, rows))

        # Optional: cap maximum to avoid growing too tall on very large screens
        # (comment out if you prefer them to grow freely)
        max_rows = max(rows, 8)
        self.sessions_list.setMaximumHeight(_min_height_for_rows(self.sessions_list, max_rows))
        self.lst_panels.setMaximumHeight(_min_height_for_rows(self.lst_panels, max_rows))

    def _toggle_sigma_by_method(self, idx: int):
        # sigma needed for first two methods
        needs_sigma = idx in (0, 1)
        for w in (self.lbl_sigma_low, self.dbl_sigma_low, self.lbl_sigma_high, self.dbl_sigma_high):
            w.setVisible(needs_sigma)

    def _toggle_drizzle_opts(self, enabled: bool):
        for w in (self.lbl_scaling, self.spin_scaling, self.lbl_pixfrac, self.spin_pixfrac, self.lbl_kernel, self.cb_kernel):
            w.setEnabled(enabled)

    def _sync_drizzle_two_pass_locks(self, *_):
        """Keep UI/behavior consistent for 2-pass registration.

        There are two cases where we must keep the UI in sync:

        1) Mosaic -> Drizzle per panel is enabled.
           - Per-panel drizzle is implemented as 2-pass (register -2pass + seqapplyreg -drizzle).
           - We therefore *force* Mosaic Registration Mode to Two-pass and lock it.
           - We also *force* Drizzle -> Use 2-pass registration ON and lock it.

        2) Mosaic is enabled and Mosaic Registration Mode is set to Two-pass.
           - For consistency, we keep Drizzle -> Use 2-pass registration checked and locked
             so the UI never suggests a contradictory mode.
        """
        per_panel = bool(self.chk_mosaic_drizzle_panel.isChecked())
        mosaic_on = bool(self.chk_mosaic_enabled.isChecked())
        mosaic_reg_two_pass = mosaic_on and (int(self.cmb_mosaic_reg.currentIndex()) == 0)

        # --- Case 1: Per-panel drizzle forces 2-pass everywhere ---
        if per_panel:
            # Remember previous UI states for restoration
            if not hasattr(self, '_prev_mosaic_reg_index'):
                self._prev_mosaic_reg_index = int(self.cmb_mosaic_reg.currentIndex())
            if not hasattr(self, '_prev_global_two_pass'):
                self._prev_global_two_pass = bool(self.cb_two_pass.isChecked())

            # Force 2-pass in Mosaic Registration Mode and lock
            self.cmb_mosaic_reg.setCurrentIndex(0)  # Two-pass
            self.cmb_mosaic_reg.setEnabled(False)

            # Force global 2-pass and lock
            self.cb_two_pass.setChecked(True)
            self.cb_two_pass.setEnabled(False)
            try:
                self.cb_two_pass.setToolTip("Forced ON when Mosaic → Drizzle per panel is enabled.")
            except Exception:
                pass
            return

        # If we reach here, per-panel drizzle is OFF.
        # Restore Mosaic Registration Mode selection (if we captured one)
        if hasattr(self, '_prev_mosaic_reg_index'):
            try:
                self.cmb_mosaic_reg.setCurrentIndex(self._prev_mosaic_reg_index)
            except Exception:
                pass
            delattr(self, '_prev_mosaic_reg_index')

        # Unlock Mosaic Registration Mode (but respect Mosaic master enable)
        self.cmb_mosaic_reg.setEnabled(mosaic_on)

        # --- Case 2: Mosaic 2-pass selected -> keep Drizzle 2-pass in sync ---
        if mosaic_reg_two_pass:
            if not hasattr(self, '_prev_global_two_pass_by_mosaic'):
                self._prev_global_two_pass_by_mosaic = bool(self.cb_two_pass.isChecked())
            self.cb_two_pass.setChecked(True)
            self.cb_two_pass.setEnabled(False)
            try:
                self.cb_two_pass.setToolTip("Locked ON while Mosaic Registration Mode is set to Two-pass.")
            except Exception:
                pass
        else:
            # Restore global 2-pass checkbox state (if we captured one)
            if hasattr(self, '_prev_global_two_pass'):
                self.cb_two_pass.setChecked(self._prev_global_two_pass)
                delattr(self, '_prev_global_two_pass')
            if hasattr(self, '_prev_global_two_pass_by_mosaic'):
                self.cb_two_pass.setChecked(self._prev_global_two_pass_by_mosaic)
                delattr(self, '_prev_global_two_pass_by_mosaic')
            self.cb_two_pass.setEnabled(True)

    def _set_mosaic_controls_enabled(self, enabled: bool):
        widgets = [
            # geometry / layout
            self.sp_mosaic_rows, self.sp_mosaic_cols, self.sp_mosaic_overlap,
            # global reference + scale + registration/stacking
            self.cmb_mosaic_ref, self.dsb_mosaic_scale, self.cmb_mosaic_reg,
            # self.cmb_mosaic_stack,
            # normalization / blending
            self.chk_mosaic_bg, self.chk_mosaic_maximize, self.chk_mosaic_overlap_norm, self.sp_mosaic_feather,
            # drizzle scope
            self.chk_mosaic_drizzle_panel,
            # grid helpers
            self.chk_auto_grid, self.btn_gen_grid, self.cmb_grid_scope, self.cmb_name_scheme,
            # preview
            self.btn_preview_mosaic,
        ]
        for w in widgets:
            w.setEnabled(enabled)

    def _on_mosaic_toggled(self, enabled: bool):
        # Left-side panels list + buttons
        for w in (self.lst_panels, self.btn_add_panel, self.btn_remove_panel):
            w.setEnabled(enabled)

        # Grey-out the Panels group box title and contents
        if hasattr(self, "grp_panels"):
            self.grp_panels.setEnabled(enabled)

        # Panel metadata fields are only editable when Mosaic is ON
        self.panel_editor.set_metadata_enabled(enabled)

        # Make the rest of the Mosaic box read-only when disabled
        self._set_mosaic_controls_enabled(enabled)

        # Pack controls and constraints
        self._set_pack_controls_enabled(not enabled)
        self._enforce_pack_off_if_mosaic()
        self.chk_link_feather.setEnabled(enabled)

        # Normalize drizzle scope without changing enabled state

        # Let the per-session/per-panel UI (including the Copy Cal button)
        # recompute its enabled state based on the new Mosaic flag.
        self._refresh_panels_ui_for_session(self._current_session())

        # Switch to the appropriate tab by default
        try:
            self.right_tabs.setCurrentWidget(self.panel_editor if enabled else self.session_editor)
        except Exception:
            pass

        # If per-panel drizzle is enabled, keep forced 2-pass + locks consistent
        self._sync_drizzle_two_pass_locks()

    def _on_add_panel(self):
        if not self.chk_mosaic_enabled.isChecked():
            return

        # NEW: generate ID using the current grid + naming scheme
        count = self.lst_panels.count()
        cols = max(1, int(self.sp_mosaic_cols.value() or 1))
        r = count // cols   # 0-based row
        c = count % cols    # 0-based col
        new_id = self._panel_name_for(r, c)

        self.lst_panels.addItem(new_id)

        # Create the panel on the model for the current session
        s = self._current_session()
        if s is not None:
            s.panels.append(Panel(panel_id=new_id))
            new_index = self.lst_panels.count() - 1
            self.lst_panels.setCurrentRow(new_index)
            self._loading_panel = True
            try:
                self.panel_editor.from_panel(s.panels[new_index])  # loads a fresh, empty Panel
            finally:
                self._loading_panel = False

        # Focus Panel tab for editing
        try:
            self.right_tabs.setCurrentWidget(self.panel_editor)
        except Exception:
            pass

        # Re-apply enable/disable rules now that we have (at least) one panel
        s = self._current_session()
        if s is not None:
            mosaic_on = self.chk_mosaic_enabled.isChecked()
            has_panels = bool(s.panels)

            # Panel tab frame lists: enabled when Mosaic is ON and there is at least one panel
            self.panel_editor.set_frame_groups_enabled(mosaic_on and has_panels)

            # Update the "Copy Calibration Frames" button state
            if has_panels:
                first_id = s.panels[0].panel_id or "A1"
                can_copy = mosaic_on and (len(s.panels) > 1)
                self.panel_editor.set_copy_source_panel(first_id, enabled=can_copy)
            else:
                self.panel_editor.set_copy_source_panel(None, enabled=False)

        self.mark_dirty()
        self._refresh_global_ref_choices(self.project.mosaic_global_reference)

        # Keep the Copy Calibration Frames button state in sync
        sess = self._current_session()
        if sess:
            panels = sess.panels
            mosaic_on = self.chk_mosaic_enabled.isChecked()
            first_id = panels[0].panel_id if panels else None
            can_copy = mosaic_on and (len(panels) > 1)
            self.panel_editor.set_copy_source_panel(first_id, enabled=can_copy)
        else:
            self.panel_editor.set_copy_source_panel(None, enabled=False)

    def _on_remove_panel(self):
        if not self.chk_mosaic_enabled.isChecked():
            return

        row = self.lst_panels.currentRow()
        if row < 0:
            return

        # Remove from UI and model
        self.lst_panels.takeItem(row)
        s = self._current_session()
        if s and 0 <= row < len(s.panels):
            s.panels.pop(row)

        # Update the editor selection / clear editor if no panels left
        self._loading_panel = True
        try:
            if self.lst_panels.count() == 0:
                self.panel_editor.from_panel(None)
            else:
                new_row = min(row, self.lst_panels.count() - 1)
                self.lst_panels.setCurrentRow(new_row)
                self.panel_editor.from_panel(s.panels[new_row] if s and 0 <= new_row < len(s.panels) else None)
        finally:
            self._loading_panel = False

        # After removal, re-apply enable/disable rules for the Panel tab
        mosaic_on = self.chk_mosaic_enabled.isChecked()
        has_panels = bool(s and getattr(s, "panels", []))

        # Panel frame lists: enabled only when Mosaic is ON and there is at least one panel
        self.panel_editor.set_frame_groups_enabled(mosaic_on and has_panels)

        # Update the "Copy Calibration Frames" button state
        if has_panels:
            first_id = s.panels[0].panel_id or "A1"
            can_copy = mosaic_on and (len(s.panels) > 1)
            self.panel_editor.set_copy_source_panel(first_id, enabled=can_copy)
        else:
            self.panel_editor.set_copy_source_panel(None, enabled=False)

        self.mark_dirty()
        self._refresh_global_ref_choices(self.project.mosaic_global_reference)

        # Keep the Copy Calibration Frames button state in sync
        sess = self._current_session()
        if sess:
            panels = sess.panels
            mosaic_on = self.chk_mosaic_enabled.isChecked()
            if panels:
                first_id = panels[0].panel_id
                can_copy = mosaic_on and (len(panels) > 1)
                self.panel_editor.set_copy_source_panel(first_id, enabled=can_copy)
            else:
                # No panels left → hide the button
                self.panel_editor.set_copy_source_panel(None, enabled=False)
        else:
            self.panel_editor.set_copy_source_panel(None, enabled=False)

    def _panel_name_for(self, r: int, c: int) -> str:
        # r, c are 0-based
        scheme = self.cmb_name_scheme.currentIndex()
        if scheme == 0:
            # RowLetter+ColNumber, e.g. A1, B3 …
            return f"{chr(ord('A') + r)}{c + 1}"
        else:
            # R#C#, e.g. R1C2 …
            return f"R{r + 1}C{c + 1}"

    def _target_panel_ids(self) -> list[str]:
        rows = max(1, int(self.sp_mosaic_rows.value()))
        cols = max(1, int(self.sp_mosaic_cols.value()))
        out = []
        for r in range(rows):
            for c in range(cols):
                out.append(self._panel_name_for(r, c))
        return out

    def _ensure_grid_for_session(self, sess: Session, prune_empty: bool = False) -> tuple[int,int]:
        """
        Ensure session.panels match the grid (append missing).
        If prune_empty=True, remove extra *empty* panels not in the grid.
        Returns (added, removed)
        """
        want = self._target_panel_ids()
        have = [p.panel_id for p in getattr(sess, "panels", [])]
        added = removed = 0

        # Append missing panels in order
        for pid in want:
            if pid not in have:
                sess.panels.append(Panel(panel_id=pid))
                added += 1

        # Optionally prune extras that are not in the grid but only if they are empty
        if prune_empty and len(sess.panels) > len(want):
            keep = set(want)
            new_list = []
            for p in sess.panels:
                if p.panel_id in keep:
                    new_list.append(p)
                else:
                    # remove only if empty
                    if any([p.lights, p.bias, p.darks, p.flats, p.dark_flats]):
                        new_list.append(p)  # keep non-empty panel
                    else:
                        removed += 1
            sess.panels = new_list

        # Finally, sort in row-major grid order
        order = {pid: i for i, pid in enumerate(want)}
        sess.panels.sort(key=lambda p: order.get(p.panel_id, 10**9))

        return added, removed

    def _on_generate_panels_from_grid(self):
        if not self.chk_mosaic_enabled.isChecked():
            QtWidgets.QMessageBox.information(self, "Mosaic", "Enable Mosaic Mode first.")
            return

        scope_all = (self.cmb_grid_scope.currentIndex() == 1)

        # Optional confirmation if pruning
        prune = False
        resp = QtWidgets.QMessageBox.question(
            self, "Generate Panels",
            "Generate panels from the grid?\n\n"
            "Tip: This will add any missing panels. It will not remove non-empty panels.\n"
            "Would you also like to remove extra *empty* panels that are outside the grid?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No
        )
        prune = (resp == QtWidgets.QMessageBox.StandardButton.Yes)

        sessions = self.project.sessions if scope_all else [self._current_session()]
        sessions = [s for s in sessions if s is not None]

        total_added = total_removed = 0
        for s in sessions:
            a, r = self._ensure_grid_for_session(s, prune_empty=prune)
            total_added += a; total_removed += r

        # Refresh left pane lists for current session
        self._refresh_panels_ui_for_session(self._current_session())
        self.mark_dirty()

        QtWidgets.QMessageBox.information(
            self, "Generate Panels",
            f"Done.\nAdded: {total_added}\nRemoved (empty only): {total_removed}"
        )
        self._refresh_global_ref_choices(self.project.mosaic_global_reference)

    def _on_grid_changed(self, *_):
        # Do nothing while we're restoring UI / switching sessions / loading a panel
        if getattr(self, "_suspend_dirty", False) or getattr(self, "_loading_session", False) or getattr(self, "_loading_panel", False):
            return

        # Only act when Mosaic is ON and auto-manage is enabled
        if not (self.chk_mosaic_enabled.isChecked() and self.chk_auto_grid.isChecked()):
            return

        s = self._current_session()
        if s is None:
            return

        sender   = self.sender()
        existing = bool(getattr(s, "panels", []))

        # If we just turned Auto-manage ON on a session that already has panels,
        # leave that session alone. Auto-manage will only apply to new sessions.
        if sender is self.chk_auto_grid and existing:
            return

        # If rows/cols changed on a session that already has panels, also leave it
        # alone. The user can explicitly use "Generate panels from grid…" instead.
        if sender in (self.sp_mosaic_rows, self.sp_mosaic_cols) and existing:
            return

        # For sessions with no panels yet, auto-generate from the current grid
        self._ensure_grid_for_session(s, prune_empty=False)
        self._refresh_panels_ui_for_session(s)
        self.mark_dirty()

    def _panel_status_for_preview(self, sess: Session) -> Dict[str, Dict[str, int]]:
        """
        Return { panel_id: {lights: n, bias: n, darks: n, flats: n, dark_flats: n} }
        for the given session. Missing panels will be absent from the dict.
        """
        out: Dict[str, Dict[str, int]] = {}
        for pan in getattr(sess, "panels", []):
            out[pan.panel_id or ""] = {
                "lights":     len(getattr(pan, "lights", [])),
                "bias":       len(getattr(pan, "bias", [])),
                "darks":      len(getattr(pan, "darks", [])),
                "flats":      len(getattr(pan, "flats", [])),
                "dark_flats": len(getattr(pan, "dark_flats", [])),
            }
        return out

    def _on_preview_mosaic(self):
        # --- Suppress any dirty writes while we open a display-only dialog ---
        old_suppress = getattr(self, "_suspend_dirty", False)
        old_loading_panel = getattr(self, "_loading_panel", False)
        self._suspend_dirty = True
        self._loading_panel = True
        try:
            # Read current UI values into the model (will not mark dirty due to guards)
            self.push_to_model()

            p = self.project
            if not p.mosaic_enabled:
                QtWidgets.QMessageBox.information(self, "Mosaic Preview", "Enable Mosaic Mode first.")
                return

            sess = self._current_session()
            if not sess:
                QtWidgets.QMessageBox.information(self, "Mosaic Preview", "Select a session to preview.")
                return

            # Global panel ref not wired yet -> None
            global_ref_pid = None

            dlg = MosaicPreviewDialog(
                rows=self.sp_mosaic_rows.value(),
                cols=self.sp_mosaic_cols.value(),
                overlap_pct=self.sp_mosaic_overlap.value(),
                name_scheme=int(getattr(p, "_ui_name_scheme", 0)),
                global_ref_pid=global_ref_pid,
                panel_status=self._panel_status_for_preview(sess),
                parent=self
            )

            # Title with session + optional scale
            title = f"Preview Mosaic Layout — {sess.name}"
            if abs(p.mosaic_canvas_scale - 1.0) > 1e-6:
                title += f" (Scale {p.mosaic_canvas_scale:g}×)"
            dlg.setWindowTitle(title)

            # Show the preview (modal). Any resize jiggles are display-only.
            dlg.exec()

        finally:
            # Restore guards after dialog closes
            self._suspend_dirty = old_suppress
            self._loading_panel = old_loading_panel

    def _init_splitter_sizes(self, splitter: QtWidgets.QSplitter):
        """
        Pick an initial split so there's no horizontal scrolling:
        - left gets the smaller share (≈32-38%),
        - right keeps at least ~600px for the editors.
        """
        # Use current widget width if available; fall back to screen width
        try:
            avail_w = max(self.width(), 1000)
            if avail_w <= 1000:
                raise RuntimeError
        except Exception:
            try:
                avail_w = QtGui.QGuiApplication.primaryScreen().availableGeometry().width()
            except Exception:
                avail_w = 1280

        # Compute a conservative left width that won't force horizontal scrolling
        # Start with a larger left portion; keep a safe floor for the right editor
        left_target = int(avail_w * 0.54)              # ~54% to the left
        left = max(560, min(780, left_target))         # clamp wider (matches new caps)
        right = max(520, int(avail_w * 0.90) - left)   # ensure right stays usable
        splitter.setSizes([left, right])

    def _refresh_global_ref_choices(self, keep_text: str | None = None):
        """
        Fill the 'Global Reference' combo with:
        • '<Session Name> / BestFrame'
        • '<Session Name> / <PanelID>' for each panel in the session
        If keep_text is provided (or project has a saved value), try to preserve selection.
        """
        p = self.project
        cur = keep_text or (getattr(p, "mosaic_global_reference", None) or "")
        self.cmb_mosaic_ref.blockSignals(True)
        try:
            self.cmb_mosaic_ref.clear()
            # Build choices
            items: list[str] = []
            for s in p.sessions:
                items.append(f"{s.name} / BestFrame")
                for pan in getattr(s, "panels", []) or []:
                    pid = pan.panel_id or "A1"
                    items.append(f"{s.name} / {pid}")

            # Populate
            for it in items:
                self.cmb_mosaic_ref.addItem(it)

            # Restore selection if possible
            if cur:
                i = self.cmb_mosaic_ref.findText(cur)
                if i >= 0:
                    self.cmb_mosaic_ref.setCurrentIndex(i)
            # If nothing selected yet but we have items, pick the first
            if self.cmb_mosaic_ref.currentIndex() < 0 and self.cmb_mosaic_ref.count() > 0:
                self.cmb_mosaic_ref.setCurrentIndex(0)
        finally:
            self.cmb_mosaic_ref.blockSignals(False)

    def _set_pack_controls_enabled(self, enabled: bool):
        for w in (self.pack_label, self.pack_mode, self.pack_thresh):
            w.setEnabled(enabled)

    def _enforce_pack_off_if_mosaic(self):
        # If mosaic is ON, force pack mode Off in the UI (no dirty flip if already Off)
        if self.chk_mosaic_enabled.isChecked():
            if self.pack_mode.currentIndex() != 0:
                self.pack_mode.setCurrentIndex(0)
            self.pack_thresh.setEnabled(False)
            # Optional: make the reason clear in tooltip
            tip = ("Disabled in Mosaic Mode. Siril 1.4 cannot plate-solve FITSEQ/SER "
                "sequences for mosaics; use unpacked FITS only.")
            self.pack_label.setToolTip(tip)
            self.pack_mode.setToolTip(tip)
            self.pack_thresh.setToolTip(tip)
        else:
            # Restore normal tooltip and enablement
            self.pack_label.setToolTip("Use FITSEQ/SER to avoid open-file limits (non-mosaic only).")
            self.pack_mode.setToolTip("Pack sequences for very large projects (non-mosaic only).")
            self.pack_thresh.setToolTip("Threshold in Auto mode")
            self._toggle_pack_thresh()  # existing logic re-enables threshold for “Auto”

    def _toggle_pack_thresh(self, *_):
        """Enable threshold only when 'Auto when > N' is selected (index 3)."""
        self.pack_thresh.setEnabled(self.pack_mode.currentIndex() == 3)

    def _update_feather_from_overlap(self):
        if not getattr(self, "chk_mosaic_enabled", None) or not self.chk_mosaic_enabled.isChecked():
            return
        # Only adjust when linking is enabled
        if not self.chk_link_feather.isChecked():
            return
        w = int(getattr(self.project, "frame_width", 0) or 0)
        h = int(getattr(self.project, "frame_height", 0) or 0)
        if not (w and h):
            # No frame size yet (e.g., before Prepare); do nothing
            return
        overlap_pct = float(self.sp_mosaic_overlap.value())
        overlap_px  = round(min(w, h) * overlap_pct / 100.0)
        est_feather = max(20, min(300, round(overlap_px * 0.5)))  # clamp 20–300
        # Avoid feedback loops
        self.sp_mosaic_feather.blockSignals(True)
        self.sp_mosaic_feather.setValue(est_feather)
        self.sp_mosaic_feather.blockSignals(False)

    def _on_overlap_pct_changed(self, *_):
        self._update_feather_from_overlap()
        self.mark_dirty()

    def _confirm(self, text: str, title: str = "Please confirm") -> bool:
        """
        Modal Yes/No dialog. Returns True if user clicks Yes.
        """
        resp = QtWidgets.QMessageBox.question(
            self, title, text,
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.No,
        )
        return resp == QtWidgets.QMessageBox.StandardButton.Yes

    def _info(self, text: str, title: str = "Info") -> None:
        """
        Modal OK dialog for informational notices.
        """
        QtWidgets.QMessageBox.information(self, title, text)

    def mark_dirty(self, *_):
        if self._suspend_dirty or self._loading_session or getattr(self, "_loading_panel", False):
            return
        self._dirty = True
        self.status_message.emit("Project has unsaved changes.")

    # ---------------- Model <-> UI ----------------
    def refresh_from_model(self):
        self._suspend_dirty = True
        try:
            p = self.project
            mode_map = {"off": 0, "fitseq": 1, "ser": 2, "auto": 3}
            pm_idx = mode_map.get((getattr(p, "pack_sequences_mode", "off") or "off").lower(), 0)

            self.ed_name.setText(p.name or "")
            self.ed_workdir.setText(p.working_dir or "")
            self.cb_use_library.setChecked(p.use_master_library)
            self.cb_allow_uncal.setChecked(bool(getattr(p, "allow_uncalibrated", False)))  # NEW
            self.cb_remember_size.setChecked(bool(getattr(p, "remember_window_size", False)))
            self.ed_siril.setText(p.siril_cli_path or "")
            self.cb_force_cli.setChecked(bool(getattr(p, "force_cli", False)))

            self.cb_drizzle.setChecked(p.drizzle_enabled)
            self.spin_scaling.setValue(float(p.drizzle_scaling or 1.0))
            self.spin_pixfrac.setValue(float(p.drizzle_pixfrac or 1.0))
            kernels = ["square", "point", "turbo", "gaussian", "lanczos2", "lanczos3"]
            self.cb_kernel.setCurrentIndex(max(0, kernels.index(p.drizzle_kernel) if p.drizzle_kernel in kernels else 0))
            self.cb_two_pass.setChecked(p.two_pass)

            sm_idx = self.cb_stack_method.findText(p.stack_method or "Winsorized Rejection")
            if sm_idx < 0:
                sm_idx = 0
            self.cb_stack_method.setCurrentIndex(sm_idx)
            self._toggle_sigma_by_method(sm_idx)

            self.dbl_sigma_low.setValue(float(p.reject_sigma_low or 3.0))
            self.dbl_sigma_high.setValue(float(p.reject_sigma_high or 3.0))
            self.cb_stack_32.setChecked(bool(getattr(p, "stack_32bit", False)))
            self.cb_compress.setChecked(bool(getattr(p, "compress_intermediates", False)))

            self.pack_mode.setCurrentIndex(pm_idx)
            self.pack_thresh.setValue(int(getattr(p, "pack_threshold", 2000)))
            self.pack_thresh.setEnabled(pm_idx == 3)

            # --- Mosaic ---
            self.chk_mosaic_enabled.setChecked(p.mosaic_enabled)
            self.chk_link_feather.setEnabled(p.mosaic_enabled)
            self._set_pack_controls_enabled(not p.mosaic_enabled)
            self._enforce_pack_off_if_mosaic()

            # Restore scheme and auto-grid first (prevents briefly using the wrong scheme)
            self.cmb_name_scheme.setCurrentIndex(int(getattr(p, "_ui_name_scheme", 0)))
            self.chk_auto_grid.setChecked(bool(getattr(p, "_ui_mosaic_auto_grid", False)))
            self.cmb_grid_scope.setCurrentIndex(int(getattr(p, "_ui_grid_scope", 0)))

            # Then restore geometry values
            self.sp_mosaic_rows.setValue(p.mosaic_grid_rows)
            self.sp_mosaic_cols.setValue(p.mosaic_grid_cols)
            self.sp_mosaic_overlap.setValue(p.mosaic_overlap_percent)

            self._refresh_global_ref_choices()

            self.dsb_mosaic_scale.setValue(p.mosaic_canvas_scale)
            self.cmb_mosaic_reg.setCurrentText(p.mosaic_registration_mode)

            self.chk_mosaic_bg.setChecked(p.panel_background_extraction)
            self.chk_mosaic_maximize.setChecked(getattr(p, 'mosaic_maximize_framing', True))
            self.chk_mosaic_overlap_norm.setChecked(p.mosaic_overlap_norm)
            self.sp_mosaic_feather.setValue(p.mosaic_feather_px)
            self.chk_link_feather.setChecked(p.link_feather_to_overlap)
            self.chk_mosaic_drizzle_panel.setChecked(p.mosaic_drizzle_per_panel)
            self._update_feather_from_overlap()            

            # sessions list
            self.sessions_list.clear()
            for s in p.sessions:
                self.sessions_list.addItem(s.name)
            if p.sessions:
                self.sessions_list.setCurrentRow(0)
                self._loading_session = True
                try:
                    self.session_editor.from_session(p.sessions[0])
                finally:
                    self._loading_session = False

            # panels: show for the selected session
            self._refresh_panels_ui_for_session(self._current_session())

            # toggle Panels availability
            self._on_mosaic_toggled(p.mosaic_enabled)

            # Keep drizzle-related UI consistent (locks/forced 2-pass when needed)
            self._sync_drizzle_two_pass_locks()

            # sigma visibility
            self._toggle_sigma_by_method(self.cb_stack_method.currentIndex())
        finally:
            self._suspend_dirty = False
        # Keep them mutually exclusive visually after loading


    def push_to_model(self):
        p = self.project
        p.name = self.ed_name.text() or "New Project"
        p.working_dir = self.ed_workdir.text() or None
        p.use_master_library = self.cb_use_library.isChecked()
        p.allow_uncalibrated = self.cb_allow_uncal.isChecked()  # NEW
        p.remember_window_size = self.cb_remember_size.isChecked()

        p.drizzle_enabled = self.cb_drizzle.isChecked()
        p.drizzle_scaling = float(self.spin_scaling.value())
        p.drizzle_pixfrac = float(self.spin_pixfrac.value())
        p.drizzle_kernel  = self.cb_kernel.currentText()
        p.two_pass        = self.cb_two_pass.isChecked()

        # Store exactly what the user picked in the combobox
        p.stack_method = self.cb_stack_method.currentText()   # "Winsorized Rejection" | "Sigma Rejection" | "Mean" | "Median"

        p.reject_sigma_low   = float(self.dbl_sigma_low.value())
        p.reject_sigma_high  = float(self.dbl_sigma_high.value())
        p.stack_32bit        = self.cb_stack_32.isChecked()
        p.compress_intermediates = self.cb_compress.isChecked()
        p.pack_sequences_mode = self.pack_mode.currentText().split()[0].lower()  # off|fitseq|ser|auto
        p.pack_threshold      = int(self.pack_thresh.value())

        p.siril_cli_path = self.ed_siril.text() or None
        p.force_cli      = self.cb_force_cli.isChecked()

        # --- Mosaic (write back) ---
        p.mosaic_enabled = self.chk_mosaic_enabled.isChecked()
        p.mosaic_grid_rows = self.sp_mosaic_rows.value()
        p.mosaic_grid_cols = self.sp_mosaic_cols.value()
        p.mosaic_overlap_percent = self.sp_mosaic_overlap.value()
        p.mosaic_global_reference = self.cmb_mosaic_ref.currentText().strip() or None
        p.mosaic_canvas_scale = float(self.dsb_mosaic_scale.value())
        p.mosaic_registration_mode = self.cmb_mosaic_reg.currentText()
        # Map UI to tokens Siril builder will expect later
        # map_stack = {"Mean":"mean", "Winsorized":"wrej", "Sigma":"rej", "Median":"median"}
        p.mosaic_stack_method = p.stack_method
        p.panel_background_extraction = self.chk_mosaic_bg.isChecked()
        p.mosaic_maximize_framing = self.chk_mosaic_maximize.isChecked()
        p.mosaic_overlap_norm = self.chk_mosaic_overlap_norm.isChecked()
        p.mosaic_feather_px = self.sp_mosaic_feather.value()
        p.link_feather_to_overlap = self.chk_link_feather.isChecked()
        p.mosaic_drizzle_per_panel = self.chk_mosaic_drizzle_panel.isChecked()

        p.pack_sequences_mode = self.pack_mode.currentText().split()[0].lower()
        p.pack_threshold      = int(self.pack_thresh.value())

        # Mosaic mutual exclusion with packing
        if p.mosaic_enabled and p.pack_sequences_mode != "off":
            p.pack_sequences_mode = "off"

        p._ui_mosaic_auto_grid = self.chk_auto_grid.isChecked()
        p._ui_name_scheme = self.cmb_name_scheme.currentIndex()
        p._ui_grid_scope = self.cmb_grid_scope.currentIndex()

        # Ensure latest Panel editor data is written to model
        self.update_current_panel()

        # Only aggregate panels -> session lists when Mosaic Mode is OFF.
        # When Mosaic Mode is ON, keep session lists independent so they don't get
        # saved/loaded with panel data.
        if not p.mosaic_enabled:
            for s in p.sessions:
                if getattr(s, "panels", []):
                    agg = {ft: [] for ft in FRAME_TYPES}
                    for pan in s.panels:
                        agg["lights"]     += list(getattr(pan, "lights", []))
                        agg["bias"]       += list(getattr(pan, "bias", []))
                        agg["darks"]      += list(getattr(pan, "darks", []))
                        agg["flats"]      += list(getattr(pan, "flats", []))
                        agg["dark_flats"] += list(getattr(pan, "dark_flats", []))
                    s.lights = agg["lights"]
                    s.bias   = agg["bias"]
                    s.darks  = agg["darks"]
                    s.flats  = agg["flats"]
                    s.dark_flats = agg["dark_flats"]

    # ---------------- pickers ----------------
    def pick_workdir(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "Select Working Directory")
        if d:
            self.ed_workdir.setText(d)

    def pick_siril(self):
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Select siril-cli executable")
        if f:
            self.ed_siril.setText(f)

    # ---------------- sessions ----------------
    def add_session(self):
        new_sess = Session(name=f"Session {len(self.project.sessions)+1}")
        self.project.sessions.append(new_sess)
        self.sessions_list.addItem(new_sess.name)
        self.sessions_list.setCurrentRow(self.sessions_list.count()-1)

        self._loading_session = True
        try:
            self.session_editor.from_session(new_sess)
        finally:
            self._loading_session = False

        # NEW: auto-generate panels for this session when mosaic + auto-grid are on
        if self.chk_mosaic_enabled.isChecked() and self.chk_auto_grid.isChecked():
            # make sure the model has the right panel set
            self._ensure_grid_for_session(new_sess, prune_empty=False)

        # NEW: refresh the Panels UI for the new current session
        self._refresh_panels_ui_for_session(new_sess)

        # NEW: sync Copy Calibration Frames button for the new session
        panels = getattr(new_sess, "panels", [])
        if panels:
            first_id = panels[0].panel_id or "A1"
            can_copy = self.chk_mosaic_enabled.isChecked() and (len(panels) > 1)
            self.panel_editor.set_copy_source_panel(first_id, enabled=can_copy)
        else:
            self.panel_editor.set_copy_source_panel(None, enabled=False)

        self.mark_dirty()
        self._refresh_global_ref_choices(self.project.mosaic_global_reference)

    def remove_session(self):
        row = self.sessions_list.currentRow()
        if row < 0:
            return

        sess = self.project.sessions[row]
        work = self.project.working_dir
        sess_root = Path(work) / (sess.work_subdir or sess.name) if work else None

        msg = "Remove this session from the project and delete its on-disk data?\n"
        if sess_root:
            msg += f"\n{sess_root}"

        if QtWidgets.QMessageBox.question(
            self,
            "Remove Session",
            msg,
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return

        # Delete on-disk data for this session (if any)
        if sess_root and sess_root.exists():
            try:
                shutil.rmtree(sess_root)
            except Exception as e:
                QtWidgets.QMessageBox.warning(
                    self,
                    "Remove Session",
                    f"Failed to delete:\n{sess_root}\n\n{e}",
                )

        # Drop any project-level mosaic panel links that point at this session
        if getattr(self.project, "panels", None):
            self.project.panels = [
                d for d in self.project.panels
                if d.get("session") != sess.name
            ]

        # Remove the session from the model + list widget
        self.project.sessions.pop(row)
        self.sessions_list.takeItem(row)

        if self.project.sessions:
            # Select the first remaining session and refresh its editors
            self.sessions_list.setCurrentRow(0)

            self._loading_session = True
            try:
                self.session_editor.from_session(self.project.sessions[0])
            finally:
                self._loading_session = False

            # Refresh panels list / editor for the new current session
            self._refresh_panels_ui_for_session(self._current_session())
        else:
            # No sessions left: clear panels UI and reset the session editor fields
            self._refresh_panels_ui_for_session(None)
            self._loading_session = True
            try:
                # from_session(None) will clear all frame lists and overrides
                self.session_editor.from_session(None)  # type: ignore[arg-type]
            finally:
                self._loading_session = False

        self.mark_dirty()
        self._refresh_global_ref_choices(self.project.mosaic_global_reference)

    def duplicate_session(self):
        row = self.sessions_list.currentRow()
        if row < 0: return
        orig = self.project.sessions[row]
        copy = Session.from_dict(orig.to_dict())
        copy.name = f"{orig.name} (copy)"
        self.project.sessions.insert(row+1, copy)
        self.sessions_list.insertItem(row+1, copy.name)
        self.sessions_list.setCurrentRow(row+1)
        self._loading_session = True
        try: self.session_editor.from_session(copy)
        finally: self._loading_session = False
        self.mark_dirty()
        self._refresh_global_ref_choices(self.project.mosaic_global_reference)

    def remove_all_sessions_data(self):
        # Operational cleanup: do not mark project dirty
        was_dirty = getattr(self, "_dirty", False)
        self._suspend_dirty = True
        try:
            self.push_to_model()
            p = self.project
            if not p.working_dir:
                QtWidgets.QMessageBox.warning(self, "Remove Data", "Please set a working directory first.")
                return

            work = Path(p.working_dir).resolve()
            targets = [work / (s.work_subdir or s.name) for s in p.sessions]

            resp = QtWidgets.QMessageBox.question(
                self,
                "Remove Data (All Sessions)",
                "Delete on-disk data for ALL sessions (configs kept)?",
                QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
                QtWidgets.QMessageBox.StandardButton.No,
            )
            if resp != QtWidgets.QMessageBox.StandardButton.Yes:
                return

            ok = fail = 0
            for t in targets:
                try:
                    if t.exists():
                        shutil.rmtree(t)
                    ok += 1
                except Exception:
                    fail += 1

            QtWidgets.QMessageBox.information(self, "Remove Data", f"Done.\nOK: {ok}\nFailed: {fail}")
        finally:
            self._suspend_dirty = False
            self._dirty = was_dirty

    def load_selected_session(self, row: int):
        if 0 <= row < len(self.project.sessions):
            self._loading_session = True
            try: self.session_editor.from_session(self.project.sessions[row])
            finally: self._loading_session = False

            # refresh Panels for this session
            self._refresh_panels_ui_for_session(self._current_session())
            self._refresh_global_ref_choices(self.project.mosaic_global_reference)

    def update_current_session(self):
        if self._loading_session:
            return
        row = self.sessions_list.currentRow()
        if 0 <= row < len(self.project.sessions):
            old = self.project.sessions[row]
            new = self.session_editor.to_session()
            # Preserve panels from the existing session
            new.panels = getattr(old, "panels", [])
            self.project.sessions[row] = new
            self.sessions_list.item(row).setText(new.name)
            self.mark_dirty()

    def _gather_session_files(self, session: Session) -> Dict[str, list[str]]:
        """
        Return a dict of frame lists for the given session.
        If Mosaic Mode is ON and the session has panels, gather files from panels.
        Otherwise, return the session-level lists (legacy behavior).
        """
        p = self.project
        buckets: Dict[str, list[str]] = {ft: [] for ft in FRAME_TYPES}

        if p.mosaic_enabled and getattr(session, "panels", []):
            # Aggregate per-panel lists (but keep session frame lists independent on disk)
            for pan in session.panels:
                buckets["lights"]     += list(getattr(pan, "lights", []))
                buckets["bias"]       += list(getattr(pan, "bias", []))
                buckets["darks"]      += list(getattr(pan, "darks", []))
                buckets["flats"]      += list(getattr(pan, "flats", []))
                buckets["dark_flats"] += list(getattr(pan, "dark_flats", []))
        else:
            # Legacy per-session lists
            buckets["lights"]     = list(getattr(session, "lights", []))
            buckets["bias"]       = list(getattr(session, "bias", []))
            buckets["darks"]      = list(getattr(session, "darks", []))
            buckets["flats"]      = list(getattr(session, "flats", []))
            buckets["dark_flats"] = list(getattr(session, "dark_flats", []))

        # De-duplicate per frame type, preserve order
        for key in buckets:
            seen = set()
            dedup = []
            for f in buckets[key]:
                if f not in seen:
                    dedup.append(f); seen.add(f)
            buckets[key] = dedup

        return buckets

    def _iter_panel_files(self, session: Session):
        """
        Yield (panel, frame_type, Path) for all files in all panels of a session.
        """
        for pan in getattr(session, "panels", []):
            for ft in FRAME_TYPES:
                for f in getattr(pan, ft, []) or []:
                    yield pan, ft, Path(f)

    # ---------------- prepare / build / run / abort ----------------
    def prepare_working_dir(self):
        # Do not let this operational action mark the project dirty
        was_dirty = getattr(self, "_dirty", False)
        self._suspend_dirty = True
        try:
            self.push_to_model()  # read UI → model without affecting dirty
            p = self.project
            if not p.working_dir:
                QtWidgets.QMessageBox.warning(self, "Prepare", "Please set a working directory.")
                return
            work = Path(p.working_dir).resolve()
            # Ensure the working directory exists to avoid FileNotFoundError when opening the log.
            try:
                # If the path exists but is not a directory, stop gracefully.
                if work.exists() and not work.is_dir():
                    QtWidgets.QMessageBox.critical(self, "Prepare", f"Working Directory path exists but is not a folder:\n{work}")
                    return
                work.mkdir(parents=True, exist_ok=True)
            except Exception as e:
                QtWidgets.QMessageBox.critical(self, "Prepare", f"Cannot create Working Directory:\n{work}\n\n{e}")
                return
            entries = []
            for sess in p.sessions:
                if getattr(p, "mosaic_enabled", False):
                    # panel-aware gather
                    for pan in getattr(sess, "panels", []) or []:
                        pid = pan.panel_id or "A1"
                        base = Path(sess.work_subdir or sess.name) / pid
                        for ft in FRAME_TYPES:
                            for f in getattr(pan, ft, []):
                                entries.append((str(base), ft, Path(f)))
                else:
                    # classic gather
                    for ft in FRAME_TYPES:
                        for f in getattr(sess, ft, []):
                            base = Path(sess.work_subdir or sess.name)
                            entries.append((str(base), ft, Path(f)))

            if not entries:
                QtWidgets.QMessageBox.information(self, "Prepare", "No files to prepare.")
                return

            from datetime import datetime
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            log_path = work / f"prepare_{ts}.log"
            ok = fail = 0
            total = len(entries)
            with open(log_path, "w", encoding="utf-8") as lf:
                for i, (base, ft, src) in enumerate(entries, start=1):
                    dst = work / base / ft / src.name
                    good, msg = safe_link_or_copy(src, dst)
                    ok += int(good)
                    fail += int(not good)
                    if self.siril.connected:
                        self.siril.log(msg)
                        self.siril.progress("Preparing…", i / total)
                    print(msg, file=lf)
                    QtWidgets.QApplication.processEvents()
            if self.siril.connected:
                self.siril.progress_reset()

            QtWidgets.QMessageBox.information(
                self, "Prepare",
                f"Done.\nOK: {ok}\nFailed: {fail}\n\nLog: {log_path}"
            )
            # After you've loaded or validated project paths in prepare:
            try:
                if not getattr(self.project, "frame_width", 0) or not getattr(self.project, "frame_height", 0):
                    cand = _find_any_light_path(self.project)
                    if cand:
                        w, h = _read_fits_size_quick(cand)
                        if w and h:
                            self.project.frame_width = int(w)
                            self.project.frame_height = int(h)
                            # (Optional) log to console or status bar so user sees it was detected
                            print(f"Detected frame size: {w} x {h} from {cand}")
                            self._update_feather_from_overlap()
                        else:
                            print(f"Could not read NAXIS1/2 from first light; feather auto-link will remain manual.")
            except Exception as e:
                print(f"Frame-size detection error: {e}")
        finally:
            # restore original dirty state and lift the guard
            self._suspend_dirty = False
            self._dirty = was_dirty

    # --- Helpers: determines what masters this session will actually have access to ---

    # --- ProjectWidget helpers: library master lookup -----------------
    def _find_library_master(self, kind: str, sess) -> Optional[str]:
        """
        Return a path to a library master for `kind` ('bias'|'dark'|'flat') or None.
        Looks under project.master_library_dir for common Siril master names.
        You can customize the patterns to match your library.
        """
        p = self.project
        lib_root = Path(getattr(p, "master_library_dir", "") or "")
        if not lib_root or not lib_root.exists():
            return None

        # Try a few common names. Add your own if needed.
        patterns = {
            "bias": ["master_bias*.fit", "bias_stacked*.fit", "pp_bias_stacked*.fit"],
            "dark": ["master_dark*.fit", "dark_stacked*.fit", "pp_dark_stacked*.fit"],
            "flat": ["master_flat*.fit", "flat_stacked*.fit", "pp_flat_stacked*.fit"],
        }.get(kind.lower(), [])

        for pat in patterns:
            for cand in lib_root.glob(pat):
                return cand.as_posix()

        # Optional: allow subfolders per kind
        sub = lib_root / kind.lower()
        if sub.exists():
            for cand in sub.glob("*.fit*"):
                return cand.as_posix()

        return None

    def _lib_has_master(self, kind: str, sess, p) -> bool:
        """
        If 'Use Siril Master Library' is ON, assume Siril can provide the master
        via $defbias/$defdark/$defflat at runtime. We can’t verify those here,
        so we optimistically return True. (Optional local folder scan kept as best-effort.)
        """
        if getattr(p, "use_master_library", False):
            return True  # trust Siril’s Master Library preference

        # Optional: keep your local scan if you configured one
        if hasattr(self, "_find_library_master"):
            return bool(self._find_library_master(kind, sess))
        return False

    def _session_calib_availability(self, sess, p):
        """
        Returns a dict describing what this session can actually use, considering
        overrides AND library. Adjust attribute names if yours differ.
        """
        has_bias_override = bool(getattr(sess, "master_bias", None))
        has_dark_override = bool(getattr(sess, "master_dark", None))
        has_flat_override = bool(getattr(sess, "master_flat", None))
        has_df_override   = bool(getattr(sess, "master_dark_flat", None))

        has_bias_lib = self._lib_has_master("bias", sess, p)
        has_dark_lib = self._lib_has_master("dark", sess, p)
        has_flat_lib = self._lib_has_master("flat", sess, p)
        has_df_lib   = self._lib_has_master("dark_flat", sess, p)

        # Raw flats present (we can build a master flat from them)
        has_raw_flats = bool(getattr(sess, "flats", []) or getattr(sess, "dark_flats", []))

        return {
            "has_bias": has_bias_override or has_bias_lib,
            "has_dark": has_dark_override or has_dark_lib,
            "has_master_flat": has_flat_override or has_flat_lib,
            "has_master_darkflat": has_df_override or has_df_lib,
            "has_raw_flats": has_raw_flats,
        }

    def _validate_calibration_or_warn(self, p) -> bool:
        """
        Return True if calibration coverage is sufficient (or user accepted proceeding),
        False if the user cancels.

        Rules:
        • Non-mosaic (per session): DARKS and FLATS and (BIAS or DARK-FLATS)
        • Mosaic (per panel with lights): DARKS OR [ FLATS and (BIAS or DARK-FLATS) ]

        When 'Use Siril Master Library' is ON, we assume Siril provides $defbias/$defdark/$defflat
        (and dark-flats as needed) at runtime; we treat those as available here.
        """
        sessions = list(getattr(p, "sessions", []) or [])
        if not sessions:
            return True

        use_lib = bool(getattr(p, "use_master_library", False))
        is_mosaic = bool(getattr(p, "mosaic_enabled", False))

        def has_any(seq) -> bool:
            return bool(list(seq or []))

        # -------- Mosaic path: validate per panel that actually has lights --------
        if is_mosaic:
            missing = []   # list of strings describing panels with missing calibration requirements

            for sess in sessions:
                panels = list(getattr(sess, "panels", []) or [])
                if not panels:
                    continue

                for pan in panels:
                    lights = list(getattr(pan, "lights", []) or [])
                    if not lights:
                        continue  # nothing to calibrate for this panel

                    # Resolve cal paths (panel → session → library)
                    md = mf = mb = mdf = None
                    try:
                        md, mf, mb, mdf = _resolve_cal_paths(p, sess, panel=pan)
                    except Exception:
                        md  = getattr(pan, "master_dark", None) or getattr(sess, "master_dark", None)
                        mf  = getattr(pan, "master_flat", None) or getattr(sess, "master_flat", None)
                        mb  = getattr(pan, "master_bias", None) or getattr(sess, "master_bias", None)
                        mdf = (getattr(pan, "master_dark_flat", None) or getattr(pan, "master_darkflat", None)
                            or getattr(sess, "master_dark_flat", None) or getattr(sess, "master_darkflat", None))

                    use_lib            = bool(getattr(p, "use_master_library", False))
                    has_dark_any       = bool(md) or use_lib
                    has_master_flat    = bool(mf) or use_lib
                    has_raw_flats      = bool(getattr(pan, "flats", []) or [])
                    has_bias_or_df     = bool(mb or mdf) or use_lib

                    # Missing conditions we want to surface in the same Yes/No prompt
                    no_flats                 = (not has_master_flat) and (not has_raw_flats)
                    raw_flats_no_support     = has_raw_flats and (not has_bias_or_df)
                    no_darks                 = not has_dark_any

                    # If any of those are true, add a single line item describing *all* reasons for this panel
                    if no_flats or raw_flats_no_support or no_darks:
                        pid = getattr(pan, "panel_id", None) or getattr(pan, "id", None) or "Panel"
                        reasons = []
                        if no_darks:
                            reasons.append("no usable darks")
                        if no_flats:
                            reasons.append("no flats")
                        if raw_flats_no_support:
                            reasons.append("raw flats present but no bias/dark-flats")
                        missing.append(f"- {getattr(sess,'name','Session')} / {pid}: " + " and ".join(reasons))

            if not missing:
                return True

            if getattr(p, "allow_uncalibrated", False):
                self._info(
                    "Proceeding with missing calibration items for:\n\n" + "\n".join(missing) +
                    "\n\nTip: attach per-panel/session masters or enable the Master Library."
                )
                return True

            return self._confirm(
                "Calibration items are missing for some panels:\n\n"
                + "\n".join(missing)
                + "\n\nYou can:\n"
                + " • Attach per-panel/session master dark/flat (or dark-flat / bias), or\n"
                + " • Enable/fix the Master Library, or\n"
                + " • Check “Allow no calibration frames” to proceed anyway.\n\n"
                + "Proceed anyway?"
            )

        # -------- Non-mosaic path: validate per session --------
        missing = []

        for sess in sessions:
            lights = list(getattr(sess, "lights", []) or [])
            if not lights:
                continue  # nothing to calibrate

            # Raw frames
            flats       = list(getattr(sess, "flats", []) or [])
            dark_flats  = list(getattr(sess, "dark_flats", []) or [])
            darks       = list(getattr(sess, "darks", []) or [])

            # Masters (accept both spellings for dark-flat)
            m_bias      = getattr(sess, "master_bias", None)
            m_dark      = getattr(sess, "master_dark", None)
            m_flat      = getattr(sess, "master_flat", None)
            m_dark_flat = getattr(sess, "master_dark_flat", None) or getattr(sess, "master_darkflat", None)

            use_lib            = bool(getattr(p, "use_master_library", False))
            has_dark_any       = bool(m_dark) or bool(darks) or use_lib
            has_master_flat    = bool(m_flat) or use_lib
            has_raw_flats      = bool(flats)
            has_bias_or_df     = bool(m_bias or m_dark_flat or dark_flats) or use_lib

            # Missing conditions to surface in the single Yes/No prompt
            no_flats             = (not has_master_flat) and (not has_raw_flats)
            raw_flats_no_support = has_raw_flats and (not has_bias_or_df)
            no_darks             = not has_dark_any

            if no_flats or raw_flats_no_support or no_darks:
                reasons = []
                if no_darks:
                    reasons.append("no usable darks")
                if no_flats:
                    reasons.append("no flats")
                if raw_flats_no_support:
                    reasons.append("raw flats present but no bias/dark-flats")
                pretty = getattr(sess, "name", "Session")
                missing.append(f"- {pretty}: " + " and ".join(reasons))

        if not missing:
            return True

        if getattr(p, "allow_uncalibrated", False):
            self._info(
                "Proceeding with missing calibration items for:\n\n"
                + "\n".join(missing)
                + "\n\nTip: attach per-session masters or enable the Master Library."
            )
            return True

        return self._confirm(
            "Calibration items are missing for some sessions:\n\n"
            + "\n".join(missing)
            + "\n\nYou can:\n"
            + " • Attach per-session master dark/flat (or dark-flat / bias), or\n"
            + " • Enable/fix the Master Library, or\n"
            + " • Check “Allow no calibration frames” to proceed anyway.\n\n"
            + "Proceed anyway?"
        )

    def build_script(self):
        # Preserve current dirty state — building an SSF is not a project save
        was_dirty = getattr(self, "_dirty", False)
        self._suspend_dirty = True
        try:
            self.push_to_model()  # copy UI → model (must NOT clear _dirty)
            p = self.project
            proceed = self._validate_calibration_or_warn(p)
            if not proceed:
                return         
            # --- NEW: warn if any mosaic panels have no lights ---
            if getattr(p, "mosaic_enabled", False):
                missing = []
                for s in p.sessions:
                    for pan in getattr(s, "panels", []) or []:
                        if not getattr(pan, "lights", []):
                            missing.append(f"{s.name} / {pan.panel_id or 'A1'}")
                if missing:
                    msg = (
                        "The following panels have no LIGHT frames:\n\n"
                        + "\n".join(missing)
                        + "\n\nContinue and skip these panels?"
                    )
                    resp = QtWidgets.QMessageBox.question(
                        self,
                        "Missing Lights in Panels",
                        msg,
                        QtWidgets.QMessageBox.StandardButton.Yes
                        | QtWidgets.QMessageBox.StandardButton.No,
                        QtWidgets.QMessageBox.StandardButton.No,
                    )
                    if resp != QtWidgets.QMessageBox.StandardButton.Yes:
                        return
            # --- END NEW ---
            out = Path(p.working_dir) / "run_project.ssf"
            script_text = SirilCommandBuilder(p).build()
            if not isinstance(script_text, str) or not script_text.strip():
                QtWidgets.QMessageBox.critical(self, "Build Siril Script",
                                            "Generated script is empty.")
                return
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(script_text, encoding="utf-8")
            if self.siril.connected:
                self.siril.log(f"[script] Wrote {out} ({len(script_text.splitlines())} lines)")
            QtWidgets.QMessageBox.information(
                self, "Build Siril Script", f"Script written to:\n{out}"
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Build Siril Script",
                                        f"Failed to build:\n{e}")
        finally:
            # Restore whatever the dirty state was before building
            self._suspend_dirty = False 
            self._dirty = was_dirty

    def run_siril(self):
        from datetime import datetime

        # Do NOT let a run clear the dirty flag
        was_dirty = bool(getattr(self, "_dirty", False))
        self._suspend_dirty = True
        try:
            self.push_to_model()
        finally:
            self._suspend_dirty = False
        p = self.project
        if not p.working_dir:
            QtWidgets.QMessageBox.warning(self, "Run", "Please set a working directory.")
            return
        script_path = Path(p.working_dir) / "run_project.ssf"
        if not script_path.exists():
            QtWidgets.QMessageBox.warning(self, "Run", "Script not found. Click 'Build Siril Script' first.")
            return
        proceed = self._validate_calibration_or_warn(p)
        if not proceed:
            return
        self.lbl_run_mode.setText("Run mode: deciding…")
        self.lbl_run_mode.setStyleSheet("color: #666666; font-style: italic;")
    
        # --- Decide API vs CLI ---
        force_cli = bool(getattr(p, "force_cli", False))
        api_available = getattr(self, "siril", None) and getattr(self.siril, "iface", None)

        # --- PREFERRED: run inside Siril via Python API (unless force_cli is set) ---
        if api_available and not force_cli:
            try:
                # One place to say which mode we're using
                if self.siril.connected:
                    self.siril.log("[run] In-Siril execution selected (using Siril Python API).")

                # Compute project directory and log what we are about to run
                proj_dir = Path(self.project.working_dir or p.working_dir).as_posix()
                if self.siril.connected:
                    self.siril.log(f'[run] Executing SSF inside Siril: cd \"{proj_dir}\" ; @run_project.ssf')

                # cd into the project working directory then run the script by name
                self.siril.iface.cmd(f'cd \"{proj_dir}\"')
                self.siril.iface.cmd('@run_project.ssf')

                # Update run-mode label
                if hasattr(self, "lbl_run_mode"):
                    self.lbl_run_mode.setText("Run mode: Siril Python API (in-process)")
                    self.lbl_run_mode.setStyleSheet("color: #2e7d32;")

                # Let the user know this is fire-and-forget from the GUI perspective
                if self.siril.connected:
                    self.siril.log(
                        "[run] run_project.ssf submitted to Siril. "
                        "Follow progress in Siril's log/progress bar; use Siril's Stop button to abort."
                    )

                QtWidgets.QMessageBox.information(
                    self,
                    "Run (Siril API)",
                    "Siril has started run_project.ssf using the Python API.\n\n"
                    "Watch Siril's progress bar and log window for completion.\n"
                    "To abort, click the Stop button in Siril.",
                )
                # Do not change dirty state here
                return
            except Exception as e:
                # If in-Siril run failed, note and fall back to CLI
                if self.siril.connected:
                    self.siril.log(f"[run] In-Siril execution failed, will try CLI. Error: {e}")
                if hasattr(self, "lbl_run_mode"):
                    self.lbl_run_mode.setText("Run mode: Siril Python API failed → falling back to CLI")
                    self.lbl_run_mode.setStyleSheet("color: #f57c00;")

        # If we reach here, we will run via CLI. Reset run-mode label baseline if present.
        if hasattr(self, "lbl_run_mode"):
            self.lbl_run_mode.setText("Run mode: siril-cli (pending launch)")
            self.lbl_run_mode.setStyleSheet("color: #666666; font-style: italic;")

        # --- FALLBACK: run via siril-cli (your existing behavior) ---
        siril = find_siril_cli(p.siril_cli_path)
        if not siril:
            f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Locate siril-cli")
            if not f:
                QtWidgets.QMessageBox.critical(self, "Run", "siril-cli not found.")
                return
            siril = f
            # marks dirty (expected)
            self.ed_siril.setText(f)

        # Version (for log / drizzle guard)
        ver = get_siril_version(siril)
        self._run_siril_verstr = (
            f"{ver[0]}.{ver[1]}.{ver[2]}" if ver is not None else "unknown"
        )

        # Update run-mode label for CLI
        if hasattr(self, "lbl_run_mode"):
            if self._run_siril_verstr != "unknown":
                self.lbl_run_mode.setText(f"Run mode: siril-cli ({self._run_siril_verstr})")
            else:
                self.lbl_run_mode.setText("Run mode: siril-cli")
            self.lbl_run_mode.setStyleSheet("color: #1565c0;")

        # Drizzle still explicitly requires 1.4+ (no GUI/CLI comparison anymore)
        if ver is not None and p.drizzle_enabled and (ver[0], ver[1]) < (1, 4):
            QtWidgets.QMessageBox.warning(
                self,
                "Siril Version",
                f"Detected siril-cli {self._run_siril_verstr}. Drizzle requires 1.4+.",
            )

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = Path(p.working_dir) / f"siril_run_{ts}.log"

        creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if platform.system() == "Windows" else 0
        try:
            self._run_started_at = datetime.now()
            with open(log_path, "a", encoding="utf-8", newline="") as lf:
                lf.write(f"=== Multi-Night Stacking run started at {self._run_started_at.isoformat()} ===\n")
                lf.write(f"Siril CLI   : {siril}\n")
                lf.write(f"Siril Version: {self._run_siril_verstr}\n")
                lf.write(f"Working Dir : {p.working_dir}\n")
                lf.write(f"Script      : {script_path}\n\n")

            self._proc = subprocess.Popen(
                [siril, "-s", str(script_path)],
                cwd=str(p.working_dir),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",       # <<< add
                errors="replace",       # <<< add (prevents crashes on weird bytes)
                bufsize=1,
                creationflags=creationflags,
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Run", f"Failed to launch siril-cli: {e}")
            return

        # Toggle buttons during run
        self.btn_run_siril.setEnabled(False)
        self.btn_abort.setEnabled(True)
        self.btn_prepare.setEnabled(False)
        self.btn_build_script.setEnabled(False)

        if self.siril.connected:
            self.siril.log(f"[siril-cli] Launching: {siril} -s {script_path}")
            self.siril.log(f"[siril-cli] Siril Version: {self._run_siril_verstr}")
            self.siril.log(f"[siril-cli] CWD: {p.working_dir}")
            self.siril.log(f"[siril-cli] Log: {log_path}")

        # Reader thread (unchanged from your current code)
        self._reader = _ProcReader(self._proc, log_path, self.siril, parent=self)
        self._last_pct = -1.0
        self._last_emit = 0.0
        self._progress_interval = 0.22
        self._progress_min_delta = 0.3

        def on_lines(lines: list[str]):
            if not self.siril.connected:
                return
            now = time.monotonic()
            maybe_emit = (now - self._last_emit) >= self._progress_interval
            for ln in lines:
                self.siril.log(ln.rstrip())
                m = re.search(r"progress:\s.*?(\d{1,3}(?:\.\d+)?)\s*%", ln)
                if m:
                    try: pct = float(m.group(1))
                    except ValueError: pct = None
                    if pct is not None:
                        delta_ok = (self._last_pct < 0) or (abs(pct - self._last_pct) >= self._progress_min_delta)
                        if delta_ok or maybe_emit:
                            self._last_pct = pct
                            self._last_emit = now
                            try:
                                self.siril.progress("Running siril-cli…", max(0.0, min(1.0, pct / 100.0)))
                            except Exception:
                                pass

        def on_finished(rc: int):
            if self.siril.connected:
                try:
                    self.siril.progress("Done", 1.0)
                except Exception:
                    pass
                self.siril.progress_reset()
            self.btn_run_siril.setEnabled(True)
            self.btn_abort.setEnabled(False)
            self.btn_prepare.setEnabled(True)
            self.btn_build_script.setEnabled(True)

            end_dt = datetime.now()
            start_dt = self._run_started_at
            self._run_started_at = None
            def _fmt_elapsed(start_dt, end_dt):
                try:
                    total = int((end_dt - start_dt).total_seconds())
                    h = total // 3600
                    m = (total % 3600) // 60
                    s = total % 60
                    if h: return f"{h}h {m}m {s}s"
                    if m: return f"{m}m {s}s"
                    return f"{s}s"
                except Exception:
                    return "n/a"
            elapsed_txt = _fmt_elapsed(start_dt, end_dt) if start_dt else "n/a"
            try:
                with open(log_path, "a", encoding="utf-8", newline="") as lf:
                    lf.write(f"\n=== Finished at {end_dt.isoformat()} (elapsed {elapsed_txt}) ===\n")
            except Exception:
                pass

            if self.siril.connected:
                self.siril.log(f"[siril-cli] Finished (rc={rc}) in {elapsed_txt} — Siril {self._run_siril_verstr}.")

            if rc == 0:
                try:
                    proj_slug = safe_slug(self.project.name)
                    final_path = Path(self.project.working_dir) / f"{proj_slug}_final.fit"
                    if final_path.exists() and getattr(self.siril, "iface", None):
                        self.siril.iface.cmd(f'load "{final_path.as_posix()}"')
                        self.siril.log(f"[viewer] Loaded final image: {final_path}")
                except Exception as e:
                    if self.siril.connected:
                        self.siril.log(f"[viewer] Failed to load final image: {e}")
                QtWidgets.QMessageBox.information(self, "Run (CLI)", f"Processing finished in {elapsed_txt}.\n\nLog saved to:\n{log_path}")
            else:
                QtWidgets.QMessageBox.critical(self, "Run (CLI)", f"siril-cli exited with code {rc} after {elapsed_txt}.\n\nLog:\n{log_path}")

            self._proc = None
            self._reader = None
            if not getattr(self, "_dirty", False) and was_dirty:
                self._dirty = True

        self._reader.got_lines.connect(on_lines)
        self._reader.finished_ok.connect(on_finished)
        self._reader.start()

    def abort_siril(self):
        if not self._proc:
            QtWidgets.QMessageBox.information(self, "Abort", "No siril-cli process is running.")
            return
        ans = QtWidgets.QMessageBox.question(
            self, "Abort Run",
            "Stop the current Siril job?",
            QtWidgets.QMessageBox.StandardButton.Yes | QtWidgets.QMessageBox.StandardButton.No,
            QtWidgets.QMessageBox.StandardButton.Yes,
        )
        if ans != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        try:
            if platform.system() == "Windows":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                self._proc.send_signal(signal.SIGINT)
            if hasattr(self, "_reader") and self._reader:
                self._reader.stop()
        except Exception:
            pass
        # Escalation remains the same as you already had…


    def _escalate_abort(self):
        if not self._proc: return
        try:
            if self._proc.poll() is None:
                self._proc.terminate()
                QtCore.QTimer.singleShot(800, self._kill_abort)
        except Exception:
            self._kill_abort()

    def _kill_abort(self):
        if not self._proc: return
        try:
            if self._proc.poll() is None:
                self._proc.kill()
        except Exception: pass
        self._proc = None
        if self._run_timer: self._run_timer.stop()
        if self.siril.connected:
            self.siril.progress_reset()
            self.siril.log("[abort] Process killed.")
        self.btn_run_siril.setEnabled(True)
        self.btn_abort.setEnabled(False)
        self.btn_prepare.setEnabled(True)
        self.btn_build_script.setEnabled(True)
        QtWidgets.QMessageBox.information(self, "Abort", "Processing aborted.")


    # ------------------------
    # Panel helpers (new)
    # ------------------------
    def _current_session(self):
        idx = self.sessions_list.currentRow()
        return self.project.sessions[idx] if 0 <= idx < len(self.project.sessions) else None

    def _current_session_has_panels(self):
        s = self._current_session()
        return bool(s and s.panels)

    def _refresh_panels_ui_for_session(self, sess):
        # Start by clearing the editor to avoid stale carry-over
        self._loading_panel = True
        try:
            self.panel_editor.from_panel(None)
        finally:
            self._loading_panel = False

        self.lst_panels.clear()
        mosaic_on = self.chk_mosaic_enabled.isChecked()

        if not sess:
            # No session selected
            # - Session frame lists obey Mosaic: disabled when Mosaic is ON
            self.session_editor.set_frame_groups_enabled(not mosaic_on)
            # - Panel frame lists disabled, no copy button
            self.panel_editor.set_frame_groups_enabled(False)
            self.panel_editor.set_copy_source_panel(None, enabled=False)
            return

        # Populate left-hand panels list
        for p in getattr(sess, "panels", []):
            self.lst_panels.addItem(p.panel_id or "")

        has_panels = bool(sess.panels)

        if has_panels:
            self.lst_panels.setCurrentRow(0)
            # Load first panel under guard
            self._loading_panel = True
            try:
                self.panel_editor.from_panel(sess.panels[0])
            finally:
                self._loading_panel = False

            # Configure the 'copy calibration frames' button based on panel count
            first_id = sess.panels[0].panel_id or "A1"
            can_copy = mosaic_on and (len(sess.panels) > 1)
            self.panel_editor.set_copy_source_panel(first_id, enabled=can_copy)
        else:
            # No panels: hide/disable the copy button
            self.panel_editor.set_copy_source_panel(None, enabled=False)

        # --- Final enable/disable rules ---

        # Session tab frame lists:
        #   always greyed out when Mosaic is enabled,
        #   enabled when Mosaic is OFF.
        self.session_editor.set_frame_groups_enabled(not mosaic_on)

        # Panel tab frame lists:
        #   enabled only when Mosaic is ON AND there is at least one panel.
        #   Otherwise greyed out.
        self.panel_editor.set_frame_groups_enabled(mosaic_on and has_panels)

    def load_selected_panel(self, row: int):
        s = self._current_session()
        self._loading_panel = True
        try:
            if not s or not (0 <= row < len(s.panels)):
                self.panel_editor.from_panel(None)
                return
            self.panel_editor.from_panel(s.panels[row])
        finally:
            self._loading_panel = False

    def update_current_panel(self):
        # Ignore writes while we’re programmatically updating the editor
        if getattr(self, "_loading_panel", False):
            return

        s = self._current_session()
        row = self.lst_panels.currentRow()
        if not s or not (0 <= row < len(s.panels)):
            return

        s.panels[row] = self.panel_editor.to_panel()
        # keep the list label in sync
        self.lst_panels.item(row).setText(s.panels[row].panel_id or f"A{row+1}")
        self.mark_dirty()

    def _on_copy_cals_from_first_panel(self):
        """Copy calibration frame lists from the first panel to all other panels in the session.

        This operates on Bias, Darks, Flats and Dark Flats only – Lights are not affected.
        """
        sess = self._current_session()
        if not sess:
            return

        panels = getattr(sess, "panels", [])
        if len(panels) < 2:
            QtWidgets.QMessageBox.information(
                self,
                "Copy Calibration Frames",
                "You need at least two panels in the current session to use this feature.",
            )
            return

        src = panels[0]

        # Copy calibration frame lists (make new lists, do not share references)
        for pan in panels[1:]:
            pan.bias = list(src.bias)
            pan.darks = list(src.darks)
            pan.flats = list(src.flats)
            pan.dark_flats = list(src.dark_flats)

        # Refresh the currently selected panel in the editor so the user sees the change
        row = self.lst_panels.currentRow()
        if 0 <= row < len(panels):
            self._loading_panel = True
            try:
                self.panel_editor.from_panel(panels[row])
            finally:
                self._loading_panel = False

        self.mark_dirty()

    # ---------------- file I/O ----------------
    def new_project(self):
        self.project = Project()
        self.project.sessions = [Session(name="Session 1")]
        self.refresh_from_model()
        self._dirty = True

    def open_project(self) -> bool:
        f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Open Project", filter="Project (*.json)")
        if not f:
            return False
        try:
            self.project = Project.from_dict(json.loads(Path(f).read_text(encoding="utf-8")))
            self.project.project_file = f
            if not self.project.sessions:
                self.project.sessions = [Session(name="Session 1")]
            self.refresh_from_model()
            self._dirty = False
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Open Project", f"Failed: {e}")
            return False

    def load_project_file(self, path: str) -> bool:
        """Load a project JSON from a path (no dialogs). Returns True on success."""
        try:
            txt = Path(path).read_text(encoding="utf-8")
            data = json.loads(txt)
            proj = Project.from_dict(data)
            # ensure at least one session
            if not getattr(proj, "sessions", None):
                proj.sessions = [Session(name="Session 1")]
            self.project = proj
            self.project.project_file = path
            self._project_path = path
            self._dirty = False
            # refresh UI from model
            self.refresh_from_model()
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Open Project", f"Failed to load:\n{e}")
            return False

    def save_project(self) -> bool:
        if not self.project.project_file:
            return self.save_project_as()
        self.push_to_model()
        try:
            Path(self.project.project_file).write_text(
                json.dumps(self.project.to_dict(), indent=2), encoding="utf-8"
            )
            self._dirty = False
            return True
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Save Project", f"Failed: {e}")
            return False

    def save_project_as(self) -> bool:
        # Pick a reasonable starting folder
        start_dir = ""
        if getattr(self, "_project_path", None):
            start_dir = os.path.dirname(self._project_path)
        elif getattr(self.project, "working_dir", None):
            start_dir = self.project.working_dir

        f, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Save Project As", start_dir, "Project (*.json)"
        )
        if not f:
            return False
        if not f.lower().endswith(".json"):
            f += ".json"

        # Update both pointers so downstream code sees the path
        self.project.project_file = f
        self._project_path = f

        # Delegate the actual write (and _dirty reset) to save_project()
        return self.save_project()

class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self._suppress_resize_dirty = True
        self.setWindowTitle("OSC Multi-Night Stacking (Siril 1.4) Version 2.1")

        self.proj_widget = ProjectWidget()
        self.setCentralWidget(self.proj_widget)

        self.status = self.statusBar()
        self.proj_widget.status_message.connect(self.status.showMessage)

        # --- Menu setup ---
        m = self.menuBar()
        file_menu = m.addMenu("&File")
        file_menu.addAction(self.proj_widget.action_new)
        file_menu.addAction(self.proj_widget.action_open)
        file_menu.addAction(self.proj_widget.action_save)
        file_menu.addAction(self.proj_widget.action_save_as)

        # --- Add Exit action ---
        exit_action = QtGui.QAction("E&xit", self)
        exit_action.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Quit))  # or "Ctrl+Q"
        exit_action.setMenuRole(QtGui.QAction.MenuRole.QuitRole)
        exit_action.triggered.connect(self.close)
        file_menu.addSeparator()
        file_menu.addAction(exit_action)


        # --- Connect menu actions ---
        self.proj_widget.action_new.triggered.connect(self.on_file_new)
        self.proj_widget.action_open.triggered.connect(self.on_file_open)
        self.proj_widget.action_save.triggered.connect(self.proj_widget.save_project)
        self.proj_widget.action_save_as.triggered.connect(self.proj_widget.save_project_as)

        # After layout is set up
        QtCore.QTimer.singleShot(0, self._rebalance_after_show)

        # existing About action creation can remain as-is, just avoid creating a second Help menu
        help_menu = m.addMenu("&Help")  # you already have one; keep a single instance
        about_action = QtGui.QAction("About", self)
        help_menu.addAction(about_action)
        about_action.triggered.connect(lambda: QtWidgets.QMessageBox.information(
            self, "About",
            "OSC Multi-Night Stacking for Siril 1.4 (PyQt6) Version 2.1\n"
            "• Drizzle: Scaling, Pixel Fraction, Kernel\n"
            "• 2-pass registration toggle\n"
            "• Global stacking options (sigma or winsorized rejection (sigma high and low), mean)\n"
            "• 32-bit output for final stack and intermediate file compression toggles\n"
            "• Siril console logging via sirilpy\n"
            "• Abort Run button (graceful stop)\n"
            "• Final stack copied, mirrored, and opened in Siril"
        ))

        # ---- Help → Quick Start Instructions ----
        menubar = self.menuBar()
        help_menu = None
        for act in menubar.actions():
            if act.text().replace("&", "").lower() == "help":
                help_menu = act.menu()
                break
        if help_menu is None:
            help_menu = menubar.addMenu("&Help")

        act_quickstart = QtGui.QAction("Quick Start Instructions", self)
        act_quickstart.setShortcut(QtGui.QKeySequence("F1"))
        act_quickstart.triggered.connect(self.show_quick_start)

        # Insert near the top of Help
        if help_menu.actions():
            help_menu.insertAction(help_menu.actions()[0], act_quickstart)
        else:
            help_menu.addAction(act_quickstart)

        # Optional: hook for future auto-show (still self-contained)
        QtCore.QTimer.singleShot(0, self.maybe_autoshow_quickstart)

        # existing About action creation can remain as-is, just avoid creating a second Help menu

        # Size adaptively to the screen (works well on 1080p laptops)
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        w = min(1450, int(avail.width() * 0.95))
        h = min(880,  int(avail.height() * 0.9))
        self.resize(w, h)
        # Center on screen after initial sizing
        frame_geom = self.frameGeometry()
        center_point = QtGui.QGuiApplication.primaryScreen().availableGeometry().center()
        frame_geom.moveCenter(center_point)
        self.move(frame_geom.topLeft())
        self._apply_remembered_window_size()
        
        # Size adaptively to the screen (works well on 1080p laptops)
        avail = QtGui.QGuiApplication.primaryScreen().availableGeometry()
        w = min(1450, int(avail.width() * 0.95))
        h = min(880,  int(avail.height() * 0.9))
        self.resize(w, h)

        # Center on screen after initial sizing
        frame_geom = self.frameGeometry()
        center_point = QtGui.QGuiApplication.primaryScreen().availableGeometry().center()
        frame_geom.moveCenter(center_point)
        self.move(frame_geom.topLeft())

        self._apply_remembered_window_size()

        # turn back on after first paint cycle so user resizes will mark dirty
        QtCore.QTimer.singleShot(0, lambda: setattr(self, "_suppress_resize_dirty", False))


    def maybe_save(self)->bool:
        pw=self.proj_widget
        if not getattr(pw,"_dirty",False): return True
        btn=QtWidgets.QMessageBox.question(self,"Unsaved changes",
             "You have unsaved changes. Save before closing?",
             QtWidgets.QMessageBox.StandardButton.Save|QtWidgets.QMessageBox.StandardButton.Discard|QtWidgets.QMessageBox.StandardButton.Cancel,
             QtWidgets.QMessageBox.StandardButton.Save)
        if btn==QtWidgets.QMessageBox.StandardButton.Save:
            pw.save_project(); return not getattr(pw,"_dirty",False)
        if btn==QtWidgets.QMessageBox.StandardButton.Discard: return True
        return False
    
    def closeEvent(self, e: QtGui.QCloseEvent):
        if not self.maybe_save():
            e.ignore()              # cancel close
            return
        super().closeEvent(e)       # allow normal close + cleanup

    # ------------------------------
    # File menu handlers
    # ------------------------------

    # ---- Quick Start plumbing (no persistence yet) ----
    def show_quick_start(self):
        dlg = QuickStartDialog(QUICK_START_MD, self)
        dlg.exec()

    def _should_autoshow_quickstart(self) -> bool:
        """
        Placeholder for future startup behavior. Returns False to keep
        everything self-contained now. If approved later, switch this to
        read a QSettings flag or an env var/CLI flag.
        """
        return False

    def maybe_autoshow_quickstart(self):
        if self._should_autoshow_quickstart():
            dlg = QuickStartDialog(QUICK_START_MD, self)
            dlg.setModal(False)
            dlg.show()

    def on_file_new(self):
        """Create a new empty project after prompting to save unsaved changes."""
        if not self.maybe_save():
            return
        self.proj_widget.project = Project()
        if not getattr(self.proj_widget.project, "sessions", None):
            self.proj_widget.project.sessions = [Session(name="Session 1")]
        self.proj_widget._project_path = None
        self.proj_widget._dirty = False
        self.proj_widget.refresh_from_model()
        self.status.showMessage("Created new project", 4000)

    def on_file_open(self):
        """Prompt to save, then open a project file."""
        if not self.maybe_save():
            return
        path, _ = QtWidgets.QFileDialog.getOpenFileName(
            self, "Open Project", "", "Project JSON (*.json)"
        )
        if not path:
            return
        ok = self.proj_widget.load_project_file(path)
        if ok:
            self.proj_widget._project_path = path
            self.proj_widget._dirty = False
            self.proj_widget.refresh_from_model()
            self._apply_remembered_window_size()
            self.status.showMessage(f"Opened project: {os.path.basename(path)}", 4000)

    def _rebalance_after_show(self):
        # Re-run sizing once we have a real window size
        try:
            # Find the splitter in the project widget
            splitters = self.proj_widget.findChildren(QtWidgets.QSplitter)
            if splitters:
                self.proj_widget._init_splitter_sizes(splitters[0])
        except Exception:
            pass

    def _apply_remembered_window_size(self):
        p = getattr(self.proj_widget, "project", None)
        try:
            if p and getattr(p, "remember_window_size", False) and p.window_w and p.window_h:
                self._suppress_resize_dirty = True
                try:
                    self.resize(int(p.window_w), int(p.window_h))
                finally:
                    # allow user-driven resizes to mark dirty afterwards
                    QtCore.QTimer.singleShot(0, lambda: setattr(self, "_suppress_resize_dirty", False))
        except Exception:
            pass

    def resizeEvent(self, e: QtGui.QResizeEvent) -> None:
        try:
            p = getattr(self.proj_widget, "project", None)

            # Ignore “phantom” resizes while a modal dialog (e.g., Preview) is open
            if QtWidgets.QApplication.activeModalWidget() is not None:
                super().resizeEvent(e)
                return

            if p and getattr(p, "remember_window_size", False):
                sz = e.size()
                new_w, new_h = int(sz.width()), int(sz.height())
                old_w = int(p.window_w) if p.window_w else 0
                old_h = int(p.window_h) if p.window_h else 0

                # Ignore tiny jiggles (<= 4 px) that often happen on dialog show/close
                if abs(new_w - old_w) <= 4 and abs(new_h - old_h) <= 4:
                    super().resizeEvent(e)
                    return

                changed = (new_w != old_w) or (new_h != old_h)
                p.window_w, p.window_h = new_w, new_h

                # Only mark dirty for meaningful, user-driven resizes
                if changed and not getattr(self, "_suppress_resize_dirty", False):
                    try:
                        self.proj_widget.mark_dirty()
                        # optional: self.status.showMessage("Window size changed (will be saved).", 2500)
                    except Exception:
                        pass
        except Exception:
            pass

        super().resizeEvent(e)

def main():
    try:
        QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception: pass
    app=QtWidgets.QApplication(sys.argv); w=MainWindow(); w.show(); sys.exit(app.exec())

if __name__=="__main__": main()
