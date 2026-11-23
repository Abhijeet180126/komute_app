from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # Real environment variables win over .env. extra="ignore": this reads the main Komute app's .env, which holds
    # many keys that are not ours.
    model_config = SettingsConfigDict(env_prefix="MAP_", env_file=".env", env_file_encoding="utf-8", extra="ignore",
                                      validate_by_name=True, validate_by_alias=True)

    env: Literal["dev", "production"] = Field("dev", validation_alias=AliasChoices("MAP_ENV", "KOMUTE_ENV"))
    # Key the main app sends in X-API-Key (MAP_API_KEY). Unset or empty = no authentication (local testing only).
    api_key: SecretStr | None = None
    # Apple Maps credentials from the developer portal (see README).
    apple_team_id: str | None = Field(None, validation_alias=AliasChoices("APPLE_MAPS_TEAM_ID"))
    apple_key_id: str | None = Field(None, validation_alias=AliasChoices("APPLE_MAPS_KEY_ID"))
    apple_private_key_path: Path | None = Field(None, validation_alias=AliasChoices("APPLE_MAPS_PRIVATE_KEY_PATH"))
    apple_private_key: SecretStr | None = Field(None, validation_alias=AliasChoices("APPLE_MAPS_PRIVATE_KEY"))  # inline PEM
    apple_web_origin: str | None = Field(None, validation_alias=AliasChoices("APPLE_MAPS_WEB_ORIGIN"))  # e.g. https://komute.ca
    connect_timeout_s: float = Field(3.05, gt=0, le=30)
    read_timeout_s: float = Field(10.0, gt=0, le=60)
    retry_after_s: int = 5
    max_waypoints: int = Field(5, ge=0, le=20)
    max_matrix_cells: int = Field(100, ge=1, le=2500)  # origins x destinations
    # Countries the node serves (Apple's limitToCountries and the reverse-geocode guard). Komute supports the US and Canada.
    countries: str = Field("CA,US", validation_alias=AliasChoices("MAP_COUNTRIES"))
    # Where autocomplete looks when the caller sends no location. Verified: a Canada-wide default (Ottawa) still finds
    # Vancouver, Calgary and Halifax addresses; with no location at all Apple often answers nothing.
    default_bias: str = Field("45.4215,-75.6972", validation_alias=AliasChoices("MAP_DEFAULT_BIAS"))
    max_matrix_origins: int = Field(20, ge=1, le=100)  # Apple takes one origin per call, so each origin is a call
    # Short on purpose: repeat queries cost quota, and Apple's terms on keeping results are not verified.
    suggest_ttl_s: int = Field(300, ge=0)
    place_ttl_s: int = Field(3600, ge=0)  # details, geocode, reverse
    cache_max_entries: int = Field(2000, ge=1)

    @field_validator("api_key", "apple_private_key")
    @classmethod
    def _empty_secret_is_none(cls, v: SecretStr | None) -> SecretStr | None:
        return v if v is not None and v.get_secret_value().strip() else None

    @field_validator("apple_team_id", "apple_key_id", "apple_web_origin", mode="before")
    @classmethod
    def _blank_is_none(cls, v):
        v = v.strip() if isinstance(v, str) else v
        return v or None  # a blank .env line is "not set"

    @field_validator("apple_web_origin")
    @classmethod
    def _origin_has_a_scheme(cls, v: str | None) -> str | None:
        # MapKit compares this to the page's origin, which always carries the scheme ("https://komute.ca", no path).
        if v is not None and not re.fullmatch(r"https?://[A-Za-z0-9.*-]+(:\d{1,5})?", v):
            raise ValueError("apple_web_origin must look like https://komute.ca (scheme and host, no path or slash)")
        return v

    @field_validator("countries")
    @classmethod
    def _countries_are_iso_codes(cls, v: str) -> str:
        v = v.replace(" ", "").upper()
        if not re.fullmatch(r"[A-Z]{2}(,[A-Z]{2})*", v):
            raise ValueError("countries must be ISO 3166-1 codes separated by commas, e.g. CA,US")
        return v

    @field_validator("default_bias")
    @classmethod
    def _default_bias_is_lat_lon(cls, v: str) -> str:
        try:
            lat, lon = (float(x) for x in v.split(","))
        except ValueError:
            raise ValueError("default_bias must look like 45.4215,-75.6972") from None
        if not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError("default_bias is out of range")
        return f"{lat},{lon}"

    @property
    def countries_list(self) -> list[str]:
        return self.countries.split(",")

    @property
    def default_bias_point(self) -> tuple[float, float]:
        lat, lon = self.default_bias.split(",")
        return float(lat), float(lon)

    @field_validator("apple_private_key_path", mode="before")
    @classmethod
    def _blank_path_is_none(cls, v):
        return None if isinstance(v, str) and not v.strip() else v  # a blank line must not become Path(".")

    def apple_key_pem(self) -> str | None:
        """The .p8 text, from the inline setting or the file. None when neither is set or the file is unreadable."""
        if self.apple_private_key is not None:
            return self.apple_private_key.get_secret_value()
        if self.apple_private_key_path is not None:
            try:
                return self.apple_private_key_path.read_text(encoding="utf-8")
            except OSError:
                return None
        return None

    @property
    def apple_ready(self) -> bool:
        return bool(self.apple_team_id and self.apple_key_id and self.apple_key_pem())

    @model_validator(mode="after")
    def _production_rules(self) -> Settings:
        if self.env == "production":
            if self.api_key is None or len(self.api_key.get_secret_value()) < 24:
                raise ValueError("api_key (at least 24 characters) is required in production")
            if not self.apple_ready:
                raise ValueError("apple_team_id, apple_key_id and a readable private key are required in production")
            if self.apple_web_origin is None:
                raise ValueError("apple_web_origin is required in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
