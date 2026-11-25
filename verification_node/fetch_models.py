"""Download the node's four ONNX models and check each one against models.lock (SHA-256).

    python fetch_models.py                    # models folder next to this script (see below)
    python fetch_models.py --models-dir DIR   # an explicit folder that holds models.lock
    python fetch_models.py --force            # download again even if a file is present and correct

A file is put in place only if its SHA-256 equals the one in models.lock. A file that does not match is deleted,
never used. The same script works on Windows and Linux and needs nothing but the Python standard library.
In this repository it is scripts/fetch_models.py; inside the main Komute app it is verification_node/fetch_models.py.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

# The public source of each locked file. The SHA-256 in models.lock is the authority, not the URL.
SOURCES = {
    "face_detection_yunet_2023mar.onnx": {
        "url": "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"},
    "yolo-v9-t-384-license-plates-end2end.onnx": {
        "url": "https://github.com/ankandrew/open-image-models/releases/download/assets/yolo-v9-t-384-license-plates-end2end.onnx"},
    "minifasnet_v2.onnx": {  # liveness: MiniFASNetV2 from minivision-ai/Silent-Face-Anti-Spoofing (Apache-2.0), ONNX by UniFace
        "url": "https://huggingface.co/yakhyo/uniface-weights/resolve/main/minifasnet_v2.onnx"},
    "cct_xs_v2_global.onnx": {
        "url": "https://github.com/ankandrew/fast-plate-ocr/releases/download/arg-plates/cct_xs_v2_global.onnx"},
    "face_recognition_sface_2021dec.onnx": {
        "url": "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx"},
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def download(url: str, dest: Path) -> None:
    last: Exception | None = None
    for attempt in range(1, 4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "komute-fetch-models"})
            with urllib.request.urlopen(req, timeout=120) as r, open(dest, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except Exception as e:  # network hiccup: retry, then fail loudly
            last = e
            time.sleep(2 * attempt)
    raise RuntimeError(f"download failed: {url} ({last})")


def default_models_dir() -> Path:
    here = Path(__file__).resolve().parent
    for candidate in (here / "backend" / "models", here.parent / "backend" / "models"):
        if (candidate / "models.lock").exists():
            return candidate
    raise SystemExit("models.lock not found next to this script; pass --models-dir")


def fetch(models_dir: Path, force: bool) -> int:
    lock = json.loads((models_dir / "models.lock").read_text(encoding="utf-8"))
    failed = 0
    for name, want in lock.items():
        want = want.lower()
        dest = models_dir / name
        if not force and dest.exists() and sha256(dest) == want:
            print(f"ok    {name} (present, hash matches)")
            continue
        src = SOURCES.get(name)
        if src is None:
            print(f"FAIL  {name} is in models.lock but has no download source; copy the file to {models_dir}")
            failed += 1
            continue
        print(f"..    downloading {name}", flush=True)
        part = dest.with_name(dest.name + ".part")
        with tempfile.TemporaryDirectory() as tmp:
            try:
                if "zip_entry" in src:
                    archive = Path(tmp) / "archive.zip"
                    download(src["url"], archive)
                    with zipfile.ZipFile(archive) as z:
                        entry = next((n for n in z.namelist() if n.split("/")[-1] == src["zip_entry"]), None)
                        if entry is None:
                            raise RuntimeError(f"{src['zip_entry']} is not inside {src['url']}")
                        with z.open(entry) as s, open(part, "wb") as d:
                            shutil.copyfileobj(s, d)
                else:
                    download(src["url"], part)
                got = sha256(part)
                if got != want:
                    raise RuntimeError(f"hash mismatch: expected {want}, got {got}; the file was discarded")
                os.replace(part, dest)
                print(f"ok    {name} (downloaded, hash matches)")
            except Exception as e:
                print(f"FAIL  {name}: {e}")
                failed += 1
            finally:
                part.unlink(missing_ok=True)
    return failed


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--models-dir", type=Path, default=None)
    p.add_argument("--force", action="store_true")
    a = p.parse_args()
    models_dir = (a.models_dir or default_models_dir()).resolve()
    failed = fetch(models_dir, a.force)
    print("all models ready" if not failed else f"{failed} model file(s) failed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
