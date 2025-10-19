#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, 
# or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
# See <https://www.gnu.org/licenses/>.
"""
Multi-Night Stacking for Siril 1.4 (PyQt6) — with Siril console integration (sirilpy) - version 1.0

What's inside:
- JSON projects (persist drizzle options + 2-pass flag); new project starts with one session.
- Prepare Working Directory (symlink→hardlink→copy) with logs + Siril console progress.
- Master Library or per-session master overrides (bias/dark/flat); OSC-first pipeline.
- Drizzle workflow per Siril 1.4 docs:
    * Drizzle ON: NO -debayer during calibrate; register (-layer=0 [+ -2pass]); seqapplyreg (-scale, -drizzle, -pixfrac, -kernel); stack r_*.
    * Drizzle OFF: include -debayer in calibrate; register (-layer=0 [+ -2pass]); stack r_*.
- Global stack options (rej / wrej / mean + sigma low/high), sigma controls hide for Mean.
- SSF: requires 1.3.4, setcompress 0, setfindstar reset (start & end), final close.
- Final save to <project_slug>_final.fit and auto-open in Siril.
- Remove Session (config+name+data) and Remove Data (All Sessions) (data only).
- Guarded session switching to prevent file list cross-contamination.
- NEW: Abort Run (graceful stop of siril-cli).
"""
from __future__ import annotations

import json, os, platform, re, shutil, subprocess, sys, signal, time
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import List, Dict, Optional, Tuple
from PyQt6 import QtCore, QtGui, QtWidgets

# Optional: Siril Python API
try:
    import sirilpy as s
except Exception:
    s = None

FRAME_TYPES = ["lights", "bias", "darks", "flats", "dark_flats"]

# -----------------------------
# Data Model
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

    def to_dict(self) -> Dict: return asdict(self)
    @staticmethod
    def from_dict(d: Dict) -> "Session": return Session(**d)

@dataclass
class Project:
    name: str = "Untitled Project"
    project_file: Optional[str] = None
    working_dir: Optional[str] = None

    use_master_library: bool = True
    siril_cli_path: Optional[str] = None

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

    sessions: List[Session] = field(default_factory=list)

    def to_dict(self) -> Dict:
        return {
            "name": self.name,
            "project_file": self.project_file,
            "working_dir": self.working_dir,
            "use_master_library": self.use_master_library,
            "siril_cli_path": self.siril_cli_path,

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
            "sessions": [s.to_dict() for s in self.sessions],
        }

    @staticmethod
    def from_dict(d: Dict) -> "Project":
        p = Project()
        p.name = d.get("name", "Untitled Project")
        p.project_file = d.get("project_file")
        p.working_dir = d.get("working_dir")
        p.use_master_library = d.get("use_master_library", True)
        p.siril_cli_path = d.get("siril_cli_path")

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
        p.sessions = [Session.from_dict(x) for x in d.get("sessions", [])]
        return p

# -----------------------------
# Utilities
# -----------------------------

def safe_slug(name: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", (name or "").strip())
    slug = re.sub(r"_+", "_", slug).strip("_")
    return slug or "project"

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

def get_siril_version(siril_path: str) -> Optional[Tuple[int,int,int]]:
    try:
        out = subprocess.check_output([siril_path, "-v"], text=True, stderr=subprocess.STDOUT, timeout=10)
    except Exception:
        return None
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out)
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None

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
    def log(self, text: str):
        try:
            if self.iface: self.iface.log(text)
        except Exception: pass
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
        # Priority:
        # 1) pp_flat_stacked in this session's process dir
        if produced_flat:
            return " -flat=pp_flat_stacked"
        # 2) Session-level master flat override
        if getattr(sess, "master_flat", None):
            return f" -flat={normalize_path(sess.master_flat)}"  # e.g. replace backslashes with slashes
        # 3) Project-level Siril master library
        if p.use_master_library:
            return " -flat=$defflat"
        # 4) Nothing available → omit
        return ""

    def _stack_cmd(
        self,
        seq_name: str,
        *,
        norm: str = "addscale",
        out: str = "stacked",
        rgb_equal: bool = False,
        output_norm: bool = True,
        nonorm: bool = False,
        use_32b: bool = False,   # <-- add
    ) -> str:
        """Build a Siril 'stack' command using the project's global stacking options.

        - For bias, dark, or darkflat masters:   nonorm=True
        - For flats:                             norm="mul"
        - For lights:                            norm="addscale" (and output_norm=True, rgb_equal=True)
        """
        m = (self.project.stack_method or "rej").lower()
        lo = float(self.project.reject_sigma_low or 3.0)
        hi = float(self.project.reject_sigma_high or 3.0)

        # choose method and sigma formatting
        if m in ("rej", "wrej"):
            method_token, sigma_part = m, f" {lo:g} {hi:g}"
        elif m == "mean":
            method_token, sigma_part = "mean", ""
        else:
            method_token, sigma_part = "rej", f" {lo:g} {hi:g}"

        # normalization options
        opts = []
        if nonorm:
            opts.append("-nonorm")
        else:
            opts.append(f"-norm={norm}")

        # optional flags
        if output_norm:
            opts.append("-output_norm")
        if rgb_equal:
            opts.append("-rgb_equal")
        if use_32b:
            opts.append("-32b")   # <-- add

        return f"stack {seq_name} {method_token}{sigma_part} " + " ".join(opts) + f" -out={out}"

    def build(self) -> str:
        p = self.project
        if not p.working_dir: raise ValueError("Working directory is not set.")
        work = Path(p.working_dir).resolve()

        L: List[str] = []
        L.append("#!Siril script generated by multi-night-stacking.py")
        L.append("requires 1.3.4")
        L.append(f"setcompress {1 if p.compress_intermediates else 0}")
        L.append("setfindstar reset")
        L.append("")
        L.append(f'cd "{work.as_posix()}"')
        L.append("")
        L.append("# Use Master Library: " + ("enabled (configured in Siril preferences)." if p.use_master_library else "disabled (using per-session overrides if provided)."))
        if p.drizzle_enabled:
            L.append(f"# Drizzle: ON (Scaling={p.drizzle_scaling:g}, PixFrac={p.drizzle_pixfrac:g}, Kernel={p.drizzle_kernel})")
        else:
            L.append("# Drizzle: OFF")
        two_pass_txt = "ON" if p.two_pass else "OFF"
        L.append(f"# 2-pass registration: {two_pass_txt}")
        L.append(f"# Global stack method: {p.stack_method} (low={p.reject_sigma_low:g}, high={p.reject_sigma_high:g})")
        L.append("")

        # Build pp_light per session (no registration yet)
        pp_seqs: List[Tuple[str,str]] = []
        any_session = False
        produced_flat = {}   # track sessions that produced pp_flat_stacked

        for sess in p.sessions:
            sess_root   = (work / (sess.work_subdir or sess.name)).resolve()
            bias_dir    = (sess_root / "bias").resolve()
            darks_dir   = (sess_root / "darks").resolve()
            flats_dir   = (sess_root / "flats").resolve()
            lights_dir  = (sess_root / "lights").resolve()
            process_dir = (sess_root / "process").resolve()

            L.append(f"# ---------------- Session: {sess.name} ----------------")

            # --- Decide effective sources (raw frames > per-session master > library) ---

            # Bias (used for FLAT calibration only; not for darks)
            will_build_bias_master = False
            if sess.bias:
                eff_bias = "bias_stacked"; will_build_bias_master = True
            elif sess.master_bias:
                eff_bias = siril_arg(sess.master_bias)
            elif self.project.use_master_library:
                eff_bias = "$defbias"
            else:
                eff_bias = None

            # Dark (used to calibrate LIGHTS only; we DO NOT calibrate darks)
            will_build_dark_master = False
            if sess.darks:
                eff_dark = "dark_stacked"; will_build_dark_master = True
            elif sess.master_dark:
                eff_dark = siril_arg(sess.master_dark)
            elif self.project.use_master_library:
                eff_dark = "$defdark"
            else:
                eff_dark = None

            # Dark-Flat (used to calibrate FLATS; takes precedence over bias for flats)
            will_build_df_master = False
            if getattr(sess, "dark_flats", None) and len(sess.dark_flats) > 0:
                eff_df = "df_stacked"; will_build_df_master = True
            elif getattr(sess, "master_dark_flat", None):
                eff_df = siril_arg(sess.master_dark_flat)
            else:
                eff_df = None

            # --- Build master BIAS (raw biases -> bias_stacked) ---
            if will_build_bias_master:
                L.append(f'cd "{bias_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert bias -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')
                # Bias: use -nonorm, no normalization
                L.append(self._stack_cmd("bias", norm="none", out="bias_stacked",
                                        rgb_equal=False, output_norm=False, nonorm=True))
                L.append("cd .."); L.append("")

            # --- Build master DARK-FLAT (raw darkflats -> df_stacked, no normalization) ---
            if will_build_df_master:
                df_dir = (sess_root / "darkflats").resolve()
                L.append(f'cd "{df_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert darkflat -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')
                L.append(self._stack_cmd("darkflat", norm="none", out="df_stacked",
                                        rgb_equal=False, output_norm=False, nonorm=True))
                L.append("cd .."); L.append("")

            # --- Build master DARK (raw darks -> dark_stacked, no normalization) ---
            if will_build_dark_master:
                L.append(f'cd "{darks_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert dark -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')
                L.append(self._stack_cmd("dark", norm="none", out="dark_stacked",
                                        rgb_equal=False, output_norm=False, nonorm=True))
                L.append("cd .."); L.append("")

            # --- FLATS (optional) ---
            did_make_pp_flat = False
            if sess.flats:
                L.append(f'cd "{flats_dir.as_posix()}"')
                L.append("setext fit")
                L.append("convert flat -out=../process")
                L.append(f'cd "{process_dir.as_posix()}"')

                # Dark-flat takes precedence over bias for flat calibration
                if eff_df:
                    if eff_df == "df_stacked":
                        L.append("calibrate flat -bias=df_stacked")
                    else:
                        L.append(f"calibrate flat -bias={eff_df}")
                elif eff_bias:
                    if eff_bias == "bias_stacked":
                        L.append("calibrate flat -bias=bias_stacked")
                    else:
                        L.append(f"calibrate flat -bias={eff_bias}")
                else:
                    L.append("calibrate flat")

                L.append(self._stack_cmd("pp_flat", norm="mul", out="pp_flat_stacked",
                                        rgb_equal=False, output_norm=False))
                L.append("cd ..")
                did_make_pp_flat = True
                L.append("")
                produced_flat[sess.name] = True

            # --- LIGHTS: convert & calibrate (registration/stack later, globally) ---
            L.append(f'cd "{lights_dir.as_posix()}"')
            L.append("setext fit")
            L.append("convert light -out=../process")
            L.append(f'cd "{process_dir.as_posix()}"')

            # Flat for lights: prefer pp_flat_stacked > per-session master > library
            if did_make_pp_flat:
                flat_part = " -flat=pp_flat_stacked"
            else:
                if sess.master_flat and not sess.flats:   # only if no raw flats present
                    flat_part = f" -flat={siril_arg(sess.master_flat)}"
                elif self.project.use_master_library and not sess.flats:
                    flat_part = " -flat=$defflat"
                else:
                    flat_part = ""

            # Dark for lights: prefer session-built dark_stacked > per-session master > library
            has_dark = False
            if will_build_dark_master:
                dark_part = " -dark=dark_stacked"; has_dark = True
            elif sess.master_dark and not sess.darks:     # only if no raw darks present
                dark_part = f" -dark={siril_arg(sess.master_dark)}"; has_dark = True
            elif self.project.use_master_library and not sess.darks:
                dark_part = " -dark=$defdark"; has_dark = True
            else:
                dark_part = ""

            cc_flag = " -cc=dark" if has_dark else ""

            L.append("# Calibrate Light Frames (OSC)")
            if self.project.drizzle_enabled:
                L.append(f"calibrate light{dark_part}{flat_part}{cc_flag} -cfa")
            else:
                L.append(f"calibrate light{dark_part}{flat_part}{cc_flag} -cfa -equalize_cfa -debayer")
            L.append("")

            pp_seqs.append((process_dir.as_posix(), "pp_light"))
            any_session = True

        # ----- GLOBAL: MERGE → SETFINDSTAR → REGISTER → [SEQAPPLYREG] → STACK -----
        if any_session and pp_seqs:
            L.append("# ---------------- Global Registration & Stacking ----------------")
            base_dir, _ = pp_seqs[0]
            L.append(f'cd "{base_dir}"')

            # Register/Drizzle flags prepared once
            reg_flags    = " -layer=0" + (" -2pass" if p.two_pass else "")
            drizzle_args = f" -drizzle -scale={p.drizzle_scaling:g} -pixfrac={p.drizzle_pixfrac:g} -kernel={p.drizzle_kernel}"

            # Required before register
            L.append("setfindstar")

            # If we will use DRIZZLE anywhere in this block, drizztmp must be uncompressed.
            # Do this ONCE up front for drizzle paths (prevents r_* in .fit.fz).
            if p.compress_intermediates and p.drizzle_enabled:
                L.append("setcompress 0")

            if len(pp_seqs) == 1:
                # ---------------- Single session ----------------
                L.append("# Single session: register pp_light, then stack")
                reg_target = "pp_light"

                # Drizzle fast path (no seqapplyreg) when 2-pass is OFF
                if p.drizzle_enabled and not p.two_pass:
                    # Optional -flat for drizzle weights, based on what this session produced/configured
                    # (pp_flat_stacked, per-session master, or $defflat). Omit otherwise.
                    sess = p.sessions[0]
                    flat_opt = self._drizzle_flat_arg(p, sess, produced_flat.get(sess.name, False))
                    L.append(f"register {reg_target}{reg_flags}{drizzle_args}{flat_opt}")
                    # Final stack (already uncompressed due to the single up-front setcompress 0 for drizzle)
                    L.append(self._stack_cmd(
                        "r_pp_light",
                        norm="addscale", out="final_stacked",
                        rgb_equal=True, output_norm=True,
                        use_32b=p.stack_32bit,
                    ))
                else:
                    # Either drizzle+2pass (needs seqapplyreg), or non-drizzle branch
                    L.append(f"register {reg_target}{reg_flags}")

                    if p.drizzle_enabled:
                        # 2-pass + drizzle → apply transforms to create registered frames
                        L.append(f"seqapplyreg {reg_target}{drizzle_args}")
                        L.append(self._stack_cmd(
                            "r_pp_light",
                            norm="addscale", out="final_stacked",
                            rgb_equal=True, output_norm=True,
                            use_32b=p.stack_32bit,
                        ))
                    else:
                        # Non-drizzle: if 2-pass, we must export registered frames before stacking
                        if p.two_pass:
                            L.append(f"seqapplyreg {reg_target}")
                        # Ensure final_stacked is .fit (not .fit.fz) so the subsequent 'load final_stacked.fit' works
                        if p.compress_intermediates:
                            L.append("setcompress 0")
                        L.append(self._stack_cmd(
                            "r_pp_light",
                            norm="addscale", out="final_stacked",
                            rgb_equal=True, output_norm=True,
                            use_32b=p.stack_32bit,
                        ))
            else:
                # ---------------- Multi-session: MERGE first ----------------
                inputs = " ".join([f'"{folder}/{seq}"' for (folder, seq) in pp_seqs])
                L.append(f"merge {inputs} all_sessions")

                if p.drizzle_enabled and not p.two_pass:
                    # Drizzle fast path on merged sequence (no -flat across sessions)
                    L.append(f"register all_sessions{reg_flags}{drizzle_args}")
                    L.append(self._stack_cmd(
                        "r_all_sessions",
                        norm="addscale", out="final_stacked",
                        rgb_equal=True, output_norm=True,
                        use_32b=p.stack_32bit,
                    ))
                else:
                    # Either drizzle+2pass, or non-drizzle
                    L.append(f"register all_sessions{reg_flags}")
                    if p.drizzle_enabled:
                        # 2-pass + drizzle → apply transforms to create drizzled registered frames
                        L.append(f"seqapplyreg all_sessions{drizzle_args}")
                        L.append(self._stack_cmd(
                            "r_all_sessions",
                            norm="addscale", out="final_stacked",
                            rgb_equal=True, output_norm=True,
                            use_32b=p.stack_32bit,
                        ))
                    else:
                        if p.two_pass:
                            L.append("seqapplyreg all_sessions")
                        # Ensure final_stacked is .fit (not .fit.fz) so the subsequent 'load final_stacked.fit' works
                        if p.compress_intermediates:
                            L.append("setcompress 0")
                        L.append(self._stack_cmd(
                            "r_all_sessions",
                            norm="addscale", out="final_stacked",
                            rgb_equal=True, output_norm=True,
                            use_32b=p.stack_32bit,
                        ))

            # Final save at project working root
            proj_slug = safe_slug(self.project.name)
            L.append('# Copy final image to the project working directory')
            L.append("load final_stacked.fit")
            L.append("mirrorx -bottomup")
            L.append(f'save "../../{proj_slug}_final.fit"')
            L.append(f"# Final output: ../../{proj_slug}_final.fit")
            L.append(f'cd "{base_dir}"')
        else:
            L.append("# No sessions with usable lights were found to stack.")
        
        # Footer resets + close
        L.append("")
        L.append("setfindstar reset")
        L.append("close")
        return "\n".join(L)

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
            lst.model().rowsInserted.connect(self.changed.emit)
            lst.model().rowsRemoved.connect(self.changed.emit)
            lst.itemChanged.connect(self.changed.emit)

    # ---------- Utilities ----------
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

class ProjectWidget(QtWidgets.QWidget):
    status_message = QtCore.pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.project = Project()
        self._dirty = False
        self._suspend_dirty = False
        self._loading_session = False

        # Siril console bridge
        self.siril = SirilConsoleBridge()
        if self.siril.connected:
            self.siril.log("[Multi-Night Stacking] Connected to Siril Python API.")

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
        self.cb_stack_method = QtWidgets.QComboBox()
        self.cb_stack_method.addItems(["Rejection (sigma)", "Winsorized Rejection", "Mean"])
        self._stack_method_map = {0: "rej", 1: "wrej", 2: "mean"}
        self._stack_method_rev = {v: k for k, v in self._stack_method_map.items()}

        self.lbl_sigma_low  = QtWidgets.QLabel("Sigma Low")
        self.dbl_sigma_low  = QtWidgets.QDoubleSpinBox()
        self.dbl_sigma_low.setRange(0.1, 10.0);  self.dbl_sigma_low.setSingleStep(0.1);  self.dbl_sigma_low.setValue(3.0)

        self.lbl_sigma_high = QtWidgets.QLabel("Sigma High")
        self.dbl_sigma_high = QtWidgets.QDoubleSpinBox()
        self.dbl_sigma_high.setRange(0.1, 10.0); self.dbl_sigma_high.setSingleStep(0.1); self.dbl_sigma_high.setValue(3.0)

        # Moved under Stacking (boxed)
        self.cb_stack_32 = QtWidgets.QCheckBox("32-bit Output for Final Stack")
        self.cb_stack_32.setToolTip("Writes the final LIGHTS stack as 32-bit FITS (-32b).")
        self.cb_compress = QtWidgets.QCheckBox("Compress Intermediates (Lossless)")
        self.cb_compress.setToolTip("Use lossless FITS tile compression for intermediates to save disk space.")

        # siril-cli path
        self.ed_siril = QtWidgets.QLineEdit()
        self.btn_siril = QtWidgets.QPushButton("Find…")

        # Sessions list + editor
        self.sessions_list       = QtWidgets.QListWidget()
        self.btn_add_sess        = QtWidgets.QPushButton("Add Session")
        self.btn_remove_sess     = QtWidgets.QPushButton("Remove Session")
        self.btn_dup_sess        = QtWidgets.QPushButton("Duplicate Session")
        self.btn_remove_data_all = QtWidgets.QPushButton("Remove Data (All Sessions)")

        self.session_editor = SessionEditor()  # right pane widget (already draws its own boxed lists)

        # Bottom actions
        self.btn_prepare     = QtWidgets.QPushButton("Prepare Working Directory (Symlink/Copy Files)")
        self.btn_build_script= QtWidgets.QPushButton("Build Siril Script")
        self.btn_run_siril   = QtWidgets.QPushButton("Run Siril (CLI)")
        self.btn_abort       = QtWidgets.QPushButton("Abort Run")
        self.btn_abort.setEnabled(False)

        # ---------------- Left column layout (with boxes) ----------------
        left_form = QtWidgets.QFormLayout()
        left_form.addRow("Project Name", self.ed_name)

        work_row = QtWidgets.QHBoxLayout()
        work_row.addWidget(self.ed_workdir, 1)
        work_row.addWidget(self.btn_workdir)
        left_form.addRow("Working Directory", work_row)
        left_form.addRow("", self.cb_use_library)

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

        # siril-cli path row
        sr = QtWidgets.QHBoxLayout()
        sr.addWidget(self.ed_siril, 1)
        sr.addWidget(self.btn_siril)
        left_form.addRow("siril-cli Path (optional)", sr)

        # add the two boxes to the left form
        left_form.addRow(drizzle_box)
        left_form.addRow(stack_box)

        # sessions list + buttons under left form
        left_col = QtWidgets.QVBoxLayout()
        left_col.addLayout(left_form)
        left_col.addWidget(QtWidgets.QLabel("Sessions"))
        left_col.addWidget(self.sessions_list, 1)

        sbtns = QtWidgets.QHBoxLayout()
        sbtns.addWidget(self.btn_add_sess)
        sbtns.addWidget(self.btn_remove_sess)
        sbtns.addWidget(self.btn_dup_sess)
        sbtns.addStretch(1)
        sbtns.addWidget(self.btn_remove_data_all)
        left_col.addLayout(sbtns)

        # ---------------- Splitter (left/right) ----------------
        splitter = QtWidgets.QSplitter()
        left_widget = QtWidgets.QWidget(); left_widget.setLayout(left_col)
        splitter.addWidget(left_widget)
        splitter.addWidget(self.session_editor)
        splitter.setStretchFactor(0, 1)
        splitter.setStretchFactor(1, 1)

        # ---------------- Bottom bar ----------------
        bottom = QtWidgets.QHBoxLayout()
        bottom.addWidget(self.btn_prepare)
        bottom.addWidget(self.btn_build_script)
        bottom.addStretch(1)
        bottom.addWidget(self.btn_run_siril)
        bottom.addWidget(self.btn_abort)

        # ---------------- Main layout ----------------
        main = QtWidgets.QVBoxLayout(self)
        main.addWidget(splitter)
        main.addLayout(bottom)

        # ---------------- Wiring ----------------
        self.btn_workdir.clicked.connect(self.pick_workdir)
        self.btn_siril.clicked.connect(self.pick_siril)

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

        for w in (self.ed_name, self.ed_workdir, self.ed_siril):
            w.textChanged.connect(self.mark_dirty)
        self.cb_use_library.toggled.connect(self.mark_dirty)

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
        self._toggle_drizzle_opts(self.project.drizzle_enabled)
        self._toggle_sigma_by_method(self.cb_stack_method.currentIndex())

    # ---------------- helpers ----------------
    def _toggle_sigma_by_method(self, idx: int):
        m = self._stack_method_map.get(idx, "rej")
        vis = m in ("rej", "wrej")
        for w in (self.lbl_sigma_low, self.dbl_sigma_low, self.lbl_sigma_high, self.dbl_sigma_high):
            w.setVisible(vis)

    def _toggle_drizzle_opts(self, enabled: bool):
        for w in (self.lbl_scaling, self.spin_scaling, self.lbl_pixfrac, self.spin_pixfrac, self.lbl_kernel, self.cb_kernel):
            w.setEnabled(enabled)

    def mark_dirty(self, *_):
        if self._suspend_dirty or self._loading_session:
            return
        self._dirty = True
        self.status_message.emit("Project has unsaved changes.")

    # ---------------- Model <-> UI ----------------
    def refresh_from_model(self):
        self._suspend_dirty = True
        try:
            p = self.project
            self.ed_name.setText(p.name or "")
            self.ed_workdir.setText(p.working_dir or "")
            self.cb_use_library.setChecked(p.use_master_library)

            self.cb_drizzle.setChecked(p.drizzle_enabled)
            self.spin_scaling.setValue(float(p.drizzle_scaling or 1.0))
            self.spin_pixfrac.setValue(float(p.drizzle_pixfrac or 1.0))
            kernels = ["square", "point", "turbo", "gaussian", "lanczos2", "lanczos3"]
            self.cb_kernel.setCurrentIndex(max(0, kernels.index(p.drizzle_kernel) if p.drizzle_kernel in kernels else 0))
            self.cb_two_pass.setChecked(p.two_pass)

            self.cb_stack_method.setCurrentIndex(self._stack_method_rev.get(p.stack_method, 0))
            self.dbl_sigma_low.setValue(float(p.reject_sigma_low or 3.0))
            self.dbl_sigma_high.setValue(float(p.reject_sigma_high or 3.0))
            self.cb_stack_32.setChecked(bool(getattr(p, "stack_32bit", False)))
            self.cb_compress.setChecked(bool(getattr(p, "compress_intermediates", False)))

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

            # sigma visibility
            self._toggle_sigma_by_method(self.cb_stack_method.currentIndex())
        finally:
            self._suspend_dirty = False

    def push_to_model(self):
        p = self.project
        p.name = self.ed_name.text() or "New Project"
        p.working_dir = self.ed_workdir.text() or None
        p.use_master_library = self.cb_use_library.isChecked()

        p.drizzle_enabled = self.cb_drizzle.isChecked()
        p.drizzle_scaling = float(self.spin_scaling.value())
        p.drizzle_pixfrac = float(self.spin_pixfrac.value())
        p.drizzle_kernel  = self.cb_kernel.currentText()
        p.two_pass        = self.cb_two_pass.isChecked()

        p.stack_method       = self._stack_method_map.get(self.cb_stack_method.currentIndex(), "rej")
        p.reject_sigma_low   = float(self.dbl_sigma_low.value())
        p.reject_sigma_high  = float(self.dbl_sigma_high.value())
        p.stack_32bit        = self.cb_stack_32.isChecked()
        p.compress_intermediates = self.cb_compress.isChecked()

        p.siril_cli_path = self.ed_siril.text() or None

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
        self.mark_dirty()

    def remove_session(self):
        row = self.sessions_list.currentRow()
        if row < 0: return
        sess = self.project.sessions[row]
        work = self.project.working_dir
        sess_root = Path(work) / (sess.work_subdir or sess.name) if work else None
        msg = "Remove this session from the project and delete its on-disk data?\n"
        if sess_root: msg += f"\n{sess_root}"
        if QtWidgets.QMessageBox.question(self, "Remove Session", msg) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        if sess_root and sess_root.exists():
            try: shutil.rmtree(sess_root)
            except Exception as e:
                QtWidgets.QMessageBox.warning(self, "Remove Session", f"Failed to delete:\n{sess_root}\n\n{e}")
        self.project.sessions.pop(row)
        self.sessions_list.takeItem(row)
        if self.project.sessions:
            self.sessions_list.setCurrentRow(0)
            self._loading_session = True
            try: self.session_editor.from_session(self.project.sessions[0])
            finally: self._loading_session = False
        self.mark_dirty()

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

    def remove_all_sessions_data(self):
        self.push_to_model()
        p = self.project
        if not p.working_dir:
            QtWidgets.QMessageBox.warning(self, "Remove Data", "Set a working directory first.")
            return
        work = Path(p.working_dir)
        targets = [work / (s.work_subdir or s.name) for s in p.sessions]
        if QtWidgets.QMessageBox.question(
            self, "Remove Data (All Sessions)",
            "Delete on-disk data for ALL sessions (configs kept)?"
        ) != QtWidgets.QMessageBox.StandardButton.Yes:
            return
        ok=fail=0
        for t in targets:
            if t.exists():
                try: shutil.rmtree(t); ok+=1
                except Exception: fail+=1
            else:
                ok+=1
        QtWidgets.QMessageBox.information(self, "Remove Data", f"Done.\nOK: {ok}\nFailed: {fail}")

    def load_selected_session(self, row: int):
        if 0 <= row < len(self.project.sessions):
            self._loading_session = True
            try: self.session_editor.from_session(self.project.sessions[row])
            finally: self._loading_session = False

    def update_current_session(self):
        if self._loading_session: return
        row = self.sessions_list.currentRow()
        if 0 <= row < len(self.project.sessions):
            self.project.sessions[row] = self.session_editor.to_session()
            self.sessions_list.item(row).setText(self.project.sessions[row].name)
            self.mark_dirty()

    # ---------------- prepare / build / run / abort ----------------
    def prepare_working_dir(self):
        self.push_to_model()
        p = self.project
        if not p.working_dir:
            QtWidgets.QMessageBox.warning(self, "Prepare", "Please set a working directory.")
            return
        work = Path(p.working_dir).resolve()
        entries = []
        for sess in p.sessions:
            for ft in FRAME_TYPES:
                for f in getattr(sess, ft):
                    entries.append((sess, ft, Path(f)))

        if not entries:
            QtWidgets.QMessageBox.information(self, "Prepare", "No files to prepare.")
            return

        from datetime import datetime
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = work / f"prepare_{ts}.log"
        ok=fail=0
        with open(log_path, "w", encoding="utf-8") as lf:
            for i,(sess,ft,src) in enumerate(entries, start=1):
                dst = work/(sess.work_subdir or sess.name)/ft/src.name
                good,msg = safe_link_or_copy(src, dst)
                ok += int(good); fail += int(not good)
                if self.siril.connected: self.siril.log(msg)
                print(msg, file=lf)
                if self.siril.connected:
                    self.siril.progress("Preparing…", i/len(entries))
                    QtWidgets.QApplication.processEvents()
        if self.siril.connected: self.siril.progress_reset()
        QtWidgets.QMessageBox.information(self, "Prepare", f"Done.\nOK: {ok}\nFailed: {fail}\n\nLog: {log_path}")

    def build_script(self):
        # Preserve current dirty state — building an SSF is not a project save
        was_dirty = getattr(self, "_dirty", False)
        try:
            self.push_to_model()  # copy UI → model (must NOT clear _dirty)
            p = self.project
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
            self._dirty = was_dirty

    def run_siril(self):
        from datetime import datetime

        # --- Do NOT let a run clear the dirty flag
        was_dirty = bool(getattr(self, "_dirty", False))

        self.push_to_model()  # this should NOT clear _dirty
        p = self.project
        if not p.working_dir:
            QtWidgets.QMessageBox.warning(self, "Run Siril", "Please set a working directory.")
            return
        script_path = Path(p.working_dir) / "run_project.ssf"
        if not script_path.exists():
            QtWidgets.QMessageBox.warning(self, "Run Siril", "Script not found. Click 'Build Siril Script' first.")
            return

        siril = find_siril_cli(p.siril_cli_path)
        if not siril:
            f, _ = QtWidgets.QFileDialog.getOpenFileName(self, "Locate siril-cli")
            if not f:
                QtWidgets.QMessageBox.critical(self, "Run Siril", "siril-cli not found.")
                return
            siril = f
            # Setting this *does* mark the project dirty, which is expected
            self.ed_siril.setText(f)

        # Version (for log)
        ver = get_siril_version(siril)
        self._run_siril_verstr = f"{ver[0]}.{ver[1]}.{ver[2]}" if ver else "unknown"
        if ver is not None and p.drizzle_enabled and (ver[0], ver[1]) < (1, 4):
            QtWidgets.QMessageBox.warning(
                self, "Siril Version", f"Detected siril-cli {self._run_siril_verstr}. Drizzle requires 1.4+."
            )

        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        log_path = Path(p.working_dir) / f"siril_run_{ts}.log"

        # Launch process (new group for abort on Windows)
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
                bufsize=1,  # line-buffered
                creationflags=creationflags,
            )
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "Run Siril", f"Failed to launch siril-cli: {e}")
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

        # Reader thread
        self._reader = _ProcReader(self._proc, log_path, self.siril, parent=self)

        # Throttle progress updates: only on % change or at most ~8 per second
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
                # (optional) filter console lines)
                #if ("progress:" in ln) or ("log:" in ln) or ("error:" in ln):
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
            # Reset UI state
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

            # Compute elapsed and append to log
            end_dt = datetime.now()
            start_dt = self._run_started_at
            self._run_started_at = None

            def _fmt_elapsed(start_dt, end_dt):
                try:
                    total = int((end_dt - start_dt).total_seconds())
                    h = total // 3600
                    m = (total % 3600) // 60
                    s = total % 60
                    if h:
                        return f"{h}h {m}m {s}s"
                    if m:
                        return f"{m}m {s}s"
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

            # Popup & load final
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
                QtWidgets.QMessageBox.information(self, "Run Siril", f"Processing finished in {elapsed_txt}.\n\nLog saved to:\n{log_path}")
            else:
                QtWidgets.QMessageBox.critical(self, "Run Siril", f"siril-cli exited with code {rc} after {elapsed_txt}.\n\nLog:\n{log_path}")

            self._proc = None
            self._reader = None

            # --- Restore whatever the dirty state was at launch
            # If the user changed things during the run, self._dirty may already be True; keep it.
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
        self.setWindowTitle("OSC Multi-Night Stacking (Siril 1.4) Version 1.0")

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

        help_menu = m.addMenu("&Help")
        about_action = QtGui.QAction("About", self)
        help_menu.addAction(about_action)
        about_action.triggered.connect(lambda: QtWidgets.QMessageBox.information(
            self, "About",
            "OSC Multi-Night Stacking for Siril 1.4 (PyQt6)\n"
            "• Drizzle: Scaling, Pixel Fraction, Kernel\n"
            "• 2-pass registration toggle\n"
            "• Global stacking options (sigma or winorized rejection (sigma high and low), mean)\n"
            "• 32-bit output for final stack and intermediate file compression toggles\n"
            "• Siril console logging via sirilpy\n"
            "• Abort Run button (graceful stop)\n"
            "• Final stack copied, mirrored, and opened in Siril"
        ))

        self.resize(1100, 780)

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
            self.status.showMessage(f"Opened project: {os.path.basename(path)}", 4000)

def main():
    try:
        QtGui.QGuiApplication.setHighDpiScaleFactorRoundingPolicy(QtCore.Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    except Exception: pass
    app=QtWidgets.QApplication(sys.argv); w=MainWindow(); w.show(); sys.exit(app.exec())

if __name__=="__main__": main()

