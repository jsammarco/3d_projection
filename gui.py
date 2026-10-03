"""Shadow Studio: an offline, local browser GUI for the projector generator.

Run python gui.py, or double-click Start Shadow Studio.cmd.
The server listens only on 127.0.0.1. Generated work stays in a private temporary
directory until the user chooses Save STLs in their browser.
"""

from __future__ import annotations

import argparse
import atexit
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import io
import json
import logging
from pathlib import Path
import secrets
import shutil
import threading
import time
import tempfile
import uuid
import webbrowser
import zipfile
import base64
import urllib.request
import urllib.error

from flask import Flask, jsonify, render_template, request, send_file, abort
from PIL import Image, ImageOps, UnidentifiedImageError
import numpy as np
import trimesh
from trimesh.ray.ray_pyembree import RayMeshIntersector
from werkzeug.exceptions import HTTPException
from werkzeug.serving import make_server

import generate as g

MAX_PIXELS = 20_000_000
PART_LABELS = {"base": "Base + light post", "wall_right": "Right wall", "wall_top": "Top wall",
               "wall_left": "Left wall", "wall_bottom": "Bottom wall", "hood_support": "Hood bracket",
               "light_hood": "Light hood"}


def parse_settings(values):
    if not isinstance(values, dict):
        raise ValueError("Settings must be a JSON object")
    defaults = asdict(g.Settings())
    unknown = set(values) - set(defaults)
    if unknown:
        raise ValueError("Unknown setting: " + sorted(unknown)[0])
    converted = dict(defaults)
    for name, value in values.items():
        if name == "light_height" and value is None:
            converted[name] = None
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not np.isfinite(value):
            raise ValueError(f"{name.replace('_', ' ')} must be a finite number")
        if name in ("threshold", "resolution"):
            if int(value) != value:
                raise ValueError(f"{name} must be a whole number")
            value = int(value)
        converted[name] = value
    settings = g.Settings(**converted)
    settings.validate()
    return settings


def image_bytes(file):
    """Decode uploads before use; normalize orientation and alpha without resizing."""
    try:
        with Image.open(file) as original:
            if original.width * original.height > MAX_PIXELS:
                raise ValueError("Choose an image with fewer than 20 million pixels")
            original.load()
            image = ImageOps.exif_transpose(original).convert("RGBA")
            output = io.BytesIO()
            image.save(output, format="PNG")
            return output.getvalue(), image.size
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError) as error:
        raise ValueError("This file is not a supported image. Choose a PNG, JPG, or WebP image.") from error


def trace_visibility(intersector, bounds, source, width=640, emitter_size=0.0, check_cancel=None):
    """Ray-test the actual assembled parts; a disk source approximates finite LEDs.

    All meshes lie on/above z=0 and the receiver is at/below z=0, so an infinite
    ray intersection has the same blocking result as a segment to the receiver.
    """
    x0, y0, x1, y1 = bounds
    height = max(1, round(width * (y1-y0)/(x1-x0)))
    if height > 1280:
        width = max(1, round(width * 1280 / height))
        height = 1280
    xs, ys = np.meshgrid(np.linspace(x0, x1, width), np.linspace(y1, y0, height))
    # The last element of source is receiving-wall z; first three are light xyz.
    targets = np.column_stack((xs.ravel(), ys.ravel(), np.full(xs.size, source[3])))
    light = np.array(source[:3], dtype=float)
    samples = [np.zeros(3)]
    if emitter_size > 0:
        samples = []
        for i in range(25):
            radius = emitter_size / 2 * np.sqrt((i+.5)/25)
            angle = i * np.pi * (3-np.sqrt(5))
            samples.append(np.array([radius*np.cos(angle), radius*np.sin(angle), 0]))
    visibility = np.zeros(len(targets), dtype=np.float32)
    for offset in samples:
        if check_cancel:
            check_cancel()
        origins = np.tile(light+offset, (len(targets), 1))
        visibility += ~intersector.intersects_any(origins, targets-origins)
    return (visibility / len(samples)).reshape(height, width)


def projection_pngs(visibility):
    mask = Image.fromarray(np.round(visibility*255).astype(np.uint8))
    dark, lit = np.array([22, 23, 22]), np.array([250, 216, 151])
    pixels = dark + (lit-dark) * visibility[:, :, None]
    color = Image.fromarray(np.round(pixels).astype(np.uint8))
    buffers = []
    for image in (mask, color):
        stream = io.BytesIO()
        image.save(stream, format="PNG")
        buffers.append(stream.getvalue())
    return buffers


class Cancelled(Exception):
    pass


class Studio:
    def __init__(self, root=None):
        self.root = Path(root) if root else Path(tempfile.mkdtemp(prefix="shadow-studio-"))
        self.root.mkdir(parents=True, exist_ok=True)
        self.owns_root = root is None
        self.jobs = {}
        self.lock = threading.RLock()
        self.pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="shadow-builder")
        self.closed = False

    def close(self):
        if self.closed:
            return
        self.closed = True
        with self.lock:
            for job in self.jobs.values():
                job["cancel"].set()
        self.pool.shutdown(wait=True, cancel_futures=True)
        # Only remove the private directory allocated by this instance.
        if self.owns_root and self.root.resolve().parent == Path(tempfile.gettempdir()).resolve() and self.root.name.startswith("shadow-studio-"):
            shutil.rmtree(self.root, ignore_errors=True)

    def get(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
        if job is None:
            abort(404, description="This preview has expired. Update the model again.")
        return job

    def status(self, job):
        with self.lock:
            return {key: job.get(key) for key in ("id", "state", "progress", "message", "error", "scene", "elapsed")}

    def update(self, job, **values):
        with self.lock:
            job.update(values)

    def submit(self, data, filename, settings):
        with self.lock:
            if sum(job["state"] in ("queued", "building") for job in self.jobs.values()) >= 3:
                raise ValueError("Please wait for the current previews, or cancel one before starting another")
            # Retain the last eight finished previews for browser history/downloads.
            finished = [job for job in self.jobs.values() if job["state"] in ("ready", "error", "cancelled")]
            for previous in finished[:-7]:
                folder = previous["folder"].resolve()
                if folder.parent == self.root.resolve():
                    shutil.rmtree(folder, ignore_errors=True)
                del self.jobs[previous["id"]]
            job_id = uuid.uuid4().hex
            folder = self.root / job_id
            folder.mkdir()
            (folder / "input.png").write_bytes(data)
            job = {"id": job_id, "folder": folder, "settings": settings, "filename": filename,
                   "state": "queued", "progress": 0, "message": "Waiting to build", "cancel": threading.Event(),
                   "simulation_lock": threading.Lock(), "scene": None, "error": None, "elapsed": None}
            self.jobs[job_id] = job
        self.pool.submit(self.build, job)
        return job

    def build(self, job):
        started = time.perf_counter()
        def check():
            if job["cancel"].is_set():
                raise Cancelled()
        try:
            check()
            s, folder = job["settings"], job["folder"]
            self.update(job, state="building", progress=12, message="Tracing the artwork")
            artwork, bounds = g.image_geometry(folder / "input.png", s)
            check()
            panels = g.build_panels(artwork, bounds, s)
            self.update(job, progress=35, message="Building walls, slots and light hood")
            parts = g.build_parts(panels, s)
            check()
            self.update(job, progress=62, message="Checking and preparing the seven print parts")
            part_report = g.export_parts(parts, s, folder)
            g.export_masks(panels, s, folder)
            assembled = trimesh.util.concatenate([g.to_trimesh(part.solid) for part in parts])
            assembled.export(folder / "shadow.stl")
            raytracer = RayMeshIntersector(assembled)
            self.update(job, progress=82, message="Simulating light through the assembled model")
            visibility = trace_visibility(raytracer, bounds.bounds, [0, 0, s.light_z, -s.wall_gap], check_cancel=check)
            mask, projection = projection_pngs(visibility)
            (folder / "visibility.png").write_bytes(mask)
            (folder / "projection.png").write_bytes(projection)
            check()
            settings_json = asdict(s)
            manifest = {"schema_version": 1, "input_name": job["filename"], "settings": settings_json,
                        "parts": part_report, "model_size_mm": assembled.extents.tolist(),
                        "light_position_mm": [0, 0, s.light_z], "receiving_wall_z_mm": -s.wall_gap}
            (folder / "project.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
            with zipfile.ZipFile(folder / "print_parts.zip", "a", zipfile.ZIP_DEFLATED) as archive:
                archive.write(folder / "project.json", "project.json")
            prefix = f"/api/jobs/{job['id']}/assets/"
            scene = {"parts": [{"name": name, "label": PART_LABELS[name], **metadata,
                                  "url": prefix+metadata["file"]} for name, metadata in part_report.items()],
                     "settings": settings_json, "input_name": job["filename"], "image_url": prefix+"input.png",
                     "mask_url": prefix+"visibility.png", "projection_url": prefix+"projection.png",
                     "model_size": assembled.extents.tolist(), "projection_bounds": list(bounds.bounds),
                     "light_position": [0, 0, s.light_z], "triangles": len(assembled.faces),
                     "volume_cm3": round(assembled.volume/1000, 1), "watertight": all(p["watertight"] for p in part_report.values()),
                     "bridges": sum(panel.bridges for panel in panels), "download_url": f"/api/jobs/{job['id']}/download"}
            self.update(job, state="ready", progress=100, message="Model ready", scene=scene,
                        raytracer=raytracer, bounds=bounds.bounds, elapsed=round(time.perf_counter()-started, 2))
        except Cancelled:
            self.update(job, state="cancelled", message="Build cancelled", progress=0)
        except Exception as error:
            logging.exception("Preview generation failed")
            self.update(job, state="error", error=str(error), message="Could not build this model")


def create_app(root=None):
    app = Flask(__name__, static_folder=str(g.ROOT/"web"), static_url_path="/static", template_folder=str(g.ROOT/"web"))
    app.config["MAX_CONTENT_LENGTH"] = 25 * 1024 * 1024
    app.config["TOKEN"] = secrets.token_urlsafe(32)
    studio = Studio(root)
    app.extensions["studio"] = studio

    @app.before_request
    def local_requests():
        host = request.host.split(":")[0]
        if host not in ("127.0.0.1", "localhost"):
            abort(403, description="Shadow Studio accepts local connections only")
        if request.method == "POST" and not secrets.compare_digest(request.headers.get("X-Studio-Token", ""), app.config["TOKEN"]):
            abort(403, description="Reload Shadow Studio before making changes")

    @app.after_request
    def headers(response):
        # Windows registry MIME mappings sometimes label .js as text/plain.
        # Module scripts require a JavaScript MIME type, especially with nosniff.
        if request.path.startswith('/static/') and request.path.endswith('.js'):
            response.mimetype = 'text/javascript'
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "same-origin"
        if request.path.startswith("/api/") or request.path == "/":
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.errorhandler(Exception)
    def errors(error):
        if isinstance(error, HTTPException):
            description = "Image is too large. Choose a file under 25 MB." if error.code == 413 else error.description
            return jsonify(error=description), error.code
        if isinstance(error, (ValueError, json.JSONDecodeError)):
            return jsonify(error=str(error)), 400
        logging.exception("Studio request failed")
        return jsonify(error="Something went wrong. Check the app terminal for details."), 500

    @app.get("/")
    def index():
        return render_template("index.html", token=app.config["TOKEN"])

    @app.get("/api/health")
    def health():
        return jsonify(app="shadow-studio", version=1)

    @app.get("/api/defaults")
    def defaults():
        return jsonify(settings=asdict(g.Settings()), sample_url="/api/sample", sample_name="fish.jpg")

    @app.get("/api/sample")
    def sample():
        return send_file(g.ROOT/"fish.jpg")

    @app.post("/api/jobs")
    def submit():
        settings = parse_settings(json.loads(request.form.get("settings", "{}")))
        upload = request.files.get("image")
        if upload:
            data, _ = image_bytes(upload.stream)
            filename = (upload.filename or "artwork").replace("\\", "/").split("/")[-1][:120]
        else:
            data, _ = image_bytes(g.ROOT/"fish.jpg")
            filename = "fish.jpg"
        job = studio.submit(data, filename, settings)
        return jsonify(studio.status(job)), 202

    @app.get("/api/jobs/<job_id>")
    def status(job_id):
        return jsonify(studio.status(studio.get(job_id)))

    @app.post("/api/jobs/<job_id>/cancel")
    def cancel(job_id):
        job = studio.get(job_id)
        job["cancel"].set()
        return jsonify(cancelled=True)

    @app.get("/api/jobs/<job_id>/assets/<filename>")
    def asset(job_id, filename):
        job = studio.get(job_id)
        allowed = {*(name+".stl" for name in PART_LABELS), "input.png", "visibility.png", "projection.png", "project.json", "assembly_guide.txt"}
        if job["state"] != "ready" or filename not in allowed:
            abort(404)
        return send_file(job["folder"]/filename)

    @app.get("/api/jobs/<job_id>/download")
    def download(job_id):
        job = studio.get(job_id)
        if job["state"] != "ready":
            abort(409, description="Wait for the preview to finish before saving")
        stem = "".join(c for c in Path(job["filename"]).stem if c.isalnum() or c in "-_")[:50] or "projector"
        return send_file(job["folder"]/"print_parts.zip", as_attachment=True, download_name=f"{stem}-print-parts.zip")

    @app.post("/api/jobs/<job_id>/simulate")
    def simulate(job_id):
        job = studio.get(job_id)
        if job["state"] != "ready":
            abort(409, description="Build a model before simulating the light")
        body = request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            raise ValueError("Simulation options must be a JSON object")
        size = body.get("emitter_size", 0)
        if isinstance(size, bool) or not isinstance(size, (int, float)) or not np.isfinite(size) or not 0 <= size <= 5:
            raise ValueError("Light-emitting diameter must be between 0 and 5 mm")
        s = job["settings"]
        with job["simulation_lock"]:
            visibility = trace_visibility(job["raytracer"], job["bounds"], [0, 0, s.light_z, -s.wall_gap], emitter_size=size)
        mask, projection = projection_pngs(visibility)
        uri = lambda value: "data:image/png;base64," + base64.b64encode(value).decode("ascii")
        return jsonify(mask=uri(mask), projection=uri(projection), emitter_size=size)

    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()
    address = f"http://127.0.0.1:{args.port}"
    if args.port:
        try:
            with urllib.request.urlopen(address + '/api/health', timeout=1) as response:
                running = json.load(response).get('app') == 'shadow-studio'
            if running:
                print(f"Shadow Studio is already running: {address}")
                if not args.no_browser:
                    webbrowser.open(address)
                return
        except (OSError, ValueError, urllib.error.URLError):
            pass
    app = create_app()
    atexit.register(app.extensions["studio"].close)
    server = make_server("127.0.0.1", args.port, app, threaded=True)
    address = f"http://127.0.0.1:{server.server_port}"
    print(f"\nShadow Studio is ready: {address}\nKeep this window open while using the app. Press Ctrl+C to stop.\n", flush=True)
    if not args.no_browser:
        webbrowser.open(address)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        app.extensions["studio"].close()


if __name__ == "__main__":
    main()
