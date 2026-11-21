from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Real environment variables win over .env. The .env file is git-ignored; see .env.example.
    # extra="ignore": this file may be the main Komüte app's .env, which holds many keys that are not ours.
    model_config = SettingsConfigDict(env_prefix="KOMUTE_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      validate_by_name=True, validate_by_alias=True)

    env: Literal["dev", "production"] = "dev"
    # unset or empty = no authentication (local testing only). DL_VERIFICATION_API_KEY is the key name the main app
    # already uses, so one value serves both; KOMUTE_API_KEY wins when both are set.
    api_key: SecretStr | None = Field(None, validation_alias=AliasChoices("KOMUTE_API_KEY", "DL_VERIFICATION_API_KEY"))
    models_dir: Path = Path("backend/models")
    liveness_model: Path | None = None
    dev_skip_liveness: bool = False
    response_barcode_fields: bool = False  # true adds every decoded barcode element (address too) to the response
    face_match_threshold: float = Field(0.35, ge=0.2, le=0.95)  # AgeDB-30: 0/3000 false accepts at 0.35
    face_detect_score: float = Field(0.7, gt=0, lt=1)
    liveness_threshold: float = Field(0.5, gt=0, lt=1)
    name_match_threshold: float = Field(90.0, ge=50, le=100)
    min_age: int = Field(18, ge=16, le=100)
    plate_detect_score: float = Field(0.35, gt=0, lt=1)
    barcode_budget_s: float = Field(10.0, gt=0, le=60)
    max_text_chars: int = Field(200, ge=20, le=1000)
    # MiniFASNet-style defaults. Unverified until the owner supplies the liveness model file.
    liveness_crop_scale: float = 2.7
    liveness_input_size: int = 80
    liveness_live_index: int = 1
    plate_min_confidence: float = 0.6
    max_upload_bytes: int = 10_000_000
    max_image_side: int = 2048
    retry_after_s: int = 5
    warmup_max_rss_mb: float = 400  # warm the models at start only while the node is below this; 0 disables

    @field_validator("api_key")
    @classmethod
    def _empty_key_is_no_key(cls, v: SecretStr | None) -> SecretStr | None:
        return v if v is not None and v.get_secret_value() else None

    @field_validator("liveness_model", mode="before")
    @classmethod
    def _blank_path_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v  # a blank .env line must not become Path(".")

    @model_validator(mode="after")
    def _production_rules(self) -> Settings:
        if self.env == "production":
            if self.api_key is None or len(self.api_key.get_secret_value()) < 24:
                raise ValueError("api_key (at least 24 characters) is required in production")
            if self.dev_skip_liveness:
                raise ValueError("dev_skip_liveness is forbidden in production")
            if self.liveness_model is None:
                raise ValueError("liveness_model is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
