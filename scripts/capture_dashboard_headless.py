#!/usr/bin/env python3
"""Capture public dashboard screenshots from disposable fixture data only."""

from __future__ import annotations

import argparse
import os
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen


ROOT = Path(__file__).resolve().parents[1]
DEMO_CONFIG = ROOT / "examples" / "platform.demo.toml"
DEMO_FIXTURE = ROOT / "examples" / "fixtures" / "demo.json"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
CAPTURES = (
    (
        "boho-analytics-dashboard.png",
        "/?report=summary&start=2026-07-01&end=2026-07-04&metric=umami.pageviews",
    ),
    (
        "boho-analytics-plot-builder.png",
        "/?report=summary&view=plot&source=search-console&metric=search.clicks"
        "&style=area&start=2026-07-01&end=2026-07-04",
    ),
)
SAFE_ENVIRONMENT_KEYS = {
    "APPDATA",
    "COMSPEC",
    "HOME",
    "LANG",
    "LC_ALL",
    "LOCALAPPDATA",
    "PATH",
    "PATHEXT",
    "PROGRAMDATA",
    "SYSTEMDRIVE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "TMPDIR",
    "USERPROFILE",
    "WINDIR",
    "XDG_RUNTIME_DIR",
}


def _toml_path(path: Path) -> str:
    return path.resolve().as_posix().replace('"', '\\"')


def _replace_once(text: str, old: str, new: str) -> str:
    if text.count(old) != 1:
        raise RuntimeError(f"demo template must contain exactly one {old!r}")
    return text.replace(old, new)


def _assert_fixture_only(text: str) -> None:
    config = tomllib.loads(text)
    connections = config.get("connections")
    if not isinstance(connections, list) or not connections:
        raise RuntimeError("refusing capture because the demo config has no fixture connection")
    for connection in connections:
        if not isinstance(connection, dict):
            raise RuntimeError("refusing capture because a demo connection is malformed")
        if connection.get("provider") != "fixture":
            raise RuntimeError("refusing capture because every demo provider must be fixture")
        if connection.get("credential_ref") != "none:fixture":
            raise RuntimeError("refusing capture because every demo credential must be none:fixture")


def _safe_environment() -> dict[str, str]:
    """Pass only operating-system essentials; never forward provider credentials."""

    environment = {
        key: value
        for key, value in os.environ.items()
        if key.upper() in SAFE_ENVIRONMENT_KEYS
    }
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    return environment


def _demo_config_text(*, state_path: Path, fixture_path: Path, port: int) -> str:
    """Resolve only the checked-in demo template into a disposable configuration."""

    text = DEMO_CONFIG.read_text(encoding="utf-8")
    text = _replace_once(
        text,
        'state_path = "../var/demo.sqlite3"',
        f'state_path = "{_toml_path(state_path)}"',
    )
    text = _replace_once(text, "port = 8787", f"port = {port}")
    text = _replace_once(
        text,
        'path = "fixtures/demo.json"',
        f'path = "{_toml_path(fixture_path)}"',
    )
    _assert_fixture_only(text)
    return text


def _browser_candidates() -> list[Path]:
    candidates: list[Path] = []
    override = os.environ.get("BOHO_SCREENSHOT_BROWSER")
    if override:
        candidates.append(Path(override))
    for command in ("chromium", "chromium-browser", "google-chrome", "microsoft-edge", "msedge"):
        resolved = shutil.which(command)
        if resolved:
            candidates.append(Path(resolved))
    if os.name == "nt":
        roots = [
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMFILES"),
            os.environ.get("LOCALAPPDATA"),
        ]
        relative_paths = (
            Path("Microsoft/Edge/Application/msedge.exe"),
            Path("Google/Chrome/Application/chrome.exe"),
        )
        for root in roots:
            if root:
                candidates.extend(Path(root) / relative for relative in relative_paths)
    return candidates


def _find_browser() -> Path:
    for candidate in _browser_candidates():
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(
        "no Chromium-family browser found; set BOHO_SCREENSHOT_BROWSER to Edge, Chrome, or Chromium"
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _run_cli(config_path: Path, *arguments: str, env: dict[str, str]) -> None:
    subprocess.run(
        [sys.executable, "-m", "boho_analytics_platform", "--config", str(config_path), *arguments],
        cwd=ROOT,
        env=env,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _wait_for_server(port: int, process: subprocess.Popen[str]) -> None:
    endpoint = f"http://127.0.0.1:{port}/healthz"
    for _ in range(100):
        if process.poll() is not None:
            stdout, stderr = process.communicate()
            raise RuntimeError(f"demo server exited early\n{stdout}\n{stderr}".strip())
        try:
            with urlopen(endpoint, timeout=0.5) as response:  # noqa: S310 - fixed loopback URL
                if response.status == 200 and response.read() == b'{"ok":true}':
                    return
        except (URLError, TimeoutError):
            time.sleep(0.05)
    raise RuntimeError("timed out waiting for the disposable demo server")


def _png_dimensions(path: Path) -> tuple[int, int]:
    payload = path.read_bytes()
    if not payload.startswith(PNG_SIGNATURE) or len(payload) < 24:
        raise RuntimeError(f"browser did not produce a valid PNG: {path}")
    width, height = struct.unpack(">II", payload[16:24])
    return width, height


def _capture(
    browser: Path,
    profile_root: Path,
    output: Path,
    url: str,
    *,
    env: dict[str, str],
) -> tuple[int, int]:
    profile = profile_root / output.stem
    command = [
        str(browser),
        "--headless=new",
        "--disable-gpu",
        "--disable-extensions",
        "--disable-default-apps",
        "--disable-dev-shm-usage",
        "--hide-scrollbars",
        "--no-first-run",
        "--force-color-profile=srgb",
        "--run-all-compositor-stages-before-draw",
        "--virtual-time-budget=2500",
        "--window-size=1440,1050",
        f"--user-data-dir={profile}",
        f"--screenshot={output}",
        url,
    ]
    if os.environ.get("BOHO_SCREENSHOT_NO_SANDBOX") == "1":
        # Some locked-down CI hosts cannot initialize Chromium's OS sandbox.
        # This opt-in remains bounded to a fixed loopback URL and disposable profile.
        command.insert(1, "--no-sandbox")
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        timeout=45,
        env=env,
    )
    if result.returncode != 0:
        raise RuntimeError(f"headless browser failed ({result.returncode}): {result.stderr.strip()}")
    width, height = _png_dimensions(output)
    if (width, height) != (1440, 1050):
        raise RuntimeError(f"unexpected screenshot dimensions: {width}x{height}")
    return width, height


def capture(output_dir: Path) -> list[Path]:
    browser = _find_browser()
    output_dir = output_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="boho-analytics-demo-") as temporary:
        runtime = Path(temporary)
        port = _free_port()
        config_path = runtime / "platform.demo.toml"
        state_path = runtime / "demo.sqlite3"
        profile_root = runtime / "browser-profiles"
        config_path.write_text(
            _demo_config_text(state_path=state_path, fixture_path=DEMO_FIXTURE, port=port),
            encoding="utf-8",
        )
        env = _safe_environment()
        _run_cli(config_path, "config", "validate", env=env)
        _run_cli(config_path, "db", "init", env=env)
        _run_cli(config_path, "sync", "--start", "2026-07-01", "--end", "2026-07-04", env=env)

        server = subprocess.Popen(
            [sys.executable, "-m", "boho_analytics_platform", "--config", str(config_path), "serve"],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        generated: list[Path] = []
        try:
            _wait_for_server(port, server)
            output_dir.mkdir(parents=True, exist_ok=True)
            for filename, route in CAPTURES:
                temporary_png = runtime / filename
                dimensions = _capture(
                    browser,
                    profile_root,
                    temporary_png,
                    f"http://127.0.0.1:{port}{route}",
                    env=env,
                )
                destination = output_dir / filename
                shutil.copyfile(temporary_png, destination)
                generated.append(destination)
                print(f"{destination} {dimensions[0]}x{dimensions[1]}")
        finally:
            server.terminate()
            try:
                server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        return generated


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Capture GitHub-ready screenshots from the checked-in analytics demo fixture."
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=ROOT / "docs" / "images",
        help="PNG destination (default: docs/images)",
    )
    arguments = parser.parse_args()
    for required in (DEMO_CONFIG, DEMO_FIXTURE):
        if not required.is_file():
            raise SystemExit(f"required public demo file is missing: {required.relative_to(ROOT)}")
    try:
        capture(arguments.output_dir)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
