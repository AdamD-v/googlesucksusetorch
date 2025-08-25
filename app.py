import os
import glob
import datetime
from flask import Flask, request, send_file, jsonify
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

VIDEO_DIR = "videos"
os.makedirs(VIDEO_DIR, exist_ok=True)

def _now_iso():
    return datetime.datetime.utcnow().isoformat() + "Z"

def _session_path(session_id: str, ext: str = ".webm"):
    return os.path.join(VIDEO_DIR, f"{session_id}{ext}")

@app.route("/")
def index():
    # Simple HTML capture page
    return """
<!DOCTYPE html>
<html>
<head>
  <title>Recorder</title>
</head>
<body>
  <h1>Screen Recorder (10fps)</h1>
  <button onclick="startRecording()">Start Recording</button>
  <button onclick="stopRecording()">Stop Recording</button>
  <script>
  let mediaStream;
  let recorder;
  let sessionId = Math.random().toString(36).slice(2);

  async function startRecording() {
    mediaStream = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false });
    recorder = new MediaRecorder(mediaStream, { mimeType: "video/webm;codecs=vp8", videoBitsPerSecond: 5000000 });
    recorder.ondataavailable = e => {
      if (e.data.size > 0) {
        uploadChunk(e.data);
      }
    };
    recorder.start(100); // capture chunks

    // snapshot loop
    setInterval(captureStill, 1000);
  }

  async function uploadChunk(blob) {
    await fetch("/upload/" + sessionId, { method: "POST", body: blob });
  }

  function stopRecording() {
    recorder.stop();
    fetch("/finalize/" + sessionId, { method: "POST" });
  }

  async function captureStill() {
    if (!mediaStream) return;
    const track = mediaStream.getVideoTracks()[0];
    if (!track) return;
    const imageCapture = new ImageCapture(track);
    try {
      const bitmap = await imageCapture.grabFrame();
      const canvas = document.createElement("canvas");
      canvas.width = bitmap.width;
      canvas.height = bitmap.height;
      const ctx = canvas.getContext("2d");
      ctx.drawImage(bitmap, 0, 0);
      canvas.toBlob(async (blob) => {
        if (blob) {
          await fetch("/snapshot/" + sessionId, { method: "POST", body: blob });
        }
      }, "image/jpeg", 0.9);
    } catch (err) {
      console.error("Snapshot error", err);
    }
  }
  </script>
</body>
</html>
    """

@app.post("/upload/<session_id>")
def upload_chunk(session_id: str):
    chunk = request.get_data()
    path = _session_path(session_id)
    with open(path, "ab") as f:
        f.write(chunk)
    return {"ok": True, "size": len(chunk)}

@app.post("/finalize/<session_id>")
def finalize(session_id: str):
    return {"ok": True, "final": _session_path(session_id)}

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
    files = []
    for f in glob.glob(os.path.join(VIDEO_DIR, "*")):
        files.append({
            "filename": os.path.basename(f),
            "bytes": os.path.getsize(f),
            "modified": datetime.datetime.utcfromtimestamp(os.path.getmtime(f)).isoformat() + "Z"
        })
    return {"ok": True, "server_time": _now_iso(), "videos": files}

@app.get("/latest")
def latest():
    vids = sorted(glob.glob(os.path.join(VIDEO_DIR, "*.webm")), key=os.path.getmtime, reverse=True)
    if not vids:
        return jsonify({"ok": False, "error": "no video"}), 404
    return send_file(vids[0], mimetype="video/webm")

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=True)
