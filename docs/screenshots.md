# Headless demo screenshots

The GitHub screenshots are generated from the checked-in fixture, never from a private analytics
configuration or live database. The capture harness creates a temporary TOML file, temporary SQLite
database, temporary browser profile, and random loopback port. It validates, initializes, and syncs
only `examples/platform.demo.toml` and `examples/fixtures/demo.json`. It fails closed unless every
configured connection is the fixture provider with the `none:fixture` credential reference, and its
child processes receive an allowlisted environment that excludes analytics credentials.

The design mirrors the Boho Secret Broker screenshot workflow: use an explicit demo mode, isolate the
runtime, wait for readiness, capture deterministic views, validate the image files, and clean up every
temporary process and file. A web dashboard does not need Xvfb, so this harness uses Edge, Chrome, or
Chromium's native headless mode.

## Capture

On Linux or macOS with Chromium installed:

```bash
scripts/capture_dashboard_headless.sh
```

On Windows with Microsoft Edge or Chrome installed:

```powershell
python scripts\capture_dashboard_headless.py
```

Set `BOHO_SCREENSHOT_BROWSER` to an explicit Chromium-family executable when automatic detection is
not appropriate. Some locked-down CI hosts cannot initialize the browser's operating-system sandbox.
For that specific case, `BOHO_SCREENSHOT_NO_SANDBOX=1` is available as an explicit opt-in. It is safe
only because the harness uses a fixed loopback origin, public fixture data, and a disposable profile;
do not reuse that setting for arbitrary or external pages.

The default outputs are:

- `docs/images/boho-analytics-dashboard.png`
- `docs/images/boho-analytics-plot-builder.png`

The release verifier accepts PNG files only under `docs/images`, enforces the normal public file-size
limit, and checks their PNG signatures. Review both images before committing them.
