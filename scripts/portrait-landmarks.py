"""The built-in portraits' face landmarks: web/face/portraits/<id>.json, found by the app's own code (web/face/photo.js,
MediaPipe's face landmarker) in headless Chrome, as the panel would find them.

    .venv\\Scripts\\python scripts\\portrait-landmarks.py            every portrait in portraits.json
    .venv\\Scripts\\python scripts\\portrait-landmarks.py sarah ruth   just these

With them the backend puts a portrait on the face by itself (a fresh face wears the default one before anyone opens
the panel), and the panel needs no face finder to choose one. Needs Chrome and what scripts\\setup.ps1 installs
(MediaPipe in deps\\shell, the face landmarker in the models folder)."""
from __future__ import annotations

import http.server
import json
import os
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from wireface import config  # noqa: E402

PORTRAITS = config.WEB / "face" / "portraits"
VENDOR = config.SHELL_TOOLS / "node_modules" / "@mediapipe" / "tasks-vision"
CHROME = next((p for p in (os.environ.get("CHROME", ""), r"C:\Program Files\Google\Chrome\Application\chrome.exe",
                           r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe") if p and Path(p).exists()), None)

PAGE = """<!doctype html><meta charset="utf-8"><script type="module">
import { preparePhoto } from '/photo.js';
// a local server on Windows now and then drops a connection: try again
const go = async (url, opts) => { for (let i = 0; ; i++) { try { return await fetch(url, opts); } catch (e) { if (i > 3) throw e; } } };
for (const id of %s) {
  let body;
  try {
    const blob = await (await go('/portrait/' + id + '.jpg')).blob();
    body = JSON.stringify({ id, landmarks: (await preparePhoto(blob)).landmarks });
  } catch (e) { body = JSON.stringify({ id, error: String(e.message || e) }); }
  await go('/result', { method: 'POST', body });
}
await go('/done', { method: 'POST' });
</script>"""


def main() -> None:
    listed = [p["id"] for p in json.loads((PORTRAITS / "portraits.json").read_text(encoding="utf-8"))]
    ids = sys.argv[1:] or listed
    if unknown := [i for i in ids if i not in listed]:
        sys.exit(f"not in portraits.json: {', '.join(unknown)}")
    for need in (CHROME, VENDOR / "vision_bundle.mjs", config.LANDMARKER):
        if not need or not Path(need).exists():
            sys.exit(f"missing: {need or 'Chrome'} (run scripts\\setup.ps1)")
    finished = threading.Event()
    failed = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def send(self, body: bytes, ctype: str):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            p = self.path.split("?")[0]
            if p == "/":
                return self.send((PAGE % json.dumps(ids)).encode(), "text/html")
            if p == "/photo.js":
                return self.send((config.WEB / "face" / "photo.js").read_bytes(), "text/javascript")
            if p.startswith("/portrait/") and p[10:-4] in ids:
                return self.send((PORTRAITS / p[10:]).read_bytes(), "image/jpeg")
            if p == "/face-vendor/face_landmarker.task":
                return self.send(config.LANDMARKER.read_bytes(), "application/octet-stream")
            f = VENDOR / p[len("/face-vendor/"):] if p.startswith("/face-vendor/") else None
            if f and f.resolve().is_relative_to(VENDOR.resolve()) and f.is_file():
                ctype = {".mjs": "text/javascript", ".js": "text/javascript", ".wasm": "application/wasm"}.get(
                    f.suffix, "application/octet-stream")
                return self.send(f.read_bytes(), ctype)
            self.send_error(404)

        def do_POST(self):
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if self.path == "/result":
                r = json.loads(body)
                if "error" in r:
                    failed.append(r["id"])
                    print(f"{r['id']}: {r['error']}")
                else:
                    pts = [[round(x, 5), round(y, 5)] for x, y in r["landmarks"]]
                    (PORTRAITS / f"{r['id']}.json").write_text(json.dumps(pts, separators=(",", ":")), encoding="utf-8")
                    print(f"{r['id']}: {len(pts)} landmarks")
            elif self.path == "/done":
                finished.set()
            self.send(b"{}", "application/json")

    class Server(http.server.ThreadingHTTPServer):
        request_queue_size = 64                     # Chrome opens several connections at once
        daemon_threads = True

        def handle_error(self, request, client_address):
            pass                                    # connections Chrome opened and dropped unused

    server = Server(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as profile:
        chrome = subprocess.Popen([CHROME, "--headless=new", "--disable-gpu", "--no-first-run", f"--user-data-dir={profile}", url],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = finished.wait(180)
        chrome.kill()
        chrome.wait()
    server.shutdown()
    if not ok or failed:
        sys.exit(f"landmarks missing for: {', '.join(failed) or 'all (timed out)'}")


if __name__ == "__main__":
    main()
