"""Opt-in native API validation, launched INSIDE Siril:

pyscript "tests/integration_storage_api.py" "small-project.json" "new-output-dir"

Use a small OSC mosaic fixture. Processing uses independent output directories;
source FITS and the supplied project remain untouched. Read report.json even if
Siril exits successfully: Siril 1.4.4 does not propagate Python exit status.
"""
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.dont_write_bytecode = True


def main():
    import sirilpy
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("storage_api_validation", root / "osc-multi-night-with-mosiac-extract-HaOIII-stacking-v3.0.1.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    project_data = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8-sig"))
    base = Path(sys.argv[2]).absolute()
    base.mkdir(parents=True, exist_ok=False)
    app = module.QtCore.QCoreApplication.instance() or module.QtCore.QCoreApplication([])
    iface = sirilpy.SirilInterface()
    iface.connect()
    results = {}
    try:
        for case in ("success", "early_cancel", "cancel", "failure"):
            project = module.Project.from_dict(project_data)
            project.storage_policy = "min_disk"
            project.working_dir = str(base / case)
            builder = module.SirilCommandBuilder(project)
            builder.build()
            bundle = builder.storage_bundle
            bundle.write()
            manifest = bundle.manifest_path

            class FailingInterface:
                def __getattr__(self, name):
                    return getattr(iface, name)

                def cmd(self, line):
                    if line.startswith("register "):
                        iface.cmd("register storage_sequence_that_does_not_exist")
                    else:
                        iface.cmd(line)

            job = module._StorageThread(manifest, FailingInterface() if case == "failure" else iface)
            if case == "early_cancel":
                (manifest.parent / "cancel.request").write_text(str(time.time_ns()), encoding="utf-8")
            if case == "cancel":
                def request_stop(message):
                    if message.startswith("STAGE ") and message.endswith("night registration"):
                        (manifest.parent / "cancel.request").write_text(str(time.time_ns()), encoding="utf-8")
                job.status.connect(request_stop)
            job.finished.connect(app.quit)
            stamp = time.time_ns()
            job.start()
            app.exec()
            job.wait()
            state = json.loads((manifest.parent / "state.json").read_text())
            expected = {"success": "complete", "early_cancel": "cancelled", "cancel": "cancelled", "failure": "failed"}[case]
            assert state["status"] == expected, (case, state.get("error"))
            retained = list(bundle.root.rglob("pp_light_*.fit")) + list(bundle.root.rglob("bkg_pp_light_*.fit"))
            if case == "success":
                module.check_storage_completion(manifest, stamp)
                iface.cmd("load " + module.LowDiskMosaicPlan.quote(bundle.plan["final"]))
            elif case != "early_cancel":
                assert retained, "The failed consumer lost its inputs"
                assert not list((manifest.parent / "receipts").glob("*/completed.fit"))
            results[case] = {"status": state["status"], "error": job.error, "prerequisite_frames_retained": len(retained)}
            if case in ("early_cancel", "cancel"):
                resumed = module._StorageThread(manifest, iface)
                resumed.finished.connect(app.quit)
                resumed.start()
                app.exec()
                resumed.wait()
                assert resumed.error is None, resumed.error
                results["resume_after_" + case] = {"status": json.loads((manifest.parent / "state.json").read_text())["status"]}
        results["passed"] = True
    except BaseException as exc:
        results["passed"] = False
        results["error"] = str(exc)
        raise
    finally:
        (base / "report.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        iface.disconnect()


if __name__ == "__main__":
    main()
