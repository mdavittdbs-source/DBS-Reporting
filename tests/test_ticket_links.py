"""The link that opens a ticket in ConnectWise."""

from dbs_reporting.config import ConnectWiseSettings


def settings(site):
    return ConnectWiseSettings(site=site, company_id="c", public_key="p", private_key="k", client_id="i")


def test_cloud_site_drops_api_prefix(monkeypatch):
    monkeypatch.delenv("CW_TICKET_URL", raising=False)
    url = settings("api-na.myconnectwise.net").ticket_url
    assert url == ("https://na.myconnectwise.net/v4_6_release/ConnectWise.aspx?locale=en_US&routeTo=ServiceFV"
                   "&companyName=c&recid={id}")


def test_self_hosted_site_kept(monkeypatch):
    monkeypatch.delenv("CW_TICKET_URL", raising=False)
    assert settings("cw.example.com").ticket_url.startswith("https://cw.example.com/v4_6_release/")


def test_override_and_off(monkeypatch):
    monkeypatch.setenv("CW_TICKET_URL", "https://cw.example.com/t?id={id}")
    assert settings("api-na.myconnectwise.net").ticket_url == "https://cw.example.com/t?id={id}"
    monkeypatch.setenv("CW_TICKET_URL", "https://no-placeholder.example.com/")
    assert settings("x").ticket_url == ""
    monkeypatch.setenv("CW_TICKET_URL", "off")
    assert settings("x").ticket_url == ""
