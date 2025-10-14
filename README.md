# Multi-Night Stacking — Siril Python Application  
**Version 1.0**

### Overview
**Multi-Night Stacking** is a standalone **Python + PyQt6 GUI** application that automates the creation of Siril 1.4 scripts for multi-night deep-sky stacking projects using Siril-style directory layouts.  
It provides a Sirilic-like interface but is fully portable, written in Python, and designed for users who capture multiple nights of imaging data and want a one-click way to generate correct Siril processing pipelines.

---

## ✨ Features (v1.0)

### Core Capabilities
- **Multi-Night Project Handling**  
  * Supports any number of nights of imaging under a single project root.  
  * Automatically merges registered and stacked data across nights (2048 file limit on Windows).  
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

> Use this 5-step guide to run the Multi-Night Stacking Python Application and process all of your sessions in Siril 1.4.

### 🧰 Requirements
- **Siril 1.4 Beta 3 or 4** (with Python scripting enabled)  
- **Python 3.10 +** with `PyQt6`, `astropy`, and `sirilpy` installed  
- N.I.N.A.-style directory layout (Session 1, Session 2, etc.)  

---

### 🪄 Steps

1. **Open Siril 1.4**  
   - Start Siril normally.  
   - Ensure Python scripting is enabled (`Preferences → Python`).  

2. **Launch the Application**
   - In Siril’s command window or a system terminal, run:
     ```bash
     python multi-night-stacking.py
     ```
   - The PyQt6 GUI will open alongside Siril.  
   - (You can also launch the app directly from Windows Explorer if file associations are set.)

3. **Create or Load a Project**
   - Click **New Project** → choose your target root folder.  
   - The app auto-detects all session folders (Session 1, Session 2, …) if they were previously created.  
   - Confirm settings. 

4. **Prepare Working Directory (Symlink?copy Files)**
   - Inside each `Session X` folder, create a sub-folder called **`process`** (if not already there).  
   - The application writes its `.ssf` script files into this folder and Siril uses it as the *working directory*.  
   - **Why:** Siril reads and writes intermediate calibrated and registered FITS frames inside this directory.  
   - Each session will therefore have:
     ```
     Session_1/process/
     Session_2/process/
     ...
     ```
5. **Generate Siril Script**
   - In the GUI, confirm the **Siril CLI path**  
     *(e.g. `C:\Program Files\Siril\bin\siril-cli.exe`)*  
   - Check or uncheck options.
   - Click **Generate Siril Scripts** — this creates:

6. *Run Siril (CLI)**

- Click the **Run Siril (CLI)** button in the main window.  
- The application automatically launches the configured **`siril-cli`** executable and executes **`run_project.ssf`** Siril script.
- Progress and Siril output appear in the log panel or terminal window.  
- When finished, your combined stack is saved automatically as **`[ProjectName]_final.fit`**

---

### 🕒 Typical Workflow Timeline
| Step | Action | Time |
|------|---------|------|
| ① | Launch Siril & Python app | 1 min |
| ② | Create or Load project & verify sessions | 2 min |
| ② | Configure Settings and Images | 5 min |
| ③ | Generate scripts | < 1 min |
| ④ | Run Siril script | Typicallly 5-? minutes per session depending on the # of subs |

---


