from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.verify_runtime_storage import (
    StorageVerificationError,
    verify_runtime_storage,
)


FILESYSTEM_UUID = "13ae39fc-84e7-4fb8-b612-9f87a3d9635a"


class RuntimeStorageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.mountpoint = self.root / "large"
        self.state = self.root / "state"
        self.mountpoint.mkdir()
        self.state.mkdir()
        (self.mountpoint / f".boho-storage-{FILESYSTEM_UUID}").write_text(
            "reviewed\n", encoding="utf-8"
        )

    @patch("scripts.verify_runtime_storage.os.path.ismount", return_value=True)
    def test_matching_mount_boundary_is_accepted(self, _is_mount):
        result = verify_runtime_storage(
            self.state, self.mountpoint, FILESYSTEM_UUID
        )

        self.assertTrue(result["ok"])
        self.assertEqual(result["state_path"], str(self.state))

    @patch("scripts.verify_runtime_storage.os.path.ismount", return_value=True)
    def test_missing_filesystem_marker_is_rejected(self, _is_mount):
        (self.mountpoint / f".boho-storage-{FILESYSTEM_UUID}").unlink()

        with self.assertRaisesRegex(
            StorageVerificationError, "filesystem marker is missing"
        ):
            verify_runtime_storage(self.state, self.mountpoint, FILESYSTEM_UUID)

    @patch("scripts.verify_runtime_storage.os.path.ismount", return_value=True)
    def test_symlinked_state_path_is_rejected(self, _is_mount):
        target = self.root / "target"
        target.mkdir()
        self.state.rmdir()
        self.state.symlink_to(target, target_is_directory=True)

        with self.assertRaisesRegex(StorageVerificationError, "must not be a symlink"):
            verify_runtime_storage(self.state, self.mountpoint, FILESYSTEM_UUID)

    @patch("scripts.verify_runtime_storage.os.path.ismount", return_value=False)
    def test_unmounted_boundary_is_rejected(self, _is_mount):
        with self.assertRaisesRegex(StorageVerificationError, "is not mounted"):
            verify_runtime_storage(self.state, self.mountpoint, FILESYSTEM_UUID)


if __name__ == "__main__":
    unittest.main()
