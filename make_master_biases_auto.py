# Siril 1.4 Beta 4 Python script
# Builds master bias frames for a NINA-style folder layout.
# ROOT comes from Python's current working directory; camera/binning read from FITS headers.

# This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, 
# or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
# PARTICULAR PURPOSE. See the GNU General Public License for more details.
# See <https://www.gnu.org/licenses/>.

import os
import shutil
import sirilpy as s
from astropy.io import fits

siril = s.SirilInterface()
siril.connect()

# Remember the starting directories so we can restore them later
START_DIR = os.getcwd()  # Python CWD (same as Siril’s working dir at launch)

# ---------------- SETTINGS ----------------
ROOT = os.getcwd()                                   # use Python CWD (matches Siril working dir)
OUT  = os.path.join(ROOT, "MasterBias")

STACK_METHOD      = "med"    # "med" or "rej"
STACK_SIGMA_LOW   = 3.0      # only used when STACK_METHOD == "rej"
STACK_SIGMA_HIGH  = 3.0
STACK_NORM        = "-nonorm"  # no normalization for bias

# Bin tag style: "1" -> Bin1, "1x1" -> Bin1x1
BIN_TAG_STYLE = "1"   # set to "1x1" if you want Bin1x1
# -------------------------------------------

os.makedirs(OUT, exist_ok=True)

def read_cam_bin_from_fits(fpath):
    with fits.open(fpath, memmap=True) as hdul:
        hdr = hdul[0].header
    cam = str(hdr.get("INSTRUME", hdr.get("CCDNAME", "UnknownCamera"))).strip() or "UnknownCamera"
    # Try to read explicit X/Y binning, fall back to single BINNING if present
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

    # Normalize camera token for filenames
    cam = cam.replace(" ", "_")

    # Format the bin tag the way you want
    if BIN_TAG_STYLE == "1":
        # If symmetric binning, show single number (Bin1, Bin2). If asymmetric, show BinXxY.
        bin_tag = f"Bin{xb}" if xb == yb else f"Bin{xb}x{yb}"
    else:
        bin_tag = f"Bin{xb}x{yb}"

    return cam, bin_tag

def find_bias_dirs(root):
    """Yield tuples (gain, offset, bias_dir_path)."""
    for dirpath, dirs, _ in os.walk(root):
        for d in dirs:
            if d.upper() == "BIASES":
                bias_dir = os.path.join(dirpath, d)
                parts = os.path.normpath(bias_dir).split(os.sep)
                if len(parts) < 3:
                    continue
                try:
                    offset = int(parts[-2])
                    gain = int(parts[-3].rstrip("gG"))
                except Exception:
                    continue
                yield gain, offset, bias_dir

def make_master(gain, offset, src_dir):
    files = [os.path.join(src_dir, f) for f in sorted(os.listdir(src_dir))
             if f.lower().endswith((".fit", ".fits"))]
    if not files:
        return

    cam, binning = read_cam_bin_from_fits(files[0])
    tag = f"G{gain}_O{offset}_{binning}"
    work = os.path.join(OUT, f"_work_{tag}")
    out_master = os.path.join(OUT, f"{cam}_BIAS_G{gain}_O{offset}_{binning}.fit")

    # fresh staging
    if os.path.isdir(work):
        shutil.rmtree(work)
    os.makedirs(work, exist_ok=True)

    # Stage frames (symlink or copy)
    for i, src in enumerate(files, start=1):
        dst = os.path.join(work, f"bias_{i:05d}.fits")
        try:
            os.symlink(src, dst)
        except (OSError, AttributeError):
            shutil.copy2(src, dst)

    print(f"\n=== Building master bias: Gain={gain}, Offset={offset} ===")

    try:
        # Switch Siril working dir for processing
        siril.cmd("cd", work)
        siril.cmd("convert", "bias")

        outname = f"masterbias_G{gain}_O{offset}"
        if STACK_METHOD == "rej":
            # pass low/high sigma as separate args
            siril.cmd(
                "stack", "bias", "rej",
                str(STACK_SIGMA_LOW), str(STACK_SIGMA_HIGH),
                STACK_NORM,
                f"-out={outname}"
            )
        else:
            siril.cmd(
                "stack", "bias", "med",
                STACK_NORM,
                f"-out={outname}"
            )

        src_master = os.path.join(work, f"{outname}.fit")
        if os.path.isfile(src_master):
            shutil.move(src_master, out_master)
            print(f"[✓] {out_master}")
        else:
            siril.error_messagebox(f"Master bias missing for G{gain} O{offset}")
    finally:
        # Always restore Siril’s working dir and clean up temp folder
        try:
            siril.cmd("cd", START_DIR)
        finally:
            shutil.rmtree(work, ignore_errors=True)

bias_dirs = list(find_bias_dirs(ROOT))
if not bias_dirs:
    siril.error_messagebox("No BIASES folders found. Make sure your Siril working directory is the parent folder.")
    raise SystemExit

for g, o, path in bias_dirs:
    make_master(g, o, path)

print("\nAll master biases created successfully.")

# Final safety: restore working dir & remove any stray _work_ dirs
try:
    siril.cmd("cd", START_DIR)
finally:
    for d in os.listdir(OUT):
        if d.startswith("_work_"):
            shutil.rmtree(os.path.join(OUT, d), ignore_errors=True)

