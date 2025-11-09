# OSC Multi-Night Stacking — Siril 1.4 Python Application Script
**Version 1.1**

### Overview
**OSC Multi-Night Stacking** is a **Python + PyQt6 GUI** Siril application script that automates the creation of Siril 1.4 scripts for multi-night deep-sky stacking projects using Siril-style directory layouts.  
It provides a Sirilic-like interface using the new Siril Python API that is fully portable, written in Python, and designed for users who capture multiple nights of imaging data and want a one-click way to generate correct Siril processing pipelines.

**Note: Siril 1.4 Beta 4 or later is required **
---
## ✨ New Features (v1.1)

New enhancements:
- Now compresses final register and stack fit files instead of just the intermediate session fit files.
    * **Limitation: register and setapplyreg commands will not compress fit files if drizzling is enabled due to a limitation with Siril 1.4 where the drizzling functionality
      does not support compressed fit files.**
- Added pack Sequences feature - this allows you to pack the fit or fz files into a SER or FITSEQ sequence file.  This allows us to overcome the 2048 open file limit on Windows.
    * Off by default
    * Auto, FITSEQ, and SER are the options that can be used.
    * Pack Sequences only will pack LIGHT frames. LIGHT frame pack threshold is configurable and can be lowered and raised as needed.
    * Auto uses the FITSEQ sequence file format (recommended for maximum compatibility) where is puts the fit or fz files into into a single file for stacking. 

## ✨ Features (v1.0)

### Core Capabilities
- **Multi-Night Project Handling**  
  * Supports OSC images.
  * Supports any number of nights of imaging under a single project root.
  * Ability to Create, Save, and Load Project json files.
  * Automatically merges registered and stacked data across nights.  
  * Generates *night-specific* and *global* Siril script for calibration, registration, stacking, and post-processing.
  * Final stack copied, mirrored, and opened in Siril

- **Automatic Directory Detection**
  * Uses Siril style directory structure.  
  * Detects camera name, gain, offset, binning, and exposure length from FITS headers for file naming.  

- **Script Generator for Siril 1.4**  
  * Produces fully commented `.ssf` scripts compatible with Siril 1.4 Beta 3/4.  
  * Handles master bias/dark/flat creation automatically.  
  * Adds **mirrorx -bottomup** at the final stage to correct FITS orientation.
  * Includes optional **setcompress 0** for lossless FITS saving.
  * Drizzle: Scaling, Pixel Fraction, Kernel
  * 2-pass registration toggle
  * Global stacking options (sigma or winorized rejection (sigma high and low), mean)
  * 32-bit output for final stack

- **UI Highlights**
  - Qt-based tabbed interface for **Project**, **Nights**, and **Processing Settings**.  
  - Live detection of unsaved project changes (with save prompt).  
  - Configurable Siril executable path and project output root.
  - Abort Run button (graceful stop)

- **Validation & Logging**
  - Generates project-specific log files.
  - Siril console logging via sirilpy
  - Performs validation on missing calibration frames b=and bad paths.

---

## 🧭 Typical Directory Layout

ProjectRoot/

    ├─ Session 1/
    
    │ ├─ process/
    
    │ ├─ LIGHTS/
    
    │ ├─ DARKS/
    
    │ ├─ FLATS/
    
    │ └─ BIASES/
    
    ├─ Session 2/
    
    │ 

## ⚡ Quick-Start — Running from Siril 1.4

> Use this 7-step guide to run the Multi-Night Stacking Python Application and process all of your sessions in Siril 1.4.

### 🧰 Requirements
- **Siril 1.4 Beta 4** (with Python scripting enabled)  
- **Python 3.10 +** with `PyQt6`, `astropy`, and `sirilpy` installed   

---

### 🪄 Steps

1. **Downlaod osc-multi-night-stacking.py**
   - Place the script in your Siril Scripts directory.
   - Refer to the [Siril 1.4 doc](https://siril.readthedocs.io/en/latest/preferences/preferences_gui.html#scripts) on how to set a Siril Scripts directoey. 
   
2. **Open Siril 1.4**  
   - Start Siril normally.
   - Ensure that the script is visiable in the UI. (Scripts->Python Scripts)
   - Refer to the [Siril 1.4 Script doc](https://siril.readthedocs.io/en/latest/preferences/preferences_gui.html#scripts) if it is not.
     
3. **Launch the Application Script from the Scripts -> Python Scripts menu in Siril**
   - The OSC Multi-Night Stacking script GUI will open alongside Siril.   

4. **Create or Load a Project**
   - Click **New Project** → choose your target root folder
   - Check or uncheck features and options
   - Add Sessions as needed
   - Add LIGHTS, FLATS, DARKS, BIAS frames to Sessions or Panels as needed
   - The app auto-detects all session folders (Session 1, Session 2, …) if they were previously created.  
   - Check or uncheck features and options

5. **Prepare Working Directory (Symlink?copy Files)**
   - Inside each `Session X` folder.  
   - The application writes its `.ssf` script files into this folder and Siril uses it as the *working directory*.  
   - **Why:** Siril reads and writes intermediate calibrated and registered FITS frames inside this directory.  
   - Each session will therefore have:
     ```
     Session 1/process/
     Session 2/process/
     ...
     ```
6. **Generate Siril Script**  
   - Confirm features and option settings.
   - Click **Build Siril Script**
   - The application writes its `.ssf` script file into the *working directory*   

7. *Run Siril Script**

- Click the **Run Siril Script** button in the main window.  
- The application automatically executes the configured **`run_project.ssf`** Siril script via the Siril Python API and use the **`siril-cli`** as a backup
- Progress and script output appear in the console log panel in Siril and in a log file in the *working directory*
- When finished, your combined stack is saved automatically as **`[ProjectName]_final.fit`** and loaded into Siril
---

### 🕒 Typical Workflow Timeline
| Step | Action | Time |
|------|---------|------|
| ① | Launch Siril & Python app | 1 min |
| ② | Create or Load project & verify sessions | 2 min |
| ② | Configure Settings and Images | 5 min |
| ③ | Generate scripts | < 1 min |
| ④ | Run Siril script | Typicallly 5-??? minutes per session depending on the # of subs |

---

`This program is free software: you can redistribute it and/or modify it under the terms of the GNU General Public License as published by the Free Software Foundation, either version 3 of the License, 
or (at your option) any later version. This program is distributed in the hope that it will be useful, but WITHOUT ANY WARRANTY; without even the implied warranty of MERCHANTABILITY or FITNESS FOR A
PARTICULAR PURPOSE. See the GNU General Public License for more details.
See <https://www.gnu.org/licenses/>.`
