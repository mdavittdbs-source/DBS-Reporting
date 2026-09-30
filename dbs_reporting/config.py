import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable {name} (see .env.example)")
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
