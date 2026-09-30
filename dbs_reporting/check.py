"""Connection check: `python -m dbs_reporting.check`.

Walks through each step of reaching ConnectWise and Anthropic and says where it breaks.
Never prints keys.
"""

import os
import socket
import urllib.request

import httpx

from .config import ENV_FILE, ConnectWiseSettings
from .connectwise import ConnectWiseClient


def step(n: int, text: str) -> None:
    print(f"\n[{n}] {text}")


def ok(text: str) -> None:
    print(f"    OK   {text}")


def fail(text: str) -> None:
    print(f"    FAIL {text}")


def main() -> None:
    step(1, f"Settings file: {ENV_FILE}")
    if not ENV_FILE.exists():
        fail("not found. Copy .env.example to .env and fill it in.")
        return
    ok("found")

    try:
        settings = ConnectWiseSettings.from_env()
    except RuntimeError as exc:
        fail(str(exc))
        return
    print(f"    CW_SITE as written in .env: {os.environ.get('CW_SITE')!r}")
    print(f"    Host the bot will use:      {settings.site!r}")
    print(f"    Full API address:           {settings.base_url}")
    print(f"    Company ID: {settings.company_id!r}   Codebase: {settings.codebase!r}")

    step(2, "Proxy settings")
    env_proxy = {k: v for k, v in os.environ.items() if k in ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY")}
    system_proxy = {k: v for k, v in urllib.request.getproxies().items() if k in ("http", "https")}
    print(f"    Proxy from environment/.env: {env_proxy or 'none'}")
    print(f"    Proxy from Windows settings: {system_proxy or 'none'}")
    if system_proxy and not env_proxy:
        print("    NOTE: Windows uses a proxy but the bot doesn't. If later steps fail, add")
        print(f"          HTTPS_PROXY={system_proxy.get('https') or system_proxy.get('http')} to .env")

    step(3, f"Look up the address of {settings.site}")
    try:
        addresses = sorted({a[4][0] for a in socket.getaddrinfo(settings.site, 443)})
        ok(", ".join(addresses))
    except OSError as exc:
        fail(f"{exc}")
        print("    This PC can't find that host name. Compare CW_SITE with the address")
        print("    your working bot uses (host part only, e.g. api-na.myconnectwise.net).")
        if not env_proxy:
            return

    step(4, "Reach ConnectWise (no login needed)")
    try:
        info = httpx.get(f"https://{settings.site}/login/companyinfo/{settings.company_id}", timeout=30)
        if info.status_code == 200 and info.text.strip() not in ("", "null"):
            data = info.json()
            ok(f"company {data.get('CompanyName')!r}, codebase {data.get('Codebase')!r}")
            codebase = (data.get("Codebase") or "").strip("/")
            if codebase and codebase != settings.codebase:
                print(f"    NOTE: your server's codebase is {codebase!r}. Add CW_CODEBASE={codebase} to .env")
        else:
            fail(f"HTTP {info.status_code}: company ID {settings.company_id!r} not found on this site")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return

    step(5, "Log in with your API keys")
    cw = ConnectWiseClient(settings)
    try:
        system = cw.get("/system/info")
        ok(f"ConnectWise version {system.get('version')}")
    except httpx.HTTPStatusError as exc:
        fail(f"HTTP {exc.response.status_code}: {exc.response.text[:300]}")
        return
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return

    step(6, "Read companies (checks the security role)")
    try:
        companies = cw.get("/company/companies", pageSize=3, fields="id,name")
        ok(f"can read companies, e.g. {[c['name'] for c in companies]}")
    except httpx.HTTPStatusError as exc:
        fail(f"HTTP {exc.response.status_code}: {exc.response.text[:300]}")
        return
    try:
        cw.get("/finance/agreements", pageSize=1, fields="id")
        ok("can read agreements")
    except httpx.HTTPStatusError as exc:
        print(f"    NOTE can't read agreements (HTTP {exc.response.status_code}). Agreement questions won't work")
        print("         until the API member's security role has Finance > Agreements: Inquire Level = All.")

    from .agent import configured_models

    default, choices = configured_models()
    step(7, f"Anthropic API key and models (default {default!r})")
    try:
        import anthropic

        client = anthropic.Anthropic()
        for model in choices:
            client.models.retrieve(model)
            ok(f"{model} available")
    except Exception as exc:
        fail(f"{type(exc).__name__}: {exc}")
        return

    from .store import Store
    from .userfile import UsersFile

    step(8, "Web chat logins (users.txt)")
    store = Store()
    users_file = UsersFile(store)
    users_file.ensure_exists()
    users_file.refresh()
    for problem in users_file.problems:
        print(f"    NOTE users.txt {problem}")
    count = store.user_count()
    if count == 0:
        fail(f"no logins yet. Add a line per person to {users_file.path}")
        return
    ok(f"{count} login(s) from {users_file.path}")

    print("\nAll checks passed. Run: python -m dbs_reporting.cli  or start the web chat with start-web.bat")


if __name__ == "__main__":
    main()
