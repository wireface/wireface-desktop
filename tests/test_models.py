import http.server
import io
import tarfile
import threading

import pytest

from wireface import config, models


@pytest.fixture
def folders(tmp_path, monkeypatch):
    """Point every model path at an empty temp folder."""
    speech = tmp_path / "speech"
    for name, value in {
        "SPEECH_DIR": speech, "VAD_MODEL": speech / "silero_vad.onnx", "DENOISER_MODEL": speech / "gtcrn_simple.onnx",
        "OFFLINE_ASR_DIR": speech / "asr", "POCKET_DIR": speech / "pocket", "KWS_DIR": speech / "kws",
        "KOKORO_DIR": speech / "kokoro", "LANDMARKER": tmp_path / "mediapipe" / "face_landmarker.task",
    }.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(config, "TTS_ENGINE", "pocket")
    return tmp_path


def make_archive(files: dict[str, bytes], top: str = "top") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def server():
    """A tiny HTTP server with Range support: serve.files maps a path to bytes."""
    files: dict[str, bytes] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            data = files.get(self.path)
            if data is None:
                self.send_error(404)
                return
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            body = data[start:]
            self.send_response(206 if rng else 200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    yield base, files
    httpd.shutdown()


def test_status_reports_everything_missing_in_an_empty_folder(folders):
    rows = {r["key"]: r for r in models.status()}
    assert not any(r["present"] for r in rows.values())
    assert {"vad", "denoiser", "asr", "pocket", "lexicon", "kokoro", "landmarker"} == set(rows)


def test_missing_message_names_what_to_install(folders):
    msg = models.missing_message("listening")
    assert "Silero" in msg and "Parakeet" in msg and "setup.ps1" in msg
    assert "Kokoro" not in (models.missing_message("voice") or "")      # not the configured engine


def test_missing_message_is_none_once_installed(folders):
    config.VAD_MODEL.parent.mkdir(parents=True)
    config.VAD_MODEL.write_bytes(b"x")
    config.OFFLINE_ASR_DIR.mkdir()
    for f in ("encoder.int8.onnx", "decoder.int8.onnx", "joiner.int8.onnx", "tokens.txt"):
        (config.OFFLINE_ASR_DIR / f).write_bytes(b"x")
    assert models.missing_message("listening") is None


def test_wanted_skips_kokoro_unless_asked(folders, monkeypatch):
    assert "kokoro" not in {m.key for m in models.wanted()}
    assert "kokoro" in {m.key for m in models.wanted(with_kokoro=True)}
    monkeypatch.setattr(config, "TTS_ENGINE", "kokoro")
    assert "kokoro" in {m.key for m in models.wanted()}


def test_install_unpacks_an_archive_into_place(folders, server):
    base, files = server
    files["/lex.tar.bz2"] = make_archive({"en.phone": b"hello HH AH0 L OW1\n", "other.txt": b"."})
    m = models.Model("lexicon", "Lexicon", f"{base}/lex.tar.bz2", config.KWS_DIR, ("en.phone",), 1, ("voice",))
    seen = []
    models.install_model(m, lambda key, done, total, phase: seen.append(phase))
    assert (config.KWS_DIR / "en.phone").read_text().startswith("hello")
    assert m.present and "download" in seen and "unpack" in seen
    assert not list(config.KWS_DIR.parent.glob(".*"))                   # no temp files left behind


def test_install_single_file(folders, server):
    base, files = server
    files["/vad.onnx"] = b"\x00" * 1000
    m = models.Model("vad", "VAD", f"{base}/vad.onnx", config.VAD_MODEL, (), 1, ("listening",), archive=False)
    models.install_model(m)
    assert config.VAD_MODEL.read_bytes() == b"\x00" * 1000


def test_download_resumes_a_partial_file(folders, server):
    base, files = server
    data = bytes(range(256)) * 400
    files["/big.bin"] = data
    target = folders / "big.bin"
    target.with_name("big.bin.part").write_bytes(data[:30000])
    models.download(f"{base}/big.bin", target)
    assert target.read_bytes() == data


def test_install_fails_when_the_archive_lacks_expected_files(folders, server):
    base, files = server
    files["/bad.tar.bz2"] = make_archive({"unrelated.txt": b"x"})
    m = models.Model("lexicon", "Lexicon", f"{base}/bad.tar.bz2", config.KWS_DIR, ("en.phone",), 1, ("voice",))
    with pytest.raises(RuntimeError, match="en.phone"):
        models.install_model(m)


def test_install_skips_models_already_present(folders, monkeypatch):
    config.VAD_MODEL.parent.mkdir(parents=True)
    config.VAD_MODEL.write_bytes(b"x")
    called = []
    monkeypatch.setattr(models, "install_model", lambda m, progress=None: called.append(m.key))
    models.install(["vad"])
    assert called == []


def test_safe_extract_rejects_paths_outside_the_target(tmp_path):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:bz2") as tar:
        info = tarfile.TarInfo("../evil.txt")
        info.size = 1
        tar.addfile(info, io.BytesIO(b"x"))
    archive = tmp_path / "evil.tar.bz2"
    archive.write_bytes(buf.getvalue())
    out = tmp_path / "out"
    out.mkdir()
    with pytest.raises(RuntimeError, match="unsafe"):
        models.safe_extract(archive, out)
    assert not (tmp_path / "evil.txt").exists()
