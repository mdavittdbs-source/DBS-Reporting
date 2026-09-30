import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(ENV_FILE)


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        if not ENV_FILE.exists():
            hint = f"No settings file found at {ENV_FILE}."
            if ENV_FILE.with_name(".env.txt").exists():
                hint += " Found .env.txt instead: rename it to .env (Notepad added .txt)."
            else:
                hint += " Copy .env.example to .env and fill it in."
        else:
            hint = f"{ENV_FILE} exists but has no value for {name}."
        raise RuntimeError(f"Missing setting {name}. {hint}")
    return value


@dataclass(frozen=True)
class ConnectWiseSettings:
    site: str
    company_id: str
    public_key: str
    private_key: str
    client_id: str
    codebase: str = "v4_6_release"

    @classmethod
    def from_env(cls) -> "ConnectWiseSettings":
        return cls(
            site=_required("CW_SITE"),
            company_id=_required("CW_COMPANY_ID"),
            public_key=_required("CW_PUBLIC_KEY"),
            private_key=_required("CW_PRIVATE_KEY"),
            client_id=_required("CW_CLIENT_ID"),
            codebase=os.environ.get("CW_CODEBASE", "v4_6_release").strip() or "v4_6_release",
        )

    @property
    def base_url(self) -> str:
        return f"https://{self.site}/{self.codebase}/apis/3.0"
