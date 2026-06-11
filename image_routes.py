"""
image_routes.py — add these routes to your existing dashboard.py
Place this file next to dashboard.py and add:
    from image_routes import image_bp
    app.register_blueprint(image_bp)
"""

import os
import shutil
import glob
import subprocess
from pathlib import Path
from flask import Blueprint, jsonify, request, send_file, make_response

image_bp = Blueprint("images", __name__)

# ── Ghost VM SSH config ──────────────────────────────────────────────────────
GHOST_HOST = "root@192.168.122.143"
GHOST_SSH_KEY = str(Path.home() / ".ssh" / "id_ed25519_vm")
GHOST_SSH = ["ssh", "-i", GHOST_SSH_KEY, "-o", "StrictHostKeyChecking=no",
             "-o", "ConnectTimeout=8", GHOST_HOST]
GHOST_SCP_PREFIX = [GHOST_SSH_KEY, GHOST_HOST]
GHOST_MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".gif",
                    ".bmp", ".tiff", ".tif", ".wav", ".mp3", ".ogg", ".flac", ".m4a"}

def ghost_ssh(cmd_str, timeout=30):
    """Run a command on the ghost VM via SSH. Returns CompletedProcess."""
    return subprocess.run(
        GHOST_SSH + [cmd_str],
        capture_output=True, text=True, timeout=timeout
    )

def ghost_scp_get(remote_path, local_path, timeout=30):
    """SCP a single file from ghost VM to local_path."""
    return subprocess.run(
        ["scp", "-i", GHOST_SSH_KEY, "-o", "StrictHostKeyChecking=no",
         "-o", "ConnectTimeout=8",
         f"{GHOST_HOST}:{remote_path}", str(local_path)],
        capture_output=True, timeout=timeout
    )

def is_container_running(container_name):
    """Check if a container is currently running before exec."""
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container_name],
        capture_output=True, text=True, timeout=5
    )
    return result.stdout.strip() == "true"

HIST_DIR = Path.home() / "container_history_commands"
IMAGE_DIR = HIST_DIR / "images"
TOGGLE_FILE = HIST_DIR / ".image_capture_enabled"

IMAGE_DIR.mkdir(parents=True, exist_ok=True)
INPUTS_DIR = IMAGE_DIR / "inputs"
INPUTS_DIR.mkdir(parents=True, exist_ok=True)
OUTPUTS_DIR = IMAGE_DIR / "outputs"
OUTPUTS_DIR.mkdir(parents=True, exist_ok=True)
SAMPLES_DIR = IMAGE_DIR / "samples"
SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

# Search paths for container OUTPUT images (ComfyUI etc)
OUTPUT_SEARCH_PATHS = [
    "/workspace/ComfyUI/output", "/workspace/ComfyUI/temp", "/workspace/ComfyUI/input", "/ComfyUI/output",
    "/workspace/outputs", "/app/outputs", "/root/outputs"
]

# Search paths for training INPUT images (Ostris, kohya etc)
INPUT_SEARCH_PATHS = [
    "/workspace/ai-toolkit/datasets",
    "/workspace/datasets",
    "/workspace/training_data",
    "/workspace/dataset",
    "/root/datasets",
    "/app/datasets",
]


def no_cache_response(response):
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    return response


@image_bp.route("/api/images/list")
def list_images():
    images = []
    # Samples subfolders: images/samples/<container>/
    for sub in (sorted(SAMPLES_DIR.iterdir()) if SAMPLES_DIR.exists() else []):
        if not sub.is_dir(): continue
        sub_files = sorted(
            [f for f in sub.iterdir() if f.is_file()
             and f.suffix in ('.png', '.jpg', '.jpeg', '.webp', '.mp4')
             and '_thumb' not in f.name],
            key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True
        )
        for f in sub_files:
            is_video = f.suffix == '.mp4'
            # Read ghost_path sidecar if this is the ghost subfolder
            ghost_path = None
            if sub.name == "ghost":
                import json as _json
                sidecar = sub / (f.name + ".ghost.json")
                if sidecar.exists():
                    try: ghost_path = _json.loads(sidecar.read_text()).get("ghost_path")
                    except: pass
            entry = {
                "filename": f.name,
                "container": sub.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "is_video": is_video,
                "input_type": "sample",
                "subfolder": sub.name,
                "url": f"/api/images/sample-file/{sub.name}/{f.name}?t={int(f.stat().st_mtime)}",
                "thumb_url": f"/api/images/sample-file/{sub.name}/{f.name}?t={int(f.stat().st_mtime)}"
            }
            if ghost_path:
                entry["ghost_path"] = ghost_path
            images.append(entry)
    # Input subfolders: images/inputs/<container>/
    for sub in (sorted(INPUTS_DIR.iterdir()) if INPUTS_DIR.exists() else []):
        if not sub.is_dir(): continue
        sub_files = sorted(
            [f for f in sub.iterdir() if f.is_file() and f.suffix in ('.png', '.jpg', '.jpeg', '.webp', '.mp4') and '_thumb' not in f.name],
            key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True
        )
        for f in sub_files:
            images.append({
                "filename": f.name,
                "container": sub.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "is_video": False,
                "input_type": "training",
                "subfolder": sub.name,
                "url": f"/api/images/input-file/{sub.name}/{f.name}?t={int(f.stat().st_mtime)}",
                "thumb_url": f"/api/images/input-file/{sub.name}/{f.name}?t={int(f.stat().st_mtime)}"
            })
    # Output subfolders: images/outputs/<container>/
    for sub in (sorted(OUTPUTS_DIR.iterdir()) if OUTPUTS_DIR.exists() else []):
        if not sub.is_dir(): continue
        sub_files = sorted(
            [f for f in sub.iterdir() if f.is_file() and f.suffix in ('.png', '.jpg', '.jpeg', '.webp', '.mp4')
             and '_thumb' not in f.name],
            key=lambda f: f.stat().st_mtime if f.exists() else 0, reverse=True
        )
        for f in sub_files:
            is_video = f.suffix == '.mp4'
            images.append({
                "filename": f.name,
                "container": sub.name,
                "size": f.stat().st_size,
                "mtime": f.stat().st_mtime,
                "is_video": is_video,
                "input_type": "output",
                "subfolder": sub.name,
                "url": f"/api/images/output-file/{sub.name}/{f.name}?t={int(f.stat().st_mtime)}",
                "thumb_url": f"/api/images/output-file/{sub.name}/{f.name}?t={int(f.stat().st_mtime)}"
            })
    return no_cache_response(jsonify({"images": images, "count": len(images)}))


@image_bp.route("/api/images/file/<filename>")
def serve_image(filename):
    filepath = IMAGE_DIR / filename
    if not filepath.exists() or filepath.suffix not in ('.png', '.jpg', '.jpeg', '.webp'):
        return jsonify({"error": "not found"}), 404
    if not str(filepath.resolve()).startswith(str(IMAGE_DIR.resolve())):
        return jsonify({"error": "forbidden"}), 403
    mime_map = {".png":"image/png",".jpg":"image/jpeg",".jpeg":"image/jpeg",".webp":"image/webp"}
    mimetype = mime_map.get(filepath.suffix.lower(), "image/jpeg")
    resp = make_response(send_file(filepath, mimetype=mimetype))
    return no_cache_response(resp)


@image_bp.route("/api/images/video/<filename>")
def serve_video(filename):
    """Serve an MP4 video file."""
    filepath = IMAGE_DIR / filename
    if not filepath.exists() or filepath.suffix != '.mp4':
        return jsonify({"error": "not found"}), 404
    if not str(filepath.resolve()).startswith(str(IMAGE_DIR.resolve())):
        return jsonify({"error": "forbidden"}), 403
    return send_file(filepath, mimetype="video/mp4")


@image_bp.route("/api/images/thumbnail/<filename>")
def serve_thumbnail(filename):
    """Serve thumbnail for an MP4 — extract frame with ffmpeg if needed."""
    mp4_path = None
    for search_dir in [IMAGE_DIR,
                       *([d for d in SAMPLES_DIR.iterdir() if d.is_dir()] if SAMPLES_DIR.exists() else []),
                       *([d for d in OUTPUTS_DIR.iterdir() if d.is_dir()] if OUTPUTS_DIR.exists() else [])]:
        candidate = search_dir / filename
        if candidate.exists() and candidate.suffix == '.mp4':
            mp4_path = candidate
            break
    if mp4_path is None:
        return jsonify({"error": "not found"}), 404
    # Extract thumbnail if not cached
    if not thumb_path.exists():
        result = subprocess.run([
            '/usr/bin/ffmpeg', '-i', str(mp4_path),
            '-ss', '00:00:01', '-vframes', '1',
            '-vf', 'scale=480:-1',
            '-q:v', '3', str(thumb_path), '-y'
        ], capture_output=True, timeout=30)
        if result.returncode != 0 or not thumb_path.exists():
            return jsonify({"error": "thumbnail extraction failed"}), 500
    resp = make_response(send_file(thumb_path, mimetype="image/jpeg"))
    return no_cache_response(resp)


@image_bp.route("/api/images/delete-folder/<folder_type>/<container_name>", methods=["DELETE"])
def delete_folder(folder_type, container_name):
    """Delete all files for a given folder (flat/output/input) and container."""
    deleted = []
    if folder_type == "sample":
        target = SAMPLES_DIR / container_name
        if target.exists() and target.is_dir():
            for f in target.iterdir():
                if f.is_file():
                    f.unlink()
                    deleted.append(f.name)
            target.rmdir()
    elif folder_type == "output":
        target = OUTPUTS_DIR / container_name
        if target.exists() and target.is_dir():
            for f in target.iterdir():
                if f.is_file():
                    f.unlink()
                    deleted.append(f.name)
            target.rmdir()
    elif folder_type == "input":
        target = INPUTS_DIR / container_name
        if target.exists() and target.is_dir():
            for f in target.iterdir():
                if f.is_file():
                    f.unlink()
                    deleted.append(f.name)
            target.rmdir()
    elif folder_type == "flat":
        # Delete flat files matching container prefix
        for f in IMAGE_DIR.iterdir():
            if f.is_file() and f.name.startswith(container_name) and '_thumb' not in f.name:
                f.unlink()
                deleted.append(f.name)
            elif f.is_file() and f.name.startswith(container_name) and '_thumb' in f.name:
                f.unlink()  # also delete thumbs
    else:
        return jsonify({"error": "invalid folder_type"}), 400
    return no_cache_response(jsonify({"deleted": deleted, "count": len(deleted)}))


@image_bp.route("/api/images/delete/<filename>", methods=["DELETE"])
def delete_image(filename):
    # Search IMAGE_DIR root and all subdirs
    filepath = None
    search_dirs = [IMAGE_DIR]
    for subdir in [SAMPLES_DIR, OUTPUTS_DIR, INPUTS_DIR]:
        if subdir.exists():
            search_dirs += [d for d in subdir.iterdir() if d.is_dir()]
    for d in search_dirs:
        candidate = d / filename
        if candidate.exists():
            filepath = candidate
            break
    if filepath is None:
        return jsonify({"error": "not found"}), 404
    # Also delete associated thumbnail if MP4
    if filepath.suffix == '.mp4':
        thumb = filepath.parent / filename.replace('.mp4', '_thumb.jpg')
        if thumb.exists():
            thumb.unlink(missing_ok=True)
    try:
        filepath.unlink()
    except Exception as e:
        return jsonify({"error": str(e)}), 500
    return no_cache_response(jsonify({"deleted": filename}))


@image_bp.route("/api/images/delete-all", methods=["DELETE"])
def delete_all_images():
    deleted = []
    # Flat files in IMAGE_DIR root
    for f in IMAGE_DIR.iterdir():
        if f.is_file() and f.suffix in ('.png', '.jpg', '.jpeg', '.mp4', '.webp', '.gif'):
            try: f.unlink()
            except: pass
            deleted.append(f.name)
    # All subfolders
    for subdir in [SAMPLES_DIR, OUTPUTS_DIR, INPUTS_DIR]:
        if not subdir.exists(): continue
        for container_dir in subdir.iterdir():
            if not container_dir.is_dir(): continue
            for f in container_dir.iterdir():
                if f.is_file():
                    try: f.unlink()
                    except: pass
                    deleted.append(f.name)
            try: shutil.rmtree(str(container_dir))
            except: pass
    return no_cache_response(jsonify({"deleted": deleted, "count": len(deleted)}))


@image_bp.route("/api/images/scan-container/<container_name>")
def scan_container(container_name):
    """Scan a running container for all media files and return their paths."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container"}), 400

    MEDIA_EXTS = "*.png *.jpg *.jpeg *.webp *.mp4 *.gif *.bmp *.tiff *.tif *.wav *.mp3 *.ogg *.flac *.m4a"
    patterns = " -o ".join([f"-name '{e}'" for e in MEDIA_EXTS.split()])

    if not is_container_running(container_name):
        return jsonify({"error": "container is not running"}), 404
    scan_paths = "/workspace /root /home /output /data"
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {scan_paths} \( {patterns} \) -size +100k "
         f"-exec stat --format='%s %n' {{}} \; 2>/dev/null | sort -k2"],
        capture_output=True, text=True, timeout=60
    )

    files = []
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            try:
                size = int(parts[0])
                path = parts[1]
                ext = Path(path).suffix.lower()
                ftype = "video" if ext == ".mp4" else                         "audio" if ext in {".wav",".mp3",".ogg",".flac",".m4a"} else "image"
                files.append({
                    "path": path,
                    "name": Path(path).name,
                    "size": size,
                    "type": ftype,
                    "ext": ext
                })
            except: pass

    # Group by directory
    dirs = {}
    for f in files:
        d = str(Path(f["path"]).parent)
        if d not in dirs:
            dirs[d] = []
        dirs[d].append(f)

    return no_cache_response(jsonify({
        "container": container_name,
        "total": len(files),
        "files": files,
        "dirs": {k: len(v) for k, v in dirs.items()}
    }))


@image_bp.route("/api/images/add-to-samples", methods=["POST"])
def add_to_samples():
    """Copy a single file from a container into samples/<container>/."""
    data = request.get_json()
    container = data.get("container", "").strip()
    path = data.get("path", "").strip()
    if not container or not container.startswith("C."):
        return jsonify({"error": "invalid container"}), 400
    if not path:
        return jsonify({"error": "path required"}), 400
    dest_dir = SAMPLES_DIR / container
    dest_dir.mkdir(parents=True, exist_ok=True)
    fname = Path(path).name
    dest = dest_dir / fname
    # Dedup by size
    if dest.exists():
        size_r = subprocess.run(
            ["docker", "exec", container, "bash", "-c", f"stat --format='%s' {path} 2>/dev/null"],
            capture_output=True, text=True, timeout=10
        )
        try:
            if int(size_r.stdout.strip()) == dest.stat().st_size:
                return no_cache_response(jsonify({"saved": fname, "skipped": True}))
        except: pass
        stem, ext = Path(fname).stem, Path(fname).suffix
        dest = dest_dir / f"{stem}_{int(__import__('time').time())}{ext}"
    cp = subprocess.run(
        ["docker", "cp", f"{container}:{path}", str(dest)],
        capture_output=True, timeout=30
    )
    if cp.returncode != 0:
        return jsonify({"error": "docker cp failed"}), 500
    if not dest.exists():
        return jsonify({"error": "file not created"}), 500
    return no_cache_response(jsonify({"saved": dest.name, "container": container}))


@image_bp.route("/api/images/import-dir", methods=["POST"])
def import_dir():
    """Recursively import all images/videos from a directory inside a container into samples/<container>/."""
    data = request.get_json()
    src_dir = data.get("path", "").strip()
    container = data.get("container", "").strip()

    if not src_dir:
        return jsonify({"error": "path required"}), 400
    if not container or not container.startswith("C."):
        return jsonify({"error": "valid container name required"}), 400

    MEDIA_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".gif", ".bmp", ".tiff", ".tif"}

    # Find all matching files in the container directory
    ext_pattern = " -o ".join([f"-name '*.{e.lstrip('.')}'" for e in MEDIA_EXTS])
    result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         f"find {src_dir} -type f -size +100k \( {ext_pattern} \) 2>/dev/null"],
        capture_output=True, text=True, timeout=30
    )
    if result.returncode != 0 and not result.stdout.strip():
        return jsonify({"error": f"Could not read {src_dir} in container {container}"}), 404

    files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    if not files:
        return jsonify({"error": f"No media files found in {src_dir}"}), 404

    dest_dir = SAMPLES_DIR / container
    dest_dir.mkdir(parents=True, exist_ok=True)

    # Get file sizes for dedup
    size_result = subprocess.run(
        ["docker", "exec", container, "bash", "-c",
         f"find {src_dir} -type f -size +100k \( {ext_pattern} \) -exec stat --format='%s %n' {{}} \; 2>/dev/null"],
        capture_output=True, text=True, timeout=30
    )
    container_sizes = {}
    for line in size_result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            try: container_sizes[parts[1]] = int(parts[0])
            except: pass

    copied = 0
    skipped = 0
    failed = 0

    for src_path in files:
        fname = Path(src_path).name
        if "_thumb" in fname:
            continue
        dest_file = dest_dir / fname
        # Dedup by name+size
        if dest_file.exists():
            container_size = container_sizes.get(src_path)
            if container_size is not None and dest_file.stat().st_size == container_size:
                skipped += 1
                continue
            stem, ext = Path(fname).stem, Path(fname).suffix
            dest_file = dest_dir / f"{stem}_{copied}{ext}"
        cp = subprocess.run(
            ["docker", "cp", f"{container}:{src_path}", str(dest_file)],
            capture_output=True, timeout=30
        )
        if cp.returncode == 0:
            copied += 1
        else:
            failed += 1

    return no_cache_response(jsonify({
        "container": container,
        "source": src_dir,
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
        "total": len(files)
    }))


@image_bp.route("/api/images/toggle", methods=["GET"])
def get_toggle():
    enabled = TOGGLE_FILE.exists()
    return no_cache_response(jsonify({"enabled": enabled}))


@image_bp.route("/api/images/toggle", methods=["POST"])
def set_toggle():
    data = request.get_json()
    enabled = data.get("enabled", False)
    if enabled:
        TOGGLE_FILE.touch()
    else:
        TOGGLE_FILE.unlink(missing_ok=True)
    return no_cache_response(jsonify({"enabled": enabled}))


@image_bp.route("/api/images/fetch-latest/<container_name>", methods=["POST"])
def fetch_latest(container_name):
    """Pull latest image from a running container on demand."""
    # Sanitize container name
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container name"}), 400

    # Search for PNG and MP4 outputs
    search_paths = OUTPUT_SEARCH_PATHS
    paths_str = " ".join(search_paths)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 5 \( -name '*.png' -o -name '*.jpg' -o -name '*.mp4' -o -name '*.webp' \) "
         f"-not -name '*.preview.png' 2>/dev/null | xargs -I{{}} stat --format='%Y %n' {{}} 2>/dev/null | sort -n | tail -20 | awk '{{print $2}}'"],
        capture_output=True, text=True, timeout=15
    )
    files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    if not files:
        return jsonify({"error": "no images or videos found in container"}), 404

    # Prefer most recent MP4, fall back to PNG
    mp4s  = sorted([f for f in files if f.endswith('.mp4')])
    imgs  = sorted([f for f in files if not f.endswith('.mp4')])
    latest = mp4s[-1] if mp4s else imgs[-1]
    is_video = latest.endswith('.mp4')

    ext = Path(latest).suffix or '.png'
    sample_dir = SAMPLES_DIR / container_name
    sample_dir.mkdir(parents=True, exist_ok=True)
    dest = sample_dir / f"latest{ext}"
    cp_result = subprocess.run(
        ["docker", "cp", f"{container_name}:{latest}", str(dest)],
        capture_output=True, timeout=30
    )
    if cp_result.returncode != 0:
        return jsonify({"error": "docker cp failed", "detail": cp_result.stderr.decode()}), 500

    # Extract thumbnail from MP4
    if is_video:
        thumb = sample_dir / "latest_thumb.jpg"
        subprocess.run([
            '/usr/bin/ffmpeg', '-i', str(dest), '-ss', '00:00:01', '-vframes', '1',
            '-vf', 'scale=480:-1', '-q:v', '3', str(thumb), '-y'
        ], capture_output=True, timeout=30)

    if not dest.exists():
        return jsonify({"error": "docker cp succeeded but file not found"}), 500
    t = int(dest.stat().st_mtime)
    return no_cache_response(jsonify({
        "saved": dest.name,
        "source": latest,
        "is_video": is_video,
        "url": f"/api/images/sample-file/{container_name}/{dest.name}?t={t}",
        "thumb_url": f"/api/images/sample-file/{container_name}/{dest.name}?t={t}"
    }))


@image_bp.route("/api/images/sample-file/<subfolder>/<filename>")
def serve_sample_file(subfolder, filename):
    """Serve a file from samples/<subfolder>/."""
    filepath = SAMPLES_DIR / subfolder / filename
    if not filepath.exists():
        return jsonify({"error": "not found"}), 404
    if not str(filepath.resolve()).startswith(str(SAMPLES_DIR.resolve())):
        return jsonify({"error": "forbidden"}), 403
    ext = filepath.suffix.lower()
    if ext == '.mp4':
        return send_file(filepath, mimetype="video/mp4")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
            "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    resp = make_response(send_file(filepath, mimetype=mime))
    return no_cache_response(resp)


@image_bp.route("/api/images/output-file/<subfolder>/<filename>")
def serve_output_file(subfolder, filename):
    """Serve a file from outputs/<subfolder>/."""
    filepath = OUTPUTS_DIR / subfolder / filename
    if not filepath.exists():
        return jsonify({"error": "not found"}), 404
    if not str(filepath.resolve()).startswith(str(OUTPUTS_DIR.resolve())):
        return jsonify({"error": "forbidden"}), 403
    ext = filepath.suffix.lower()
    if ext == '.mp4':
        return send_file(filepath, mimetype="video/mp4")
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    resp = make_response(send_file(filepath, mimetype=mime))
    return no_cache_response(resp)


@image_bp.route("/api/images/copy-all-outputs/<container_name>", methods=["POST"])
def copy_all_outputs(container_name):
    """Copy all output images from a container into images/outputs/<container_name>/."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container name"}), 400
    if not is_container_running(container_name):
        return jsonify({"error": "container is not running"}), 404
    dest_dir = OUTPUTS_DIR / container_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths_str = " ".join(OUTPUT_SEARCH_PATHS)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 5 "
         r"\( -name '*.png' -o -name '*.jpg' -o -name '*.mp4' -o -name '*.webp' \) "
         r"-size +100k -not -name '*.preview.png' "
         "2>/dev/null"],
        capture_output=True, text=True, timeout=30
    )
    files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    if not files:
        return jsonify({"error": "no output files found in container"}), 404
    # Get file sizes from container for dedup
    size_result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 5 "
         r"\( -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' -o -name '*.mp4' -o -name '*.webp' \) "
         r"-size +100k -not -name '*.preview.png' "
         r"-exec stat --format='%s %n' {} \; 2>/dev/null"],
        capture_output=True, text=True, timeout=30
    )
    container_sizes = {}
    for line in size_result.stdout.strip().splitlines():
        parts = line.strip().split(' ', 1)
        if len(parts) == 2:
            try: container_sizes[parts[1]] = int(parts[0])
            except: pass
    copied = 0
    skipped = 0
    failed = 0
    for src_path in files:
        fname = Path(src_path).name
        dest_file = dest_dir / fname
        # Skip if file exists with same size (exact duplicate)
        if dest_file.exists():
            container_size = container_sizes.get(src_path)
            if container_size is not None and dest_file.stat().st_size == container_size:
                skipped += 1
                continue
            # Size differs — file was updated, overwrite it
        cp = subprocess.run(
            ["docker", "cp", f"{container_name}:{src_path}", str(dest_file)],
            capture_output=True, timeout=30
        )
        if cp.returncode == 0:
            copied += 1
        else:
            failed += 1
    return no_cache_response(jsonify({
        "container": container_name,
        "dest": str(dest_dir),
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
        "total": len(files)
    }))


@image_bp.route("/api/images/list-output-folders")
def list_output_folders():
    """List all saved output folders with file counts."""
    folders = []
    if OUTPUTS_DIR.exists():
        for sub in sorted(OUTPUTS_DIR.iterdir()):
            if sub.is_dir():
                count = sum(1 for f in sub.iterdir() if f.is_file()
                            and f.suffix in ('.jpg','.jpeg','.png','.webp','.mp4'))
                size = sum(f.stat().st_size for f in sub.iterdir() if f.is_file())
                folders.append({
                    "name": sub.name,
                    "count": count,
                    "size_mb": round(size / 1024 / 1024, 1)
                })
    return no_cache_response(jsonify({"folders": folders}))


@image_bp.route("/api/images/input-file/<subfolder>/<filename>")
def serve_input_file(subfolder, filename):
    """Serve a file from inputs/<subfolder>/."""
    filepath = INPUTS_DIR / subfolder / filename
    if not filepath.exists():
        return jsonify({"error": "not found"}), 404
    if not str(filepath.resolve()).startswith(str(INPUTS_DIR.resolve())):
        return jsonify({"error": "forbidden"}), 403
    ext = filepath.suffix.lower()
    mime = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp"}.get(ext.lstrip("."), "image/jpeg")
    resp = make_response(send_file(filepath, mimetype=mime))
    return no_cache_response(resp)


@image_bp.route("/api/images/copy-all-inputs/<container_name>", methods=["POST"])
def copy_all_inputs(container_name):
    """Copy all training input images from a container into images/inputs/<container_name>/."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container name"}), 400
    if not is_container_running(container_name):
        return jsonify({"error": "container is not running"}), 404
    dest_dir = INPUTS_DIR / container_name
    dest_dir.mkdir(parents=True, exist_ok=True)
    paths_str = " ".join(INPUT_SEARCH_PATHS)
    # Get all input image files
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 6 "
         r"( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) -size +100k "
         "2>/dev/null"],
        capture_output=True, text=True, timeout=30
    )
    files = [f.strip() for f in result.stdout.strip().splitlines() if f.strip()]
    if not files:
        return jsonify({"error": "no input images found in container"}), 404
    # Get sizes for dedup
    size_result2 = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 6 "
         r"\( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) -size +100k "
         r"-exec stat --format='%s %n' {} \; 2>/dev/null"],
        capture_output=True, text=True, timeout=30
    )
    input_sizes = {}
    for line in size_result2.stdout.strip().splitlines():
        parts = line.strip().split(' ', 1)
        if len(parts) == 2:
            try: input_sizes[parts[1]] = int(parts[0])
            except: pass
    copied = 0
    skipped = 0
    failed = 0
    for src_path in files:
        fname = Path(src_path).name
        dest_file = dest_dir / fname
        if dest_file.exists():
            container_size = input_sizes.get(src_path)
            if container_size is not None and dest_file.stat().st_size == container_size:
                skipped += 1
                continue
        cp = subprocess.run(
            ["docker", "cp", f"{container_name}:{src_path}", str(dest_file)],
            capture_output=True, timeout=30
        )
        if cp.returncode == 0:
            copied += 1
        else:
            failed += 1
    return no_cache_response(jsonify({
        "container": container_name,
        "dest": str(dest_dir),
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
        "total": len(files)
    }))


@image_bp.route("/api/images/list-input-folders")
def list_input_folders():
    """List all saved input folders with file counts."""
    folders = []
    if INPUTS_DIR.exists():
        for sub in sorted(INPUTS_DIR.iterdir()):
            if sub.is_dir():
                count = sum(1 for f in sub.iterdir() if f.is_file() and f.suffix in ('.jpg','.jpeg','.png','.webp'))
                size = sum(f.stat().st_size for f in sub.iterdir() if f.is_file())
                folders.append({
                    "name": sub.name,
                    "count": count,
                    "size_mb": round(size / 1024 / 1024, 1)
                })
    return no_cache_response(jsonify({"folders": folders}))


# ── Ghost VM endpoints ──────────────────────────────────────────────────────

@image_bp.route("/api/images/scan-ghost")
def scan_ghost():
    """Scan a directory on the ghost VM for media files."""
    scan_path = request.args.get("path", "/home/rich-rob/stable-diffusion-webui-forge /root /workspace /data").strip()
    try:
        # Build find command — iterate each path separately so missing dirs are skipped
        paths = [p for p in scan_path.split() if p]
        find_cmds = []
        for p in paths:
            find_cmds.append(
                f"find '{p}' -type f -size +100k "
                r"\( -name '*.png' -o -name '*.jpg' -o -name '*.jpeg' -o -name '*.webp' "
                r"-o -name '*.mp4' -o -name '*.gif' -o -name '*.wav' -o -name '*.mp3' "
                r"-o -name '*.ogg' -o -name '*.flac' -o -name '*.m4a' \) "
                r"-printf '%s %p\n' 2>/dev/null"
            )
        full_cmd = " ; ".join(find_cmds) + " | sort -k2"
        result = ghost_ssh(full_cmd, timeout=60)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "SSH scan timed out"}), 504
    except Exception as e:
        return jsonify({"error": f"SSH error: {e}"}), 500

    files = []
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            try:
                size = int(parts[0])
                path = parts[1]
                ext = Path(path).suffix.lower()
                ftype = ("video" if ext == ".mp4"
                         else "audio" if ext in {".wav",".mp3",".ogg",".flac",".m4a"}
                         else "image")
                files.append({"path": path, "name": Path(path).name,
                               "size": size, "type": ftype, "ext": ext})
            except:
                pass

    dirs = {}
    for f in files:
        d = str(Path(f["path"]).parent)
        dirs[d] = dirs.get(d, 0) + 1

    return no_cache_response(jsonify({
        "source": "ghost",
        "scan_path": scan_path,
        "total": len(files),
        "files": files,
        "dirs": dirs
    }))


@image_bp.route("/api/images/fetch-from-ghost", methods=["POST"])
def fetch_from_ghost():
    """SCP a single file from ghost VM into samples/ghost/."""
    data = request.get_json()
    remote_path = data.get("path", "").strip()
    if not remote_path:
        return jsonify({"error": "path required"}), 400

    dest_dir = SAMPLES_DIR / "ghost"
    dest_dir.mkdir(parents=True, exist_ok=True)

    fname = Path(remote_path).name
    ext = Path(fname).suffix.lower()
    dest = dest_dir / fname

    # Dedup by size
    if dest.exists():
        try:
            r = ghost_ssh(f"stat -c '%s' '{remote_path}' 2>/dev/null")
            remote_size = int(r.stdout.strip())
            if remote_size == dest.stat().st_size:
                t = int(dest.stat().st_mtime)
                return no_cache_response(jsonify({
                    "saved": fname, "skipped": True,
                    "url": f"/api/images/sample-file/ghost/{fname}?t={t}",
                    "thumb_url": f"/api/images/sample-file/ghost/{fname}?t={t}"
                }))
        except:
            pass
        import time as _t
        stem, sfx = Path(fname).stem, Path(fname).suffix
        dest = dest_dir / f"{stem}_{int(_t.time())}{sfx}"

    try:
        cp = ghost_scp_get(remote_path, dest, timeout=60)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "SCP timed out"}), 504
    except Exception as e:
        return jsonify({"error": f"SCP error: {e}"}), 500

    if cp.returncode != 0:
        return jsonify({"error": "SCP failed", "detail": cp.stderr.decode()}), 500
    if not dest.exists():
        return jsonify({"error": "file not created after SCP"}), 500

    # Extract video thumbnail
    if ext == ".mp4":
        thumb = dest_dir / dest.name.replace(".mp4", "_thumb.jpg")
        subprocess.run([
            "/usr/bin/ffmpeg", "-i", str(dest), "-ss", "00:00:01",
            "-vframes", "1", "-vf", "scale=480:-1", "-q:v", "3",
            str(thumb), "-y"
        ], capture_output=True, timeout=30)

    # Save ghost_path sidecar so we can delete the original later
    sidecar = dest_dir / (dest.name + ".ghost.json")
    import json as _json
    sidecar.write_text(_json.dumps({"ghost_path": remote_path}))

    t = int(dest.stat().st_mtime)
    return no_cache_response(jsonify({
        "saved": dest.name,
        "source": remote_path,
        "is_video": ext == ".mp4",
        "ghost_path": remote_path,
        "url": f"/api/images/sample-file/ghost/{dest.name}?t={t}",
        "thumb_url": f"/api/images/sample-file/ghost/{dest.name}?t={t}"
    }))


@image_bp.route("/api/images/delete-ghost", methods=["DELETE"])
def delete_ghost():
    """Hard-delete a file on the ghost VM via SSH rm."""
    data = request.get_json()
    remote_path = data.get("path", "").strip()
    if not remote_path:
        return jsonify({"error": "path required"}), 400
    # Basic safety — must be an absolute path, no shell metacharacters
    if not remote_path.startswith("/") or any(c in remote_path for c in [";","&","|","`","$",">"]):
        return jsonify({"error": "invalid path"}), 400
    try:
        r = ghost_ssh(f"rm -f '{remote_path}' && echo OK")
    except Exception as e:
        return jsonify({"error": f"SSH error: {e}"}), 500
    if "OK" not in r.stdout:
        return jsonify({"error": "rm failed", "detail": r.stderr}), 500
    return no_cache_response(jsonify({"deleted": remote_path}))


@image_bp.route("/api/images/import-dir-ghost", methods=["POST"])
def import_dir_ghost():
    """Recursively SCP all media from a ghost VM directory into samples/ghost/."""
    data = request.get_json()
    src_dir = data.get("path", "").strip()
    if not src_dir:
        return jsonify({"error": "path required"}), 400

    MEDIA_EXTS_FIND = None  # unused — see build below
    try:
        # Run separate find per extension to avoid shell grouping issues over SSH
        exts = ["png", "jpg", "jpeg", "webp", "mp4", "gif"]
        all_lines = []
        for ext in exts:
            r = ghost_ssh(f"find '{src_dir}' -type f -size +100k -iname '*.{ext}' -printf '%s %p\\n' 2>/dev/null", timeout=60)
            if r.stdout.strip():
                all_lines.extend(r.stdout.strip().splitlines())
        result_stdout = "\n".join(all_lines)
    except subprocess.TimeoutExpired:
        return jsonify({"error": "SSH scan timed out"}), 504

    remote_files = {}
    for line in result_stdout.strip().splitlines():
        parts = line.strip().split(" ", 1)
        if len(parts) == 2:
            try:
                remote_files[parts[1]] = int(parts[0])
            except:
                pass

    if not remote_files:
        return jsonify({"error": f"No media files found in {src_dir}"}), 404

    dest_dir = SAMPLES_DIR / "ghost"
    dest_dir.mkdir(parents=True, exist_ok=True)

    copied = skipped = failed = 0
    for src_path, remote_size in remote_files.items():
        fname = Path(src_path).name
        if "_thumb" in fname:
            continue
        dest_file = dest_dir / fname
        if dest_file.exists() and dest_file.stat().st_size == remote_size:
            skipped += 1
            continue
        if dest_file.exists():
            import time as _t
            stem, sfx = Path(fname).stem, Path(fname).suffix
            dest_file = dest_dir / f"{stem}_{int(_t.time())}{sfx}"
        try:
            cp = ghost_scp_get(src_path, dest_file, timeout=60)
            if cp.returncode == 0:
                copied += 1
            else:
                failed += 1
        except:
            failed += 1

    return no_cache_response(jsonify({
        "source": "ghost",
        "path": src_dir,
        "copied": copied,
        "skipped": skipped,
        "failed": failed,
        "total": len(remote_files)
    }))


@image_bp.route("/api/images/delete-ghost-file/<filename>", methods=["DELETE"])
def delete_ghost_file(filename):
    """Delete local vault copy AND the original file on the ghost VM."""
    import json as _json
    # Find the file in samples/ghost/
    ghost_dir = SAMPLES_DIR / "ghost"
    filepath = ghost_dir / filename
    if not filepath.exists():
        return jsonify({"error": "not found locally"}), 404

    # Read ghost_path from sidecar
    sidecar = ghost_dir / (filename + ".ghost.json")
    ghost_path = None
    if sidecar.exists():
        try: ghost_path = _json.loads(sidecar.read_text()).get("ghost_path")
        except: pass

    result = {"local": False, "ghost": False, "ghost_path": ghost_path}

    # Delete local file + sidecar
    try:
        filepath.unlink()
        if sidecar.exists(): sidecar.unlink()
        result["local"] = True
    except Exception as e:
        return jsonify({"error": f"local delete failed: {e}"}), 500

    # Delete on ghost VM if we have the path
    if ghost_path:
        if not ghost_path.startswith("/") or any(c in ghost_path for c in [";","&","|","`","$",">"]):
            result["ghost"] = False
            result["ghost_error"] = "invalid path"
        else:
            try:
                r = ghost_ssh(f"rm -f '{ghost_path}' && echo OK")
                result["ghost"] = "OK" in r.stdout
                if not result["ghost"]:
                    result["ghost_error"] = r.stderr.strip()
            except Exception as e:
                result["ghost_error"] = str(e)

    return no_cache_response(jsonify(result))


@image_bp.route("/api/images/delete-ghost-folder", methods=["DELETE"])
def delete_ghost_folder():
    """Delete all files in samples/ghost/ locally, and optionally on ghost VM too."""
    import json as _json
    data = request.get_json() or {}
    also_ghost = data.get("also_ghost", False)

    ghost_dir = SAMPLES_DIR / "ghost"
    if not ghost_dir.exists():
        return no_cache_response(jsonify({"local_deleted": 0, "ghost_deleted": 0}))

    local_deleted = 0
    ghost_deleted = 0
    ghost_failed = 0

    for f in list(ghost_dir.iterdir()):
        if not f.is_file(): continue
        if f.suffix == ".json" and f.name.endswith(".ghost.json"): continue  # handle with parent
        # Read sidecar
        sidecar = ghost_dir / (f.name + ".ghost.json")
        ghost_path = None
        if sidecar.exists():
            try: ghost_path = _json.loads(sidecar.read_text()).get("ghost_path")
            except: pass
        # Delete ghost original first if requested
        if also_ghost and ghost_path:
            if ghost_path.startswith("/") and not any(c in ghost_path for c in [";","&","|","`","$",">"]):
                try:
                    r = ghost_ssh(f"rm -f '{ghost_path}' && echo OK")
                    if "OK" in r.stdout: ghost_deleted += 1
                    else: ghost_failed += 1
                except: ghost_failed += 1
        # Delete local
        try:
            f.unlink()
            if sidecar.exists(): sidecar.unlink()
            local_deleted += 1
        except: pass

    # Try to remove the dir if empty
    try:
        if not any(ghost_dir.iterdir()): ghost_dir.rmdir()
    except: pass

    return no_cache_response(jsonify({
        "local_deleted": local_deleted,
        "ghost_deleted": ghost_deleted,
        "ghost_failed": ghost_failed
    }))


@image_bp.route("/images")
def image_dashboard():
    """Serve the image dashboard HTML."""
    html_path = Path(__file__).parent / "image_dashboard.html"
    if html_path.exists():
        resp = make_response(html_path.read_text())
        resp.content_type = "text/html"
        return no_cache_response(resp)
    return "image_dashboard.html not found", 404


@image_bp.route("/api/images/container-stats/<container_name>")
def container_stats(container_name):
    """Count output files inside a running container."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid"}), 400
    paths_str = " ".join(OUTPUT_SEARCH_PATHS)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 5 "
         r"\( -name '*.png' -o -name '*.mp4' \) -size +100k -not -name '*.preview.png' "
         "2>/dev/null | wc -l"],
        capture_output=True, text=True, timeout=10
    )
    count = int(result.stdout.strip() or 0)
    return no_cache_response(jsonify({"container": container_name, "file_count": count}))


@image_bp.route("/api/images/fetch-random/<container_name>", methods=["POST"])
def fetch_random(container_name):
    """Pull a random output file from a running container."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container name"}), 400
    paths_str = " ".join(OUTPUT_SEARCH_PATHS)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 5 "
         r"\( -name '*.png' -o -name '*.mp4' \) -not -name '*.preview.png' "
         "2>/dev/null | shuf -n 1"],
        capture_output=True, text=True, timeout=15
    )
    chosen = result.stdout.strip()
    if not chosen:
        return jsonify({"error": "no files found"}), 404
    import time as _t
    is_video = chosen.endswith('.mp4')
    ext = '.mp4' if is_video else '.png'
    sample_dir = SAMPLES_DIR / container_name
    sample_dir.mkdir(parents=True, exist_ok=True)
    dest = sample_dir / f"random_{int(_t.time())}{ext}"
    cp = subprocess.run(
        ["docker", "cp", f"{container_name}:{chosen}", str(dest)],
        capture_output=True, timeout=30
    )
    if cp.returncode != 0:
        return jsonify({"error": "docker cp failed"}), 500
    if is_video:
        thumb = sample_dir / dest.name.replace('.mp4', '_thumb.jpg')
        subprocess.run([
            '/usr/bin/ffmpeg', '-i', str(dest), '-ss', '00:00:01',
            '-vframes', '1', '-vf', 'scale=480:-1', '-q:v', '3',
            str(thumb), '-y'
        ], capture_output=True, timeout=30)
    return no_cache_response(jsonify({
        "saved": dest.name,
        "source": chosen,
        "is_video": is_video,
        "url": f"/api/images/sample-file/{container_name}/{dest.name}?t={int(dest.stat().st_mtime)}",
        "thumb_url": f"/api/images/sample-file/{container_name}/{dest.name}?t={int(dest.stat().st_mtime)}"
    }))


@image_bp.route("/api/images/active-container")
def get_active_container():
    """Find the currently running Vast.ai container and classify its type."""
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.Names}}\t{{.Image}}"],
        capture_output=True, text=True
    )
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            name, image = parts
            if name.startswith("C."):
                img_lower = image.lower()
                if "ostris" in img_lower or "ai-toolkit" in img_lower or "kohya" in img_lower:
                    ctype = "training"
                elif "comfyui" in img_lower or "comfy" in img_lower:
                    ctype = "comfyui"
                elif "chroma" in img_lower or "pearl" in img_lower or "prl" in img_lower:
                    ctype = "inference"
                else:
                    ctype = "generic"
                return no_cache_response(jsonify({"name": name, "image": image, "container_type": ctype}))
    return no_cache_response(jsonify({"name": None}))


@image_bp.route("/api/images/input-stats/<container_name>")
def input_stats(container_name):
    """Count training input images inside a running container."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid"}), 400
    paths_str = " ".join(INPUT_SEARCH_PATHS)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 6 "
         r"( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) "
         "2>/dev/null | wc -l"],
        capture_output=True, text=True, timeout=10
    )
    count = int(result.stdout.strip() or 0)
    # Also find dataset folder names
    dsets = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 2 -mindepth 1 -type d 2>/dev/null"],
        capture_output=True, text=True, timeout=10
    )
    folders = [d.strip() for d in dsets.stdout.strip().splitlines() if d.strip()]
    return no_cache_response(jsonify({
        "container": container_name,
        "input_count": count,
        "dataset_folders": folders
    }))


@image_bp.route("/api/images/fetch-input-latest/<container_name>", methods=["POST"])
def fetch_input_latest(container_name):
    """Pull latest training input image from a running container."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container name"}), 400
    paths_str = " ".join(INPUT_SEARCH_PATHS)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 6 "
         r"( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) "
         "2>/dev/null | xargs -I{} stat --format='%Y %n' {} 2>/dev/null | sort -n | tail -1 | awk '{print $2}'"],
        capture_output=True, text=True, timeout=15
    )
    latest = result.stdout.strip()
    if not latest:
        return jsonify({"error": "no input images found in container"}), 404
    import time as _t
    ext = Path(latest).suffix or ".jpg"
    sample_dir = SAMPLES_DIR / container_name
    sample_dir.mkdir(parents=True, exist_ok=True)
    dest = sample_dir / f"input_latest{ext}"
    cp = subprocess.run(
        ["docker", "cp", f"{container_name}:{latest}", str(dest)],
        capture_output=True, timeout=30
    )
    if cp.returncode != 0:
        return jsonify({"error": "docker cp failed", "detail": cp.stderr.decode()}), 500
    return no_cache_response(jsonify({
        "saved": dest.name,
        "source": latest,
        "is_video": False,
        "input_type": "training",
        "url": f"/api/images/sample-file/{container_name}/{dest.name}?t={int(dest.stat().st_mtime)}",
        "thumb_url": f"/api/images/sample-file/{container_name}/{dest.name}?t={int(dest.stat().st_mtime)}"
    }))


@image_bp.route("/api/images/fetch-input-random/<container_name>", methods=["POST"])
def fetch_input_random(container_name):
    """Pull a random training input image from a running container."""
    if not container_name.startswith("C."):
        return jsonify({"error": "invalid container name"}), 400
    paths_str = " ".join(INPUT_SEARCH_PATHS)
    result = subprocess.run(
        ["docker", "exec", container_name, "bash", "-c",
         f"find {paths_str} -maxdepth 6 "
         r"( -name '*.jpg' -o -name '*.jpeg' -o -name '*.png' -o -name '*.webp' \) "
         "2>/dev/null | shuf -n 1"],
        capture_output=True, text=True, timeout=15
    )
    chosen = result.stdout.strip()
    if not chosen:
        return jsonify({"error": "no input images found"}), 404
    import time as _t
    ext = Path(chosen).suffix or ".jpg"
    sample_dir = SAMPLES_DIR / container_name
    sample_dir.mkdir(parents=True, exist_ok=True)
    dest = sample_dir / f"input_random_{int(_t.time())}{ext}"
    cp = subprocess.run(
        ["docker", "cp", f"{container_name}:{chosen}", str(dest)],
        capture_output=True, timeout=30
    )
    if cp.returncode != 0:
        return jsonify({"error": "docker cp failed"}), 500
    return no_cache_response(jsonify({
        "saved": dest.name,
        "source": chosen,
        "is_video": False,
        "input_type": "training",
        "url": f"/api/images/sample-file/{container_name}/{dest.name}?t={int(dest.stat().st_mtime)}",
        "thumb_url": f"/api/images/sample-file/{container_name}/{dest.name}?t={int(dest.stat().st_mtime)}"
    }))
