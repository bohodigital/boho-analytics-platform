from __future__ import annotations

from pathlib import Path


def config_text(state: Path, fixture: Path, *, provider: str = "fixture", credential_ref: str = "none:test", options: str | None = None) -> str:
    option_block = options if options is not None else f'path = "{fixture.as_posix()}"'
    return f'''schema_version = 2
[platform]
default_timezone = "UTC"
state_path = "{state.as_posix()}"
default_sync_days = 30
[web]
bind_host = "127.0.0.1"
port = 8787
allowed_hosts = ["127.0.0.1", "localhost"]
auth_mode = "none"
[retention]
hourly_days = 90
daily_days = 1095
[[clients]]
id = "example-client"
name = "Example Client"
[[sites]]
id = "example-site"
client_id = "example-client"
name = "Example Site"
canonical_url = "https://example.com"
timezone = "UTC"
[[connections]]
id = "example-connection"
provider = "{provider}"
credential_ref = "{credential_ref}"
[connections.options]
{option_block}
[[bindings]]
site_id = "example-site"
connection_id = "example-connection"
resource_type = "website"
resource_id = "demo"
metric_groups = ["traffic"]
[[reports]]
id = "summary"
title = "Summary"
client_id = "example-client"
site_ids = ["example-site"]
metric_ids = ["umami.pageviews", "forms.submissions", "forms.inbox-deliveries"]
default_window_days = 30
[[reports.subreports]]
id = "forms"
title = "Forms"
metric_ids = ["forms.submissions", "forms.inbox-deliveries"]
default_window_days = 30
'''


def write_fixture(path: Path) -> None:
    path.write_text('{"points":[{"resource_id":"demo","date":"2026-07-01","metric":"umami.pageviews","unit":"count","value":12},{"resource_id":"demo","date":"2026-07-01","metric":"forms.submissions","unit":"count","value":2},{"resource_id":"demo","date":"2026-07-01","metric":"forms.inbox-deliveries","unit":"count","value":1}]}', encoding="utf-8")
