# Siril 1.4 Beta 4 Python script
# Automatically builds master dark frames for a NINA-style folder layout.
# Uses current Siril working directory as ROOT and reads CAMERA/BINNING from FITS headers.

# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, 
# or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
# See <https://www.gnu.org/licenses/>.

import os
import re
import shutil
from collections import defaultdict, Counter
import sirilpy as s
from astropy.io import fits

# ---------------- Siril setup ----------------
siril = s.SirilInterface()
siril.connect()

# Remember the starting directories so we can restore them later
START_DIR = os.getcwd()                # Python CWD (same as Siril’s working dir at launch)
ROOT = os.getcwd()                     # use Python CWD (matches Siril working dir)
OUT  = os.path.join(ROOT, "MasterDarks")
os.makedirs(OUT, exist_ok=True)

# ---------------- STACKING CONTROL ----------------
#   "med" -> median
#   "rej" -> sigma-clipped mean (requires low/high sigma args)
STACK_METHOD     = "med"
STACK_SIGMA_LOW  = 3.0       # used only when STACK_METHOD == "rej"
STACK_SIGMA_HIGH = 3.0
STACK_NORM       = "-nonorm" # no normalization for dark masters
# --------------------------------------------------

GROUP_BY_TEMP = True
TEMP_ROUND = 0

# Bin tag style: "1" -> Bin1, "1x1" -> Bin1x1
BIN_TAG_STYLE = "1"   # set to "1x1" if you want Bin1x1

FNAME_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}_(?P<gain>\d+)g_(?P<temp>[-+]?\d+\.?\d*)c_(?P<offset>\d+)_"
    r"(?P<exp>\d+\.?\d*)s_\d+\.(?:fit|fits)$", re.IGNORECASE
)

def read_cam_bin_from_fits(fpath):
    """Read camera name and binning info from FITS header, with flexible bin tag style."""
    with fits.open(fpath, memmap=True) as hdul:
        hdr = hdul[0].header
    cam = str(hdr.get("INSTRUME", hdr.get("CCDNAME", "UnknownCamera"))).strip() or "UnknownCamera"
    cam = cam.replace(" ", "_")  # normalize for filenames

    xb = hdr.get("XBINNING", hdr.get("BINNING", 1))
    yb = hdr.get("YBINNING", xb)
    try:
        xb = int(xb)
    except Exception:
        pass
    try:
        yb = int(yb)
    except Exception:
        pass

    if BIN_TAG_STYLE == "1":
        bin_tag = f"Bin{xb}" if xb == yb else f"Bin{xb}x{yb}"
    else:
        bin_tag = f"Bin{xb}x{yb}"

    return cam, bin_tag

def fmt_seconds(sec_str):
    v = float(sec_str)
    if abs(v - round(v)) < 1e-6:
        return f"{int(round(v))}s"
    s = f"{v:.2f}".rstrip("0").rstrip(".")
    return f"{s}s"

def fmt_temp_c(temp_str):
    v = round(float(temp_str), TEMP_ROUND)
    if TEMP_ROUND == 0:
        v = int(v)
    return f"T{v}C"

def find_dark_groups(root):
    r"""
    Walk ROOT and find DARKS directories with structure:
      ...\DARKS\<EXPOSURE>s\<GAIN>g\<OFFSET>\DARKS\*.fits
    Returns dict keyed by (exp_str, gain, offset, temp_tag) -> list of filepaths
    """
    groups = defaultdict(list)
    for dirpath, dirs, _ in os.walk(root):
        for d in dirs:
            if d.upper() != "DARKS":
                continue
            dark_leaf = os.path.join(dirpath, d)
            parts = os.path.normpath(dark_leaf).split(os.sep)
            if len(parts) < 5:
                continue
            try:
                offset = int(parts[-2])
                gain = int(parts[-3].rstrip("gG"))
                exp_str = parts[-4].rstrip("sS")
            except Exception:
                continue

            files = [os.path.join(dark_leaf, f) for f in os.listdir(dark_leaf)
                     if f.lower().endswith((".fit", ".fits"))]
            if not files:
                continue

            temps = []
            file_buckets = defaultdict(list)
            for fp in files:
                fn = os.path.basename(fp)
                m = FNAME_RE.match(fn)
                if not m:
                    continue
                temps.append(m.group("temp"))
                temp_tag = fmt_temp_c(m.group("temp"))
                if GROUP_BY_TEMP:
                    file_buckets[temp_tag].append(fp)

            if GROUP_BY_TEMP:
                for ttag, fps in file_buckets.items():
                    if fps:
                        groups[(fmt_seconds(exp_str), gain, offset, ttag)].extend(sorted(fps))
            else:
                mode_temp = Counter(temps).most_common(1)[0][0] if temps else "0"
                temp_tag = fmt_temp_c(mode_temp)
                groups[(fmt_seconds(exp_str), gain, offset, temp_tag)].extend(sorted(files))

    return groups

def build_master(exp_str, gain, offset, temp_tag, frames):
    cam, binning = read_cam_bin_from_fits(frames[0])
    tag_core = f"{exp_str}_G{gain}_O{offset}_{temp_tag}_{binning}"
    work = os.path.join(OUT, f"_work_{tag_core}")
    out_master = os.path.join(OUT, f"{cam}_DARK_{tag_core}.fit")

    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    # Stage frames (symlink preferred; fallback copy)
    for i, src in enumerate(sorted(frames), start=1):
        dst = os.path.join(work, f"dark_{i:05d}.fits")
        try:
            os.symlink(src, dst)
        except (OSError, AttributeError):
            shutil.copy2(src, dst)

    print(f"\n=== Building master dark: {tag_core} | {len(frames)} frames ===")
    try:
        siril.cmd("cd", work)
        siril.cmd("convert", "dark")

        outname = f"masterdark_{tag_core}"
        if STACK_METHOD == "rej":
            # pass low/high sigma as separate args
            siril.cmd(
                "stack", "dark", "rej",
                str(STACK_SIGMA_LOW), str(STACK_SIGMA_HIGH),
                STACK_NORM,
                f"-out={outname}"
            )
        else:
            siril.cmd(
                "stack", "dark", "med",
                STACK_NORM,
                f"-out={outname}"
            )

        src_master = os.path.join(work, f"{outname}.fit")
        if os.path.isfile(src_master):
            shutil.move(src_master, out_master)
            print(f"[✓] {out_master}")
        else:
            siril.error_messagebox(f"Master dark missing for {tag_core}")
    finally:
        # Always restore Siril’s working dir and clean up temp folder
        try:
            siril.cmd("cd", START_DIR)
        finally:
            shutil.rmtree(work, ignore_errors=True)

# ---------------- MAIN ----------------
groups = find_dark_groups(ROOT)
if not groups:
    siril.error_messagebox("No DARKS found. Make sure your working directory is the correct parent folder.")
    raise SystemExit

for (exp_str, gain, offset, temp_tag), files in sorted(groups.items()):
    build_master(exp_str, gain, offset, temp_tag, files)

print("\nAll master darks created successfully.")

# Final safety: restore working dir & remove any stray _work_ dirs
try:
    siril.cmd("cd", START_DIR)
finally:
    for d in os.listdir(OUT):
        if d.startswith("_work_"):
            shutil.rmtree(os.path.join(OUT, d), ignore_errors=True)

