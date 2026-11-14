"""The server image is built from this folder with .dockerignore applied. Every file the server or update.sh reads FROM the image
must survive that filter. (A broker settings file once missed the image and update.sh silently could not refresh it.)"""
import fnmatch, os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NEEDED_IN_IMAGE = ["web/index.html", "mosquitto/acl", "mosquitto/mosquitto.conf", "scripts/update.sh", "plant_gateway/plant_gateway.py", "plant_gateway/launcher.py"]


def _excluded(path):
    verdict = False
    for line in open(os.path.join(ROOT, ".dockerignore"), encoding="utf-8").read().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        neg = line.startswith("!")
        pat = line[1:] if neg else line
        if fnmatch.fnmatch(path, pat) or fnmatch.fnmatch(os.path.basename(path), pat):
            verdict = not neg
    return verdict


def test_files_read_from_the_image_are_not_filtered_out():
    for f in NEEDED_IN_IMAGE:
        assert os.path.isfile(os.path.join(ROOT, f)), f"{f} is missing from the folder"
        assert not _excluded(f), f"{f} is removed by .dockerignore, so it is not in the server image"


def test_update_script_reads_only_what_the_image_has():
    s = open(os.path.join(ROOT, "scripts", "update.sh"), encoding="utf-8").read()
    for f in ("acl", "mosquitto.conf"):
        assert f"/app/mosquitto/$F" in s and not _excluded(f"mosquitto/{f}")


def test_update_script_copies_the_web_version_from_the_image():
    s = open(os.path.join(ROOT, "scripts", "update.sh"), encoding="utf-8").read()
    assert "/app/web" in s and "main.dart.js" in s            # only a real Flutter web build replaces the server's web folder
    assert not _excluded("web/main.dart.js") and not _excluded("web/assets/AssetManifest.json") and not _excluded("web/canvaskit/canvaskit.wasm")


def test_ingest_worker_count_follows_memory(monkeypatch):
    from app.workers import mqtt_ingest as m
    monkeypatch.delenv("INGEST_WORKERS", raising=False)
    monkeypatch.setattr(m.os, "cpu_count", lambda: 8)
    for mb, expect in ((836, 1), (2048, 3), (4096, 6), (16384, 16), (64000, 16)):
        monkeypatch.setattr(m, "_memory_mb", lambda mb=mb: mb)
        assert m.worker_count() == expect, (mb, m.worker_count())
    monkeypatch.setattr(m, "_memory_mb", lambda: 836)
    monkeypatch.setenv("INGEST_WORKERS", "4")
    assert m.worker_count() == 4          # an explicit setting always wins
    assert m._memory_mb() > 0
