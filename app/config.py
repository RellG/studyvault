"""Settings from environment variables. Read once at import; tests override attributes."""
import os
from dataclasses import dataclass
from pathlib import Path


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Settings:
    data_dir: Path
    seed_file: Path
    password: str
    secret: str
    tz: str
    ai_provider: str
    ai_model: str
    anthropic_api_key: str
    google_api_key: str
    ai_max_output_tokens: int
    debug: bool

    @property
    def db_path(self) -> Path:
        return self.data_dir / "studyvault.db"

    @property
    def notes_dir(self) -> Path:
        return self.data_dir / "notes"

    @property
    def attachments_dir(self) -> Path:
        return self.data_dir / "attachments"

    @property
    def ai_key(self) -> str:
        return {"anthropic": self.anthropic_api_key, "google": self.google_api_key}.get(self.ai_provider, "")

    @property
    def ai_enabled(self) -> bool:
        return bool(self.ai_provider and self.ai_model and self.ai_key)


def _default_seed(root: Path) -> Path:
    """Your own seed/courses.yaml if present (git-ignored), else the public example."""
    own = root / "seed" / "courses.yaml"
    return own if own.exists() else root / "seed" / "courses.example.yaml"


def from_env() -> Settings:
    root = Path(__file__).resolve().parent.parent
    return Settings(
        data_dir=Path(os.environ.get("STUDYVAULT_DATA", "/data")),
        seed_file=Path(os.environ.get("STUDYVAULT_SEED") or _default_seed(root)),
        password=os.environ.get("STUDYVAULT_PASSWORD", ""),
        secret=os.environ.get("STUDYVAULT_SECRET", ""),
        tz=os.environ.get("STUDYVAULT_TZ", "America/New_York"),
        ai_provider=os.environ.get("AI_PROVIDER", "").strip().lower(),
        ai_model=os.environ.get("AI_MODEL", "").strip(),
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY", "").strip(),
        google_api_key=os.environ.get("GOOGLE_API_KEY", "").strip(),
        ai_max_output_tokens=_int("AI_MAX_OUTPUT_TOKENS", 2048),
        debug=os.environ.get("STUDYVAULT_DEBUG", "") == "1",
    )


settings = from_env()
