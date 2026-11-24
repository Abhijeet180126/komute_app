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
    # Required in every mode: an unset key once meant an open API. DL_VERIFICATION_API_KEY is the key name the main app
    # already uses, so one value serves both; KOMUTE_API_KEY wins when both are set.
    api_key: SecretStr | None = Field(None, validation_alias=AliasChoices("KOMUTE_API_KEY", "DL_VERIFICATION_API_KEY"))
    models_dir: Path = Path("backend/models")
    liveness_model: Path | None = None
    dev_skip_liveness: bool = False
    response_barcode_fields: bool = False  # true adds every decoded barcode element (address too) to the response
    face_match_threshold: float = Field(0.40, ge=0.2, le=0.95)  # SFace on AgeDB-30: 1/3000 false accepts at 0.40
    face_detect_score: float = Field(0.7, gt=0, lt=1)
    liveness_threshold: float = Field(0.5, gt=0, lt=1)
    name_match_threshold: float = Field(90.0, ge=50, le=100)
    min_age: int = Field(18, ge=16, le=100)
    plate_detect_score: float = Field(0.35, gt=0, lt=1)
    barcode_budget_s: float = Field(10.0, gt=0, le=60)
    max_text_chars: int = Field(200, ge=20, le=1000)
    # MiniFASNetV2 (backend/models/minifasnet_v2.onnx): 2.7x crop, 80x80 input, class 1 = live.
    liveness_crop_scale: float = 2.7
    liveness_input_size: int = 80
    liveness_live_index: int = 1
    plate_min_confidence: float = 0.6
    max_upload_bytes: int = 10_000_000
    max_image_side: int = 2048
    retry_after_s: int = 5
    # Requests wait in line this long for their turn, then 503 BUSY. Below the main app's 120 s call timeout and Nginx's.
    busy_wait_s: float = Field(100.0, ge=0, le=110)
    # The verification queue (FIFO, one check at a time). REDIS_URL is the name the main app already uses, so the shared
    # .env serves both; KOMUTE_REDIS_URL wins when both are set. Unset: the node queues in memory (order not guaranteed).
    redis_url: str | None = Field(None, validation_alias=AliasChoices("KOMUTE_REDIS_URL", "REDIS_URL"))
    warmup_max_rss_mb: float = 400  # warm the models at start only while the node is below this; 0 disables

    @field_validator("api_key")
    @classmethod
    def _empty_key_is_no_key(cls, v: SecretStr | None) -> SecretStr | None:
        return v if v is not None and v.get_secret_value().strip() else None

    @field_validator("redis_url", mode="before")
    @classmethod
    def _blank_url_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v

    @field_validator("liveness_model", mode="before")
    @classmethod
    def _blank_path_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v  # a blank .env line must not become Path(".")

    @model_validator(mode="after")
    def _production_rules(self) -> Settings:
        if self.api_key is None:
            raise ValueError("api_key is required: set DL_VERIFICATION_API_KEY (or KOMUTE_API_KEY)")
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
