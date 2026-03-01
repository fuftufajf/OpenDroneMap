"""
ODM Simple GUI — Flask web interface for OpenDroneMap
Run: python gui/app.py
Then open: http://localhost:5050
"""

import os
import sys
import json
import uuid
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from flask import (Flask, request, jsonify, render_template,
                   Response, send_from_directory, stream_with_context)

# ─── Paths ──────────────────────────────────────────────────────────────────
GUI_DIR     = Path(__file__).parent.resolve()
ODM_DIR     = GUI_DIR.parent                       # OpenDroneMap root
PROJECTS    = GUI_DIR / "projects"                 # all task folders live here
PROJECTS.mkdir(exist_ok=True)

# ─── Runner mode ─────────────────────────────────────────────────────────────
# ODM doesn't run natively on Windows — use Docker instead.
# Set USE_DOCKER=False and PYTHON to your ODM-capable Python if on Linux.
USE_DOCKER   = True
ODM_IMAGE    = "opendronemap/odm:latest"
PYTHON       = sys.executable          # only used when USE_DOCKER=False

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 4 * 1024 * 1024 * 1024  # 4 GB

# ─── In-memory state ────────────────────────────────────────────────────────
# task_id → { status, log_lines, process, created }
tasks: dict = {}
tasks_lock = threading.Lock()

# ─── Processing presets ─────────────────────────────────────────────────────
PRESETS = {
    "fast_ortho": {
        "label": "Szybka Ortofotomapa",
        "description": "Pomija gęstą rekonstrukcję 3D — szybko daje orthophoto ze sparse clouda. Dobre na duże obszary lub pierwsze testy.",
        "icon": "⚡",
        "args": ["--fast-orthophoto", "--feature-quality", "medium",
                 "--orthophoto-resolution", "5", "--skip-report"],
    },
    "hq_ortho": {
        "label": "Ortofotomapa HD",
        "description": "Pełna rekonstrukcja z wysoką jakością. Dobry balans między jakością a czasem.",
        "icon": "🗺️",
        "args": ["--feature-quality", "high", "--orthophoto-resolution", "3",
                 "--build-overviews", "--orthophoto-compression", "DEFLATE"],
    },
    "ortho_dem": {
        "label": "Ortofotomapa + DSM/DTM",
        "description": "Ortofotomapa + Numeryczny Model Terenu i Powierzchni (DSM/DTM). Dobre do analiz wysokościowych.",
        "icon": "🏔️",
        "args": ["--feature-quality", "high", "--orthophoto-resolution", "3",
                 "--dsm", "--dtm", "--dem-resolution", "5",
                 "--build-overviews"],
    },
    "model_3d": {
        "label": "Model 3D",
        "description": "Generuje teksturowany model 3D (OBJ/GLB). Bez ortofotomapy. Dobry do wizualizacji.",
        "icon": "🧊",
        "args": ["--feature-quality", "high", "--use-3dmesh",
                 "--skip-orthophoto", "--gltf", "--mesh-octree-depth", "11"],
    },
    "pointcloud": {
        "label": "Chmura punktów (LAS)",
        "description": "Gęsta chmura punktów w formacie LAS — do analiz w QGIS / CloudCompare. Bez modelu 3D.",
        "icon": "☁️",
        "args": ["--pc-quality", "high", "--pc-las", "--pc-classify",
                 "--skip-orthophoto", "--skip-3dmodel", "--feature-quality", "high"],
    },
    "full": {
        "label": "Pełne przetwarzanie",
        "description": "Ortofotomapa + chmura punktów LAS + DSM/DTM + model 3D. Najdłuższy czas, kompletne wyniki.",
        "icon": "🚀",
        "args": ["--feature-quality", "high", "--orthophoto-resolution", "3",
                 "--dsm", "--dtm", "--dem-resolution", "5",
                 "--pc-las", "--pc-quality", "high",
                 "--build-overviews", "--gltf"],
    },
}

# ─── Helper: output file discovery ──────────────────────────────────────────
OUTPUT_GLOBS = [
    ("odm_orthophoto", "odm_orthophoto.tif"),
    ("odm_orthophoto", "odm_orthophoto.png"),
    ("odm_orthophoto", "odm_orthophoto.kmz"),
    ("odm_dem",        "dsm.tif"),
    ("odm_dem",        "dtm.tif"),
    ("odm_texturing",  "odm_textured_model_geo.obj"),
    ("odm_texturing",  "odm_textured_model_geo.glb"),
    ("odm_georeferencing", "odm_georeferenced_model.las"),
    ("odm_georeferencing", "odm_georeferenced_model.laz"),
    ("odm_report",     "report.pdf"),
]

def collect_outputs(task_dir: Path) -> list:
    results = []
    for subdir, filename in OUTPUT_GLOBS:
        p = task_dir / subdir / filename
        if p.exists():
            size_mb = p.stat().st_size / (1024 * 1024)
            results.append({"subdir": subdir, "filename": filename,
                             "size_mb": round(size_mb, 1)})
    return results


# ─── Routes ─────────────────────────────────────────────────────────────────

@app.route("/api/docker_status")
def api_docker_status():
    """Check if Docker daemon is running and the ODM image is available."""
    if not USE_DOCKER:
        return jsonify({"mode": "native", "ready": True})
    try:
        # Quick ping — 'docker version' exits 0 if daemon is up
        r = subprocess.run(["docker", "version", "--format", "{{.Server.Version}}"],
                           capture_output=True, text=True, timeout=5)
        if r.returncode != 0:
            return jsonify({"mode": "docker", "ready": False,
                            "error": "Docker daemon not running. Start Docker Desktop."})
        docker_version = r.stdout.strip()

        # Check if ODM image is already pulled
        img = subprocess.run(["docker", "image", "inspect", ODM_IMAGE, "--format", "{{.Id}}"],
                             capture_output=True, text=True, timeout=5)
        image_pulled = img.returncode == 0

        return jsonify({"mode": "docker", "ready": True,
                        "docker_version": docker_version,
                        "image": ODM_IMAGE,
                        "image_pulled": image_pulled})
    except FileNotFoundError:
        return jsonify({"mode": "docker", "ready": False,
                        "error": "Docker not installed. Install Docker Desktop from https://docker.com/products/docker-desktop"})
    except Exception as e:
        return jsonify({"mode": "docker", "ready": False, "error": str(e)})


@app.route("/api/pull_image", methods=["POST"])
def api_pull_image():
    """Pull the ODM Docker image in the background and stream progress via SSE."""
    if not USE_DOCKER:
        return jsonify({"error": "Not in Docker mode"}), 400

    task_id = "__pull__"
    with tasks_lock:
        if tasks.get(task_id, {}).get("status") == "running":
            return jsonify({"already": True})
        tasks[task_id] = {"status": "running", "log_lines": [f"[GUI] Pulling {ODM_IMAGE}…"],
                          "process": None, "created": datetime.now().isoformat()}

    def do_pull():
        try:
            proc = subprocess.Popen(["docker", "pull", ODM_IMAGE],
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True, bufsize=1)
            with tasks_lock:
                tasks[task_id]["process"] = proc
            for line in proc.stdout:
                with tasks_lock:
                    tasks[task_id]["log_lines"].append(line.rstrip())
            proc.wait()
            status = "done" if proc.returncode == 0 else "error"
            with tasks_lock:
                tasks[task_id]["status"] = status
                tasks[task_id]["log_lines"].append(
                    "[GUI] ✅ Gotowe!" if status == "done" else f"[GUI] ❌ Błąd (kod {proc.returncode})")
        except Exception as e:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["log_lines"].append(f"[GUI] BŁĄD: {e}")

    threading.Thread(target=do_pull, daemon=True).start()
    return jsonify({"pulling": True})


@app.route("/")
def index():
    return render_template("index.html", presets=PRESETS)


@app.route("/api/presets")
def api_presets():
    return jsonify(PRESETS)


@app.route("/api/upload", methods=["POST"])
def api_upload():
    """Upload images — creates task folder, saves files, returns task_id."""
    files = request.files.getlist("images")
    if not files:
        return jsonify({"error": "Brak plików"}), 400

    task_id = f"task_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    images_dir = PROJECTS / task_id / "images"
    images_dir.mkdir(parents=True, exist_ok=True)

    saved = []
    for f in files:
        if not f.filename:
            continue
        dest = images_dir / Path(f.filename).name
        f.save(str(dest))
        saved.append(dest.name)

    if not saved:
        shutil.rmtree(PROJECTS / task_id, ignore_errors=True)
        return jsonify({"error": "Nie zapisano żadnych plików"}), 400

    with tasks_lock:
        tasks[task_id] = {
            "status": "uploaded",
            "log_lines": [f"[GUI] Zapisano {len(saved)} zdjęć do {images_dir}"],
            "process": None,
            "created": datetime.now().isoformat(),
            "image_count": len(saved),
        }

    return jsonify({"task_id": task_id, "image_count": len(saved), "images": saved})


@app.route("/api/start", methods=["POST"])
def api_start():
    """Start ODM processing for a task."""
    data = request.get_json(force=True)
    task_id = data.get("task_id")
    preset  = data.get("preset", "fast_ortho")
    extra   = data.get("extra_args", [])        # optional advanced args list
    concurrency = int(data.get("concurrency", os.cpu_count() or 4))

    if not task_id or task_id not in tasks:
        return jsonify({"error": "Nieznany task_id"}), 404

    with tasks_lock:
        t = tasks[task_id]
        if t["status"] == "running":
            return jsonify({"error": "Zadanie już działa"}), 409

    preset_args = PRESETS.get(preset, PRESETS["fast_ortho"])["args"]

    # Docker: mount the projects folder as /datasets inside the container.
    # Windows paths like D:\... are supported by Docker Desktop with drive sharing.
    projects_posix = str(PROJECTS).replace("\\", "/")

    if USE_DOCKER:
        cmd = [
            "docker", "run", "--rm",
            "-v", f"{projects_posix}:/datasets",
            ODM_IMAGE,
            "--project-path", "/datasets",
            "--max-concurrency", str(concurrency),
            *preset_args,
            *extra,
            task_id,      # dataset name
        ]
    else:
        ODM_RUN = ODM_DIR / "run.py"
        cmd = [
            PYTHON, str(ODM_RUN),
            "--project-path", str(PROJECTS),
            "--max-concurrency", str(concurrency),
            *preset_args,
            *extra,
            task_id,
        ]

    with tasks_lock:
        tasks[task_id]["status"] = "running"
        tasks[task_id]["log_lines"].append(f"[GUI] Polecenie: {' '.join(cmd)}")
        tasks[task_id]["log_lines"].append(f"[GUI] Preset: {preset}  |  Wątki: {concurrency}  |  {'Docker' if USE_DOCKER else 'Native'}")

    def run_odm():
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=None if USE_DOCKER else str(ODM_DIR),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            with tasks_lock:
                tasks[task_id]["process"] = proc

            for line in proc.stdout:
                line = line.rstrip()
                with tasks_lock:
                    tasks[task_id]["log_lines"].append(line)

            proc.wait()
            success = proc.returncode == 0
            status  = "done" if success else "error"
            msg     = "[GUI] ✅ Przetwarzanie zakończone pomyślnie!" if success else \
                      f"[GUI] ❌ ODM zakończył z kodem {proc.returncode}"
            with tasks_lock:
                tasks[task_id]["status"] = status
                tasks[task_id]["log_lines"].append(msg)
        except Exception as e:
            with tasks_lock:
                tasks[task_id]["status"] = "error"
                tasks[task_id]["log_lines"].append(f"[GUI] BŁĄD: {e}")

    threading.Thread(target=run_odm, daemon=True).start()
    return jsonify({"started": True, "task_id": task_id})


@app.route("/api/stop/<task_id>", methods=["POST"])
def api_stop(task_id):
    """Kill running ODM process."""
    with tasks_lock:
        t = tasks.get(task_id)
        if not t:
            return jsonify({"error": "Unknown task"}), 404
        proc = t.get("process")
        if proc and t["status"] == "running":
            proc.terminate()
            t["status"] = "stopped"
            t["log_lines"].append("[GUI] 🛑 Przetwarzanie zatrzymane przez użytkownika.")
    return jsonify({"stopped": True})


@app.route("/api/status/<task_id>")
def api_status(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
        if not t:
            return jsonify({"error": "Unknown task"}), 404
        return jsonify({
            "status": t["status"],
            "image_count": t.get("image_count", 0),
            "log_lines": t["log_lines"][-200:],   # last 200 lines
        })


@app.route("/api/logs/<task_id>")
def api_logs_sse(task_id):
    """Server-Sent Events stream for live log updates."""
    def generate():
        sent = 0
        while True:
            with tasks_lock:
                t = tasks.get(task_id)
                if not t:
                    yield "data: [error] Task not found\n\n"
                    return
                lines = t["log_lines"]
                new_lines = lines[sent:]
                status = t["status"]

            for line in new_lines:
                yield f"data: {json.dumps({'line': line, 'status': status})}\n\n"
            sent += len(new_lines)

            if status in ("done", "error", "stopped"):
                yield f"data: {json.dumps({'line': '__END__', 'status': status})}\n\n"
                return

            time.sleep(0.5)

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.route("/api/results/<task_id>")
def api_results(task_id):
    task_dir = PROJECTS / task_id
    if not task_dir.exists():
        return jsonify({"error": "Task not found"}), 404
    outputs = collect_outputs(task_dir)
    with tasks_lock:
        status = tasks.get(task_id, {}).get("status", "unknown")
    return jsonify({"status": status, "outputs": outputs})


@app.route("/api/download/<task_id>/<subdir>/<filename>")
def api_download(task_id, subdir, filename):
    """Download a result file."""
    task_dir = PROJECTS / task_id / subdir
    return send_from_directory(str(task_dir), filename, as_attachment=True)


@app.route("/api/tasks")
def api_tasks():
    with tasks_lock:
        return jsonify([
            {"task_id": tid,
             "status": t["status"],
             "created": t["created"],
             "image_count": t.get("image_count", 0)}
            for tid, t in sorted(tasks.items(), key=lambda x: x[1]["created"], reverse=True)
        ])


@app.route("/api/delete/<task_id>", methods=["DELETE"])
def api_delete(task_id):
    with tasks_lock:
        t = tasks.get(task_id)
        if t and t.get("process") and t["status"] == "running":
            t["process"].terminate()
        tasks.pop(task_id, None)
    shutil.rmtree(PROJECTS / task_id, ignore_errors=True)
    return jsonify({"deleted": True})


# ─── Main ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("=" * 60)
    print(" ODM Simple GUI")
    print(f" ODM dir   : {ODM_DIR}")
    print(f" Projects  : {PROJECTS}")
    print(f" URL       : http://localhost:5050")
    print("=" * 60)
    app.run(host="0.0.0.0", port=5050, debug=False, threaded=True)
