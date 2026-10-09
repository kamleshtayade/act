"""Runtime settings and secrets.

Everything secret (API keys, tokens) is read from the environment / `.env`
file - see `.env.example` for the full list. Nothing secret lives in code.

Risk limits are deliberately NOT here: they are constants in `risk.py` so an
edited `.env` can never quietly loosen them.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal, Optional

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = PROJECT_ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- runtime -------------------------------------------------------------
    app_mode: Literal["mock", "paper"] = "mock"
    log_level: str = "INFO"
    db_path: Path = PROJECT_ROOT / "data" / "trading_log.db"
    checkpoint_db_path: Path = PROJECT_ROOT / "data" / "checkpoints.db"

    # --- Anthropic -----------------------------------------------------------
    anthropic_api_key: Optional[SecretStr] = None
    claude_model: str = "claude-opus-5-5"
    claude_effort: Literal["low", "medium", "high", "xhigh", "max"] = "low"
    claude_max_tokens: int = 4000
    claude_timeout_seconds: float = 90.0
    claude_enable_server_fallback: bool = True

    # --- Dhan Sandbox (orders) -----------------------------------------------
    dhan_sandbox_client_id: Optional[str] = None
    dhan_sandbox_access_token: Optional[SecretStr] = None
    dhan_sandbox_base_url: str = "https://sandbox.dhan.co/v2"

    # --- Dhan Data API -------------------------------------------------------
    dhan_data_client_id: Optional[str] = None
    dhan_data_access_token: Optional[SecretStr] = None
    dhan_data_base_url: str = "https://api.dhan.co/v2"

    # --- instrument ----------------------------------------------------------
    dhan_crudeoil_security_id: Optional[str] = None
    dhan_exchange_segment: str = "MCX_COMM"

    # --- webhook -------------------------------------------------------------
    gocharting_webhook_secret: Optional[SecretStr] = None
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8000
    signal_cooldown_seconds: int = 300

    # --- macro / events ------------------------------------------------------
    eia_api_key: Optional[SecretStr] = None
    fmp_api_key: Optional[SecretStr] = None
    trading_economics_api_key: Optional[SecretStr] = None

    contract_file: Path = Field(default=CONFIG_DIR / "mcx_crude_contract.json")
    event_calendar_file: Path = Field(default=CONFIG_DIR / "event_calendar.json")

    @model_validator(mode="after")
    def _paper_mode_guards(self) -> "Settings":
        # Hard safety rail: orders may only ever go to the Dhan sandbox host.
        if "sandbox" not in self.dhan_sandbox_base_url.lower():
            raise ValueError(
                "DHAN_SANDBOX_BASE_URL must point at the Dhan sandbox host - "
                "this project never places orders on a live account."
            )
        if self.app_mode == "paper":
            missing = [
                name
                for name in (
                    "anthropic_api_key",
                    "dhan_sandbox_client_id",
                    "dhan_sandbox_access_token",
                    "dhan_data_client_id",
                    "dhan_data_access_token",
                    "gocharting_webhook_secret",
                )
                if not getattr(self, name)
            ]
            if missing:
                raise ValueError(
                    "APP_MODE=paper requires these .env keys: "
                    + ", ".join(m.upper() for m in missing)
                )
        return self

    @property
    def is_mock(self) -> bool:
        return self.app_mode == "mock"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


def load_json(path: Path) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def get_contract() -> dict[str, Any]:
    return load_json(get_settings().contract_file)
