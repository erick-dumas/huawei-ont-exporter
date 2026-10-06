"""Configuración de la aplicación cargada desde variables de entorno / archivo .env."""

import base64
import binascii
from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- ONT ---
    ont_ip: str = Field(..., description="IP o hostname de la ONT Huawei")
    ont_username: str = Field(..., min_length=1)
    ont_password: SecretStr = Field(..., description="Contraseña (plano o Base64)")
    ont_password_encoding: Literal["plain", "base64"] = Field(
        "base64",
        description=(
            "Formato de ONT_PASSWORD. 'base64' = ya viene codificada (se envía tal cual); "
            "'plain' = texto plano (se codifica en Base64 antes de enviarla al login.cgi)."
        ),
    )
    ont_language: str = "english"
    request_timeout_seconds: float = Field(10.0, gt=0, le=120)

    # --- Muestreo ---
    poll_interval_seconds: float = Field(5.0, ge=1, le=3600)
    max_backoff_seconds: float = Field(120.0, ge=1)

    # --- API ---
    app_host: str = "0.0.0.0"
    app_port: int = Field(8000, ge=1, le=65535)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    @field_validator("ont_ip")
    @classmethod
    def _clean_ip(cls, v: str) -> str:
        v = v.strip().removeprefix("http://").removeprefix("https://").rstrip("/")
        if not v:
            raise ValueError("ONT_IP no puede estar vacío")
        return v

    @field_validator("log_level", mode="before")
    @classmethod
    def _upper_level(cls, v: str) -> str:
        return v.upper() if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_password(self) -> "Settings":
        raw = self.ont_password.get_secret_value()
        if not raw:
            raise ValueError("ONT_PASSWORD no puede estar vacío")
        if self.ont_password_encoding == "base64":
            try:
                base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise ValueError(
                    "ONT_PASSWORD no es Base64 válido; usa ONT_PASSWORD_ENCODING=plain "
                    "si la contraseña está en texto plano"
                ) from exc
        return self

    @property
    def base_url(self) -> str:
        return f"http://{self.ont_ip}"

    @property
    def login_password(self) -> str:
        """Contraseña tal como la espera login.cgi (Base64)."""
        raw = self.ont_password.get_secret_value()
        if self.ont_password_encoding == "plain":
            return base64.b64encode(raw.encode("utf-8")).decode("ascii")
        return raw


@lru_cache
def get_settings() -> Settings:
    return Settings()
