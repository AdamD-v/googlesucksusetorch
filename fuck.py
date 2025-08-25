"""
app.py — Single‑file Flask app that captures the visitor's screen/tab at 10 FPS in the browser,
uploads video chunks + periodic snapshots to the server, and exposes CORS‑enabled endpoints
that Roblox HttpService can call.

Quick start:
  1) python -m venv .venv && source .venv/bin/activate  (Windows: .venv\Scripts\activate)
  2) pip install -r requirements.txt
  3) python app.py
  4) Open https://<your-domain>/  (needs HTTPS for screen capture; localhost usually works in Chrome)

requirements.txt:
--------------------------------------------------
Flask==3.0.3
Flask-Cors==4.0.1
"""
from __future__ import annotations

import os
import uuid
import time
import glob
import shutil
import subprocess
from datetime import datetime
from typing import List, Dict

from flask import Flask, request, jsonify, send_file, Response, make_response
from flask_cors import CORS

APP_TITLE = "10‑FPS Screen Recorder"
BASE_DIR = os.path.abspath(os.path.dirname(__file__))
VIDEO_DIR = os.path.join(BASE_DIR, "videos")
PART_EXT = ".partial"

app = Flask(__name__)
CORS(app, resources={r"/*": {"origins": "*"}})

os.makedirs(VIDEO_DIR, exist_ok=True)

# ---------- Helpers ----------

def _now_iso() -> str:
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")


def _session_path(session_id: str, ext: str = ".webm") -> str:
    return os.path.join(VIDEO_DIR, f"{session_id}{ext}")


def _list_videos() -> List[Dict]:
    files = []
    for pattern, kind in [("*.mp4", "mp4"), ("*.webm", "webm")]:
        for f in glob.glob(os.path.join(VIDEO_DIR, pattern)):
            stat = os.stat(f)
            files.append({
                "filename": os.path.basename(f),
                "bytes": stat.st_size,
                "modified": datetime.utcfromtimestamp(stat.st_mtime).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "url": f"/video/{os.path.basename(f)}",
                "type": kind,
            })
    files.sort(key=lambda x: x["modified"], reverse=True)
    return files


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        return True
    except Exception:
        return False


def _finalize_to_mp4(webm_path: str) -> str | None:
    mp4_path = os.path.splitext(webm_path)[0] + ".mp4"
    if not _ffmpeg_available():
        return None
    cmd = [
        "ffmpeg", "-y",
        "-i", webm_path,
        "-r", "10",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-movflags", "+faststart",
        mp4_path,
    ]
    try:
        subprocess.run(cmd, check=True)
        return mp4_path if os.path.exists(mp4_path) else None
    except Exception:
        return None


# ---------- Routes ----------

@app.get("/")
def index() -> Response:
    html = f"""
<!doctype html>
<html>
<head>
  <meta charset=\"utf-8\" />
  <meta name=\"viewport\" content=\"width=device-width, initial-scale=1\" />
  <title>{APP_TITLE}</title>
  <style>
    body {{ font-family: system-ui, sans-serif; margin: 2rem; }}
    .wrap {{ max-width: 760px; margin: auto; }}
    .card {{ border: 1px solid #e5e7eb; border-radius: 16px; padding: 16px; box-shadow: 0 3px 14px rgba(0,0,0,.06); }}
    .row {{ display: flex; gap: .75rem; align-items: center; flex-wrap: wrap; }}
    button {{ padding: .6rem 1rem; border-radius: 999px; border: 1px solid #dadde1; background: white; cursor: pointer; }}
    button.primary {{ background: #111827; color: white; border-color: #111827; }}
    code {{ background: #f3f4f6; padding: .15rem .4rem; border-radius: 6px; }}
    #videoEl {{ width: 100%; max-height: 300px; background: #000; border-radius: 12px; }}
    .muted {{ color: #6b7280; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>{APP_TITLE}</h1>
    <p class="muted">This page will ask permission to capture your current tab/screen. It records at ~10 FPS, uploads video chunks, and also sends periodic image snapshots for Roblox to use.</p>

    <div class="card" style="margin-top:1rem;">
      <div class="row" style="justify-content: space-between;">
        <div>
          <strong>Status:</strong> <span id="status">idle</span><br/>
          <small>Session: <code id="sid">n/a</code></small>
        </div>
        <div class="row">
          <button id="startBtn" class="primary">Start 10‑FPS Capture</button>
          <button id="stopBtn">Stop</button>
        </div>
      </div>
      <video id="videoEl" autoplay muted playsinline></video>
      <p class="muted">Tip: Use Chrome on localhost or serve over HTTPS for permission to capture.</p>
    </div>

    <div class="card" style="margin-top:1rem;">
      <h3>Latest videos</h3>
      <ul id="list"></ul>
      <button id="refreshBtn">Refresh list</button>
    </div>
  </div>

<script>
const el = (id) => document.getElementById(id);
let mediaStream = null;
let recorder = null;
let sessionId = null;
let chunkSeq = 0;
let snapshotTimer = null;

async function refreshList() {{
  const res = await fetch('/status');
  const json = await res.json();
  const list = el('list');
  list.innerHTML = '';
  for (const f of json.videos) {{
    const li = document.createElement('li');
    const a = document.createElement('a');
    a.href = f.url;
    a.textContent = `${{f.filename}} ({{Math.round(f.bytes/1024)}} KB, {{f.modified}})`;
    a.target = '_blank';
    li.appendChild(a);
    list.appendChild(li);
  }}
}}

async function startCapture() {{
  try {{
    sessionId = crypto.randomUUID();
    el('sid').textContent = sessionId;
    el('status').textContent = 'requesting permission…';

    mediaStream = await navigator.mediaDevices.getDisplayMedia({{
      video: {{ frameRate: 10, width: {{ ideal: 1920 }}, height: {{ ideal: 1080 }}, displaySurface: 'browser' }},
      audio: false
    }});

    el('status').textContent = 'recording…';

    const videoEl = el('videoEl');
    videoEl.srcObject = mediaStream;

    const options = {{ mimeType: 'video/webm;codecs=vp9', bitsPerSecond: 5_000_000 }};
    recorder = new MediaRecorder(mediaStream, options);

    chunkSeq = 0;
    recorder.ondataavailable = async (evt) => {{
      if (!evt.data || evt.data.size === 0) return;
      try {{
        await fetch(`/upload/${{sessionId}}?seq=${{chunkSeq++}}`, {{
          method: 'POST',
          headers: {{ 'X-Chunk-Seq': String(chunkSeq) }},
          body: evt.data,
        }});
      }} catch (e) {{ console.error('upload error', e); }}
    }};

    recorder.onstop = async () => {{
      try {{
        await fetch(`/finalize/${{sessionId}}`, {{ method: 'POST' }});
        el('status').textContent = 'finalized';
        await refreshList();
      }} catch (e) {{ console.error(e); }}
    }};

    recorder.start(1000);

    // snapshot loop every second
    snapshotTimer = setInterval(captureStill, 1000);

  }} catch (err) {{
    console.error(err);
    el('status').textContent = 'error: ' + (err && err.message ? err.message : err);
  }}
}}

function stopCapture() {{
  if (recorder && recorder.state !== 'inactive') {{
    recorder.stop();
  }}
  if (mediaStream) {{
    mediaStream.getTracks().forEach(t => t.stop());
    mediaStream = null;
  }}
  if (snapshotTimer) clearInterval(snapshotTimer);
  el('status').textContent = 'stopped';
}}

async function captureStill() {{
  if (!mediaStream) return;
  const track = mediaStream.getVideoTracks()[0];
  const imageCapture = new ImageCapture(track);
  try {{
    const bitmap = await imageCapture.grabFrame();
    const canvas = document.createElement('canvas');
    canvas.width = bitmap.width;
    canvas.height = bitmap.height;
    const ctx = canvas.getContext('2d');
    ctx.drawImage(bitmap, 0, 0);
    canvas.toBlob(async (blob) => {{
      await fetch(`/snapshot/${{sessionId}}`, {{
        method: 'POST',
        body: blob
      }});
    }}, 'image/jpeg', 0.85);
  }} catch (e) {{ console.error('snapshot error', e); }}
}}

el('startBtn').addEventListener('click', startCapture);
el('stopBtn').addEventListener('click', stopCapture);
el('refreshBtn').addEventListener('click', refreshList);

window.addEventListener('beforeunload', () => {{
  if (sessionId) navigator.sendBeacon(`/finalize/${{sessionId}}`);
}});

refreshList();
</script>
</body>
</html>
    """
    return make_response(html)


@app.post("/upload/<session_id>")
def upload_chunk(session_id: str):
    partial_path = _session_path(session_id, ext=f".webm{PART_EXT}")
    os.makedirs(VIDEO_DIR, exist_ok=True)
    with open(partial_path, "ab") as f:
        f.write(request.get_data())
    resp = make_response("OK", 200)
    resp.headers["X-Received-Bytes"] = str(os.path.getsize(partial_path))
    resp.headers["Access-Control-Expose-Headers"] = "X-Received-Bytes"
    return resp


@app.post("/finalize/<session_id>")
def finalize(session_id: str):
    partial_path = _session_path(session_id, ext=f".webm{PART_EXT}")
    webm_path = _session_path(session_id, ext=f".webm")
    if not os.path.exists(partial_path) and not os.path.exists(webm_path):
        return jsonify({"ok": False, "error": "no recording"}), 404
    if os.path.exists(webm_path):
        out = {"ok": True, "webm": os.path.basename(webm_path)}
        mp4_path = os.path.splitext(webm_path)[0] + ".mp4"
        if os.path.exists(mp4_path):
            out["mp4"] = os.path.basename(mp4_path)
        return jsonify(out)
    if os.path.exists(partial_path):
        shutil.move(partial_path, webm_path)
    mp4_path = _finalize_to_mp4(webm_path)
    return jsonify({
        "ok": True,
        "webm": os.path.basename(webm_path) if os.path.exists(webm_path) else None,
        "mp4": os.path.basename(mp4_path) if mp4_path and os.path.exists(mp4_path) else None,
        "at": _now_iso(),
    })


@app.post("/snapshot/<session_id>")
def snapshot(session_id: str):
    snap_path = _session_path(session_id, ext=".jpg")
    with open(snap_path, "wb") as f:
        f.write(request.get_data())
    return {"ok": True, "at": _now_iso(), "file": os.path.basename(snap_path)}


@app.get("/snapshot/latest")
def snapshot_latest():
    snaps = sorted(glob.glob(os.path.join(VIDEO_DIR, "*.jpg")), key=os.path.getmtime, reverse=True)
    if not snaps:
        return jsonify({"ok": False, "error": "no snapshot"}), 404
    return send_file(snaps[0], mimetype="image/jpeg")


@app.get("/status")
def status():
    return jsonify({
        "ok": True,
        "videos": _list_videos(),
        "server_time": _now_iso(),
    })


@app.get("/latest")
def latest():
    vids = _list_videos()
    if not vids:
        return jsonify({"ok": False, "error": "no videos yet"}), 404
    preferred = None
    for v in vids:
        if v["filename"].endswith(".mp4"):
            preferred = v
            break
    if preferred is None:
        preferred = vids[0]
    return send_file(os.path.join(VIDEO_DIR, preferred["filename"]), as_attachment=False)


@app.get("/video/<path:filename>")
def serve_video(filename: str):
    fp = os.path.join(VIDEO_DIR, filename)
    if not os.path.exists(fp):
        return jsonify({"ok": False, "error": "not found"}), 404
    resp = send_file(fp, as_attachment=False)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['Cache-Control'] = 'no-store'
    return resp


@app.get("/health")
def health():
    return jsonify({"ok": True, "time": _now_iso()})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
