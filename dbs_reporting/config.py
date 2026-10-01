import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

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


def _clean_site(value: str) -> str:
    """Accept "https://host/", "host/v4_6_release" etc. and keep just the hostname."""
    value = value.strip().strip('"').strip("'")
    for prefix in ("https://", "http://"):
        if value.lower().startswith(prefix):
            value = value[len(prefix):]
    return value.split("/")[0].strip()


@dataclass(frozen=True)
class ConnectWiseSettings:
    site: str
    company_id: str
    public_key: str
    private_key: str
    client_id: str
    codebase: str = "v4_6_release"
    # The company custom field that says which POS software a client uses
    software_field: str = "Software"
    # The project phase whose scheduled day is a site's go-live
    golive_phase: str = "Deployment"

    @classmethod
    def from_env(cls) -> "ConnectWiseSettings":
        return cls(
            site=_clean_site(_required("CW_SITE")),
            company_id=_required("CW_COMPANY_ID"),
            public_key=_required("CW_PUBLIC_KEY"),
            private_key=_required("CW_PRIVATE_KEY"),
            client_id=_required("CW_CLIENT_ID"),
            codebase=os.environ.get("CW_CODEBASE", "v4_6_release").strip() or "v4_6_release",
            software_field=os.environ.get("CW_SOFTWARE_FIELD", "Software").strip().rstrip(":") or "Software",
            golive_phase=os.environ.get("CW_GOLIVE_PHASE", "Deployment").strip() or "Deployment",
        )

    @property
    def base_url(self) -> str:
        return f"https://{self.site}/{self.codebase}/apis/3.0"

    @property
    def ticket_url(self) -> str:
        """Link that opens a ticket in ConnectWise, with {id} where the ticket number goes.

        CW_TICKET_URL overrides it ("off" turns links off). Otherwise it's built from CW_SITE, minus the
        "api-" that cloud API hostnames start with (api-na.myconnectwise.net -> na.myconnectwise.net)."""
        custom = os.environ.get("CW_TICKET_URL", "").strip()
        if custom.lower() == "off":
            return ""
        if custom:
            return custom if "{id}" in custom else ""
        host = self.site[4:] if self.site.lower().startswith("api-") else self.site
        return (f"https://{host}/{self.codebase}/ConnectWise.aspx?locale=en_US&routeTo=ServiceFV"
                f"&companyName={quote(self.company_id)}" "&recid={id}")
