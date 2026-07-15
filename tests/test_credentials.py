from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from boho_analytics_platform.credentials import CredentialError, ReferenceCredentialProvider


class CredentialTests(unittest.TestCase):
    def test_json_environment_credential_is_opaque_and_closeable(self):
        with patch.dict(os.environ, {"TEST_CREDENTIAL": '{"api_token":"sensitive"}'}, clear=False):
            lease = ReferenceCredentialProvider().acquire("env:TEST_CREDENTIAL")
            self.assertEqual(repr(lease), "<CredentialLease redacted>"); self.assertEqual(lease.read("api_token"), b"sensitive")
            lease.close()
            with self.assertRaises(CredentialError): lease.read("api_token")

    def test_missing_environment_credential_fails_without_value(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(CredentialError, "unavailable"): ReferenceCredentialProvider().acquire("env:MISSING")

    def test_none_reference_has_no_fields(self):
        with ReferenceCredentialProvider().acquire("none:test") as lease: self.assertIsNone(lease.read("value"))


if __name__ == "__main__": unittest.main()
