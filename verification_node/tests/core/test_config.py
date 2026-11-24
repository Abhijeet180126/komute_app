from pathlib import Path

import pytest
from pydantic import ValidationError

from backend.core.config import Settings

FACE_THRESHOLD = 0.40  # set from the SFace AgeDB-30 benchmark (docs/calibration/face-agedb30-sface.md)


def make(**kw) -> Settings:
    base = dict(api_key="a" * 32)
    base.update(kw)
    return Settings(_env_file=None, **base)


def test_defaults_ok():
    s = make()
    assert s.env == "dev" and s.face_match_threshold == FACE_THRESHOLD


def test_production_rejects_dev_skip_liveness():
    with pytest.raises(ValidationError):
        make(env="production", dev_skip_liveness=True, liveness_model=Path("m.onnx"))


def test_production_requires_liveness_model():
    with pytest.raises(ValidationError):
        make(env="production")
    assert make(env="production", liveness_model=Path("m.onnx")).env == "production"


def test_api_key_required_in_every_mode():
    # An unset or blank key used to mean an open API in dev mode (ticket item 2).
    for env in ("dev", "production"):
        for key in (None, "", "   "):
            with pytest.raises(ValidationError):
                make(env=env, liveness_model=Path("m.onnx"), api_key=key)


def test_production_rejects_short_api_key_dev_allows_it():
    with pytest.raises(ValidationError):
        make(env="production", liveness_model=Path("m.onnx"), api_key="short")
    assert make(api_key="short").env == "dev"
    assert make(env="production", liveness_model=Path("m.onnx"), api_key="k" * 24).env == "production"


def test_new_tunables_default_and_are_bounded():
    s = make()
    assert (s.name_match_threshold, s.min_age, s.plate_detect_score, s.barcode_budget_s) == (90.0, 18, 0.35, 10.0)
    for bad in ({"face_match_threshold": 0.01}, {"min_age": 5}, {"name_match_threshold": 10}, {"barcode_budget_s": 0}):
        with pytest.raises(ValidationError):
            make(**bad)


def test_values_load_from_dotenv_file(tmp_path):
    f = tmp_path / ".env"
    f.write_text("KOMUTE_API_KEY=k\nKOMUTE_FACE_MATCH_THRESHOLD=0.55\n")
    assert Settings(_env_file=f).face_match_threshold == 0.55


def test_the_main_app_env_file_can_be_shared(tmp_path):
    # One .env for both apps: the main app's keys (tricky values included) are ignored, and the node reads the
    # key name the main app already uses.
    f = tmp_path / ".env"
    f.write_text(
        'SECRET_KEY=fake-#-secret-$x\nDEBUG=False\nALLOWED_HOSTS=a.net,b.net\nEMAIL_HOST_PASSWORD="p@ss # not a comment"\n'
        "DL_VERIFICATION_API_URL=http://127.0.0.1:8080/api/v1/verify\nDL_VERIFICATION_API_KEY=shared-key-from-main-app\n"
        "KOMUTE_ENV=dev\nKOMUTE_DEV_SKIP_LIVENESS=true\nKOMUTE_FACE_MATCH_THRESHOLD=0.35  # inline comment\n"
    )
    s = Settings(_env_file=f)
    assert s.api_key.get_secret_value() == "shared-key-from-main-app"
    assert (s.env, s.dev_skip_liveness, s.face_match_threshold) == ("dev", True, 0.35)


def test_komute_api_key_wins_over_the_main_app_name(tmp_path):
    f = tmp_path / ".env"
    f.write_text("DL_VERIFICATION_API_KEY=from-main-app\nKOMUTE_API_KEY=from-node\n")
    assert Settings(_env_file=f).api_key.get_secret_value() == "from-node"


def test_no_storage_settings_remain():
    assert not [n for n in Settings.model_fields if "storage" in n or "manifest" in n or "ttl" in n]


def test_blank_liveness_model_line_means_none():
    assert make(liveness_model="").liveness_model is None
