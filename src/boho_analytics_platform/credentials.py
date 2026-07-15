"""Opaque credential leases backed by environment or systemd credentials."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Mapping


class CredentialError(RuntimeError):
    pass


class MemoryCredentialLease:
    """A non-printable, explicitly closeable credential view."""

    __slots__ = ("_values", "_closed")

    def __init__(self, values: Mapping[str, bytes]) -> None:
        self._values = {key: bytearray(value) for key, value in values.items()}
        self._closed = False

    def __repr__(self) -> str:
        return "<CredentialLease redacted>"

    def read(self, field: str) -> bytes | None:
        if self._closed:
            raise CredentialError("credential lease is closed")
        value = self._values.get(field)
        return bytes(value) if value is not None else None

    def close(self) -> None:
        if self._closed:
            return
        for value in self._values.values():
            value[:] = b"\0" * len(value)
        self._values.clear()
        self._closed = True

    def __enter__(self) -> "MemoryCredentialLease":
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


def _decode_mapping(raw: bytes, label: str) -> dict[str, bytes]:
    stripped = raw.strip()
    if not stripped:
        raise CredentialError(f"credential reference is empty: {label}")
    if stripped.startswith(b"{"):
        try:
            parsed = json.loads(stripped.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CredentialError(f"credential reference is not valid UTF-8 JSON: {label}") from exc
        if not isinstance(parsed, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parsed.items()):
            raise CredentialError(f"credential JSON must contain only string fields: {label}")
        return {key: value.encode("utf-8") for key, value in parsed.items()}
    return {"value": stripped}


class ReferenceCredentialProvider:
    """Resolve ``env:``, ``systemd:``, and explicit ``none:`` references."""

    def acquire(self, reference: str) -> MemoryCredentialLease:
        scheme, separator, target = reference.partition(":")
        if not separator:
            raise CredentialError("invalid credential reference")
        if scheme == "none":
            return MemoryCredentialLease({})
        if scheme == "env":
            value = os.environ.get(target)
            if value is None:
                raise CredentialError(f"environment credential is unavailable: {target}")
            return MemoryCredentialLease(_decode_mapping(value.encode("utf-8"), reference))
        if scheme == "systemd":
            directory = os.environ.get("CREDENTIALS_DIRECTORY")
            if not directory:
                raise CredentialError("CREDENTIALS_DIRECTORY is unavailable")
            path = Path(directory) / target
            try:
                return MemoryCredentialLease(_decode_mapping(path.read_bytes(), reference))
            except OSError as exc:
                raise CredentialError(f"systemd credential is unavailable: {target}") from exc
        raise CredentialError(f"unsupported credential reference scheme: {scheme}")


def require_text(lease: MemoryCredentialLease, *fields: str) -> str:
    for field in fields:
        value = lease.read(field)
        if value:
            try:
                return value.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise CredentialError(f"credential field is not UTF-8: {field}") from exc
    raise CredentialError(f"credential is missing required field: {' or '.join(fields)}")
