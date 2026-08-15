"""Configuration, loaded from the environment / .env.

Everything the agent is allowed to do is expressed here rather than in prompt
text, so the limits hold regardless of what the model decides.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

# src/log2ticket_triage/config.py -> src/log2ticket_triage -> src -> repo root
PROJECT_ROOT = Path(__file__).resolve().parents[2]

MCP_BASE_URL = "https://api.githubcopilot.com/mcp/x/issues"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Model ---
    llm_model: str = "anthropic:claude-opus-5"

    # --- GitHub ---
    github_token: str = ""
    github_repo: str = ""
    # NoDecode is load-bearing: without it pydantic-settings JSON-decodes any
    # complex-typed field inside the env source, *before* field validators run,
    # so a plain comma-separated REPO_ALLOWLIST in .env raises SettingsError and
    # the app cannot start.
    repo_allowlist: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- Behaviour ---
    dry_run: bool = True
    max_writes_per_run: int = 5
    duplicate_confidence_floor: float = 0.7

    # --- Paths ---
    target_repo_path: Path = Path("samples/app")

    # --- Context assembly ---
    source_context_lines: int = 15
    max_excerpt_bytes: int = 4_000

    # --- Local variable capture ---
    # Off by default. Locals are the highest-risk field we send: they hold
    # request bodies, and whatever a credential was assigned to.
    capture_locals: bool = False
    max_locals_per_frame: int = 12
    max_local_repr_chars: int = 200

    # --- Incident buffer ---
    incident_buffer_size: int = 50

    @field_validator("repo_allowlist", mode="before")
    @classmethod
    def _split_csv(cls, v: object) -> object:
        """REPO_ALLOWLIST is a comma-separated string in .env."""
        if isinstance(v, str):
            return [item.strip() for item in v.split(",") if item.strip()]
        return v

    @field_validator("target_repo_path", mode="after")
    @classmethod
    def _absolutise(cls, v: Path) -> Path:
        return v if v.is_absolute() else (PROJECT_ROOT / v).resolve()

    @property
    def mcp_url(self) -> str:
        """Read-only endpoint in dry-run, so write tools are never even loaded."""
        return f"{MCP_BASE_URL}/readonly" if self.dry_run else MCP_BASE_URL

    def writes_allowed_to(self, repo: str) -> bool:
        return not self.dry_run and repo in self.repo_allowlist


def get_settings() -> Settings:
    return Settings()
