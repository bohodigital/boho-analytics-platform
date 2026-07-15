"""Connector registry."""

from __future__ import annotations

from ..config import AppConfig
from ..http import JsonHttpClient
from .cloudflare import CloudflareAnalyticsConnector, CloudflareFormsConnector
from .fixture import FixtureConnector
from .forms_inbox import FormsInboxConnector
from .google import GoogleAnalyticsConnector, SearchConsoleConnector
from .umami import UmamiConnector


def build_connector(provider: str, config: AppConfig, http: JsonHttpClient):
    connectors = {
        "fixture": FixtureConnector,
        "umami": UmamiConnector,
        "cloudflare": CloudflareAnalyticsConnector,
        "google-analytics": GoogleAnalyticsConnector,
        "search-console": SearchConsoleConnector,
        "cloudflare-forms": CloudflareFormsConnector,
        "forms-inbox": FormsInboxConnector,
    }
    try:
        return connectors[provider](config, http)
    except KeyError as exc:
        raise ValueError(f"unsupported connector provider: {provider}") from exc


__all__ = ["build_connector"]
