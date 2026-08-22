#!/usr/bin/env python3
"""Fail closed unless normalized analytics state is on the reviewed disk."""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
from pathlib import Path


UUID_PATTERN = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


class StorageVerificationError(RuntimeError):
    """The runtime storage boundary does not match the reviewed layout."""


def _directory_without_symlink(path: Path, label: str) -> os.stat_result:
    if path.is_symlink():
        raise StorageVerificationError(f"{label} must not be a symlink")
    try:
        metadata = path.stat()
    except FileNotFoundError as exc:
        raise StorageVerificationError(f"{label} is missing") from exc
    if not stat.S_ISDIR(metadata.st_mode):
        raise StorageVerificationError(f"{label} must be a directory")
    return metadata


def verify_runtime_storage(
    state_path: str | Path,
    required_mountpoint: str | Path,
    required_filesystem_uuid: str,
) -> dict[str, object]:
    state = Path(state_path)
    mountpoint = Path(required_mountpoint)
    if not state.is_absolute() or not mountpoint.is_absolute():
        raise StorageVerificationError("storage paths must be absolute")
    if not UUID_PATTERN.fullmatch(required_filesystem_uuid):
        raise StorageVerificationError("required filesystem UUID is invalid")

    mount_metadata = _directory_without_symlink(
        mountpoint, "required mountpoint"
    )
    if not os.path.ismount(mountpoint):
        raise StorageVerificationError("required mountpoint is not mounted")

    marker = mountpoint / f".boho-storage-{required_filesystem_uuid}"
    if marker.is_symlink() or not marker.is_file():
        raise StorageVerificationError("required filesystem marker is missing")
    marker_metadata = marker.stat()
    if marker_metadata.st_dev != mount_metadata.st_dev:
        raise StorageVerificationError(
            "required filesystem marker is on the wrong device"
        )

    state_metadata = _directory_without_symlink(state, "analytics state path")
    if not os.path.ismount(state):
        raise StorageVerificationError(
            "analytics state path is not a dedicated mountpoint"
        )
    if state_metadata.st_dev != mount_metadata.st_dev:
        raise StorageVerificationError(
            "analytics state path is not on the required filesystem"
        )

    return {
        "ok": True,
        "state_path": str(state),
        "required_mountpoint": str(mountpoint),
        "required_filesystem_uuid": required_filesystem_uuid.casefold(),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-path", required=True)
    parser.add_argument("--required-mountpoint", required=True)
    parser.add_argument("--required-filesystem-uuid", required=True)
    args = parser.parse_args()
    try:
        result = verify_runtime_storage(
            args.state_path,
            args.required_mountpoint,
            args.required_filesystem_uuid,
        )
    except StorageVerificationError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
