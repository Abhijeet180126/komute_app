import json
import os
from pathlib import Path

import pytest

MODELS_DIR = Path(__file__).resolve().parent.parent / "backend" / "models"


def _models_present() -> bool:
    """Every file named in models.lock is on disk (fetch_models.py downloads them)."""
    try:
        names = json.loads((MODELS_DIR / "models.lock").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return all((MODELS_DIR / n).is_file() for n in names)


def pytest_collection_modifyitems(config, items):
    d = os.environ.get("KOMUTE_SAMPLES_DIR")
    have_samples = bool(d and Path(d).is_dir() and any(Path(d).iterdir()))
    have_models = _models_present()
    skip_samples = pytest.mark.skip(reason="KOMUTE_SAMPLES_DIR missing or empty")
    skip_models = pytest.mark.skip(reason="model files missing: run python scripts/fetch_models.py")
    for item in items:
        if not have_samples and "samples" in item.keywords:
            item.add_marker(skip_samples)
        if not have_models and "models" in item.keywords:
            item.add_marker(skip_models)
