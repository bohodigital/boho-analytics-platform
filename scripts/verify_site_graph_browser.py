#!/usr/bin/env python3
"""Exercise Site Graph interactions in a disposable Chromium fixture runtime."""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import struct
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen
from urllib.parse import urlparse

from capture_dashboard_headless import (
    DEMO_FIXTURE,
    ROOT,
    _demo_config_text,
    _find_browser,
    _free_port,
    _run_cli,
    _safe_environment,
    _seed_demo_site_graph,
    _wait_for_server,
)


class DevTools:
    """Minimal masked WebSocket client for Chrome DevTools Protocol."""

    def __init__(self, url: str) -> None:
        parsed = urlparse(url)
        self.socket = socket.create_connection((parsed.hostname, parsed.port), timeout=5)
        key = base64.b64encode(os.urandom(16)).decode()
        request = (
            f"GET {parsed.path} HTTP/1.1\r\n"
            f"Host: {parsed.hostname}:{parsed.port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://127.0.0.1\r\n\r\n"
        )
        self.socket.sendall(request.encode())
        response = self._receive_until(b"\r\n\r\n")
        if not response.startswith(b"HTTP/1.1 101"):
            raise RuntimeError("browser rejected the DevTools WebSocket")
        self.next_id = 1
        self.exceptions: list[str] = []

    def close(self) -> None:
        self.socket.close()

    def _receive_until(self, marker: bytes) -> bytes:
        payload = bytearray()
        while marker not in payload:
            chunk = self.socket.recv(4096)
            if not chunk:
                raise RuntimeError("browser closed the DevTools connection")
            payload.extend(chunk)
        return bytes(payload)

    def _read_exact(self, length: int) -> bytes:
        payload = bytearray()
        while len(payload) < length:
            chunk = self.socket.recv(length - len(payload))
            if not chunk:
                raise RuntimeError("browser closed the DevTools connection")
            payload.extend(chunk)
        return bytes(payload)

    def _send_frame(self, payload: bytes, *, opcode: int = 1) -> None:
        mask = os.urandom(4)
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length < 65_536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        self.socket.sendall(bytes(header) + mask + masked)

    def _receive_json(self) -> dict[str, object]:
        while True:
            first, second = self._read_exact(2)
            opcode = first & 0x0F
            length = second & 0x7F
            if length == 126:
                length = struct.unpack(">H", self._read_exact(2))[0]
            elif length == 127:
                length = struct.unpack(">Q", self._read_exact(8))[0]
            mask = self._read_exact(4) if second & 0x80 else None
            payload = self._read_exact(length)
            if mask:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            if opcode == 8:
                raise RuntimeError("browser closed the DevTools connection")
            if opcode == 9:
                self._send_frame(payload, opcode=10)
                continue
            if opcode != 1:
                continue
            return json.loads(payload)

    def call(self, method: str, **params: object) -> dict[str, object]:
        request_id = self.next_id
        self.next_id += 1
        self._send_frame(json.dumps({"id": request_id, "method": method, "params": params}).encode())
        while True:
            message = self._receive_json()
            if message.get("method") == "Runtime.exceptionThrown":
                details = message.get("params", {}).get("exceptionDetails", {})
                self.exceptions.append(str(details.get("text", "JavaScript exception")))
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise RuntimeError(f"DevTools {method} failed: {message['error']}")
            return message.get("result", {})

    def evaluate(self, expression: str):
        result = self.call(
            "Runtime.evaluate",
            expression=expression,
            returnByValue=True,
            awaitPromise=True,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(f"browser evaluation failed: {result['exceptionDetails']}")
        return result["result"].get("value")


def _wait_for_devtools(port: int, process: subprocess.Popen[str]) -> str:
    endpoint = f"http://127.0.0.1:{port}/json/list"
    for _ in range(120):
        if process.poll() is not None:
            raise RuntimeError("Chromium exited before DevTools became available")
        try:
            with urlopen(endpoint, timeout=0.5) as response:  # noqa: S310 - fixed loopback URL
                targets = json.loads(response.read())
            page = next(item for item in targets if item.get("type") == "page")
            return str(page["webSocketDebuggerUrl"])
        except (URLError, TimeoutError, StopIteration):
            time.sleep(0.05)
    raise RuntimeError("timed out waiting for Chromium DevTools")


def _mouse(devtools: DevTools, event_type: str, point: dict[str, float], **extra: object) -> None:
    devtools.call(
        "Input.dispatchMouseEvent",
        type=event_type,
        x=point["x"],
        y=point["y"],
        **extra,
    )


def _click(devtools: DevTools, point: dict[str, float]) -> None:
    _mouse(devtools, "mouseMoved", point)
    _mouse(devtools, "mousePressed", point, button="left", buttons=1, clickCount=1)
    _mouse(devtools, "mouseReleased", point, button="left", buttons=0, clickCount=1)


def _press_escape(devtools: DevTools) -> None:
    for event_type in ("keyDown", "keyUp"):
        devtools.call(
            "Input.dispatchKeyEvent",
            type=event_type,
            key="Escape",
            code="Escape",
            windowsVirtualKeyCode=27,
            nativeVirtualKeyCode=27,
        )


def _assert(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def _exercise(devtools: DevTools, url: str) -> dict[str, object]:
    devtools.call("Runtime.enable")
    devtools.call("Page.enable")
    devtools.call("Page.navigate", url=url)
    for _ in range(100):
        if devtools.evaluate("document.readyState") == "complete":
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("Site Graph page did not finish loading")

    state = devtools.evaluate(
        """(() => {
          const svg=document.querySelector('.site-graph-svg');
          const node=document.querySelector('[data-graph-node][data-graph-node-route="/services/"]');
          const edge=document.querySelector('[data-graph-edge]');
          const sr=svg.getBoundingClientRect();
          const nr=node.getBoundingClientRect();
          const point=edge.getPointAtLength(edge.getTotalLength()/2).matrixTransform(edge.getScreenCTM());
          return {
            svg:{x:sr.x,y:sr.y,width:sr.width,height:sr.height},
            node:{x:nr.x+nr.width/2,y:nr.y+nr.height/2},
            edge:{x:point.x,y:point.y},
            transform:document.querySelector('[data-graph-viewport]').getAttribute('transform'),
            status:document.querySelector('[data-graph-zoom-status]').textContent
          };
        })()"""
    )
    _click(devtools, state["node"])
    node_selection = devtools.evaluate(
        """(() => {const s=document.querySelector('[data-site-graph-stage]');
        return {pinned:s.dataset.graphPinned,inspector:s.querySelector('[data-graph-inspector]').textContent};})()"""
    )
    _assert(node_selection["pinned"] == "true" and "Pinned page" in node_selection["inspector"],
            "ordinary pointer click did not pin a node")
    _press_escape(devtools)
    _click(devtools, state["edge"])
    edge_selection = devtools.evaluate(
        """(() => {const s=document.querySelector('[data-site-graph-stage]');
        return {pinned:s.dataset.graphPinned,inspector:s.querySelector('[data-graph-inspector]').textContent};})()"""
    )
    _assert(edge_selection["pinned"] == "true" and "Pinned edge" in edge_selection["inspector"],
            "ordinary pointer click did not pin an edge")
    _press_escape(devtools)

    zoom_button = devtools.evaluate(
        """(() => {const r=document.querySelector('[data-graph-zoom-in]').getBoundingClientRect();
        return {x:r.x+r.width/2,y:r.y+r.height/2};})()"""
    )
    _click(devtools, zoom_button)
    zoomed = devtools.evaluate(
        """({status:document.querySelector('[data-graph-zoom-status]').textContent,
        transform:document.querySelector('[data-graph-viewport]').getAttribute('transform')})"""
    )
    _assert(zoomed["status"] != state["status"] and zoomed["transform"] != state["transform"],
            "zoom-in control did not change the viewport")
    reset_button = devtools.evaluate(
        """(() => {const r=document.querySelector('[data-graph-zoom-reset]').getBoundingClientRect();
        return {x:r.x+r.width/2,y:r.y+r.height/2};})()"""
    )
    _click(devtools, reset_button)
    reset = devtools.evaluate(
        """({status:document.querySelector('[data-graph-zoom-status]').textContent,
        transform:document.querySelector('[data-graph-viewport]').getAttribute('transform')})"""
    )
    _assert(reset == {"status": state["status"], "transform": state["transform"]},
            "reset did not restore the fitted viewport")

    center = {
        "x": state["svg"]["x"] + state["svg"]["width"] / 2,
        "y": state["svg"]["y"] + state["svg"]["height"] / 2,
    }
    _mouse(devtools, "mouseWheel", center, deltaX=0, deltaY=-180)
    wheel = devtools.evaluate(
        """({status:document.querySelector('[data-graph-zoom-status]').textContent,
        transform:document.querySelector('[data-graph-viewport]').getAttribute('transform'),
        scrollY:window.scrollY})"""
    )
    _assert(wheel["status"] != state["status"], "wheel zoom did not change scale")

    _press_escape(devtools)
    drag_start = state["node"]
    _mouse(devtools, "mouseMoved", drag_start)
    _mouse(devtools, "mousePressed", drag_start, button="left", buttons=1, clickCount=1)
    drag_end = {"x": drag_start["x"] + 80, "y": drag_start["y"] - 40}
    _mouse(devtools, "mouseMoved", drag_end, button="left", buttons=1)
    _mouse(devtools, "mouseReleased", drag_end, button="left", buttons=0, clickCount=1)
    dragged = devtools.evaluate(
        """(() => {const s=document.querySelector('[data-site-graph-stage]');return {
        transform:document.querySelector('[data-graph-viewport]').getAttribute('transform'),
        dragging:s.dataset.graphDragging,suppress:s.dataset.graphSuppressClick,
        pinned:s.dataset.graphPinned};})()"""
    )
    _assert(dragged["transform"] != wheel["transform"], "dragging did not pan the viewport")
    _assert(dragged["dragging"] == "false" and dragged["pinned"] == "false",
            "drag completion leaked state or triggered selection")

    devtools.call(
        "Emulation.setDeviceMetricsOverride",
        width=390,
        height=844,
        deviceScaleFactor=1,
        mobile=False,
    )
    responsive = devtools.evaluate(
        """(() => {window.dispatchEvent(new Event('resize'));const r=document.querySelector('.site-graph-svg').getBoundingClientRect();
        return {innerWidth:window.innerWidth,overflow:document.documentElement.scrollWidth-window.innerWidth,
        svgWidth:r.width,status:document.querySelector('[data-graph-zoom-status]').textContent};})()"""
    )
    _assert(responsive["innerWidth"] == 390 and responsive["overflow"] == 0 and responsive["svgWidth"] > 0,
            "responsive Site Graph viewport is unusable or overflows horizontally")
    devtools.call("Runtime.evaluate", expression="0", returnByValue=True)
    _assert(not devtools.exceptions, f"browser JavaScript exceptions: {devtools.exceptions}")
    return {
        "node_click": "passed",
        "edge_click": "passed",
        "toolbar_zoom_reset": "passed",
        "wheel_zoom": "passed",
        "drag_pan_and_click_suppression": "passed",
        "responsive_390x844": "passed",
        "javascript_exceptions": 0,
    }


def verify(browser: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="boho-site-graph-browser-") as temporary:
        runtime = Path(temporary)
        app_port = _free_port()
        debug_port = _free_port()
        config_path = runtime / "platform.demo.toml"
        state_path = runtime / "demo.sqlite3"
        config_path.write_text(
            _demo_config_text(state_path=state_path, fixture_path=DEMO_FIXTURE, port=app_port),
            encoding="utf-8",
        )
        environment = _safe_environment()
        _run_cli(config_path, "config", "validate", env=environment)
        _run_cli(config_path, "db", "init", env=environment)
        _seed_demo_site_graph(state_path)
        server = subprocess.Popen(
            [sys.executable, "-B", "-m", "boho_analytics_platform", "--config", str(config_path), "serve"],
            cwd=ROOT,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        chrome = subprocess.Popen(
            [
                str(browser),
                "--headless=new",
                "--disable-gpu",
                "--disable-extensions",
                "--disable-default-apps",
                "--no-first-run",
                "--remote-allow-origins=*",
                f"--remote-debugging-port={debug_port}",
                f"--user-data-dir={runtime / 'browser-profile'}",
                "about:blank",
            ],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        devtools = None
        try:
            _wait_for_server(app_port, server)
            devtools = DevTools(_wait_for_devtools(debug_port, chrome))
            url = f"http://127.0.0.1:{app_port}/site-graph?site=fixture-static&page=%2Fservices%2F"
            with urlopen(url, timeout=3) as response:  # noqa: S310 - fixed loopback URL
                csp = response.headers.get("Content-Security-Policy", "")
            _assert("script-src 'self'" in csp and "unsafe-inline" not in csp,
                    "Site Graph CSP is missing or weakened")
            results = _exercise(devtools, url)
            results["csp"] = "passed"
            results["browser"] = browser.name
            return results
        finally:
            if devtools is not None:
                devtools.close()
            for process in (chrome, server):
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser", type=Path, help="Chromium-family browser executable")
    arguments = parser.parse_args()
    try:
        browser = arguments.browser.resolve() if arguments.browser else _find_browser()
        results = verify(browser)
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    print(json.dumps(results, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
