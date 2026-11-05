from __future__ import annotations

import importlib
import os
import sys
import unittest
from unittest.mock import MagicMock, patch


class ObjectStoreSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.environment = patch.dict(
            os.environ,
            {
                "KNOWLEDGE_OBJECT_STORE": "s3",
                "KNOWLEDGE_S3_BUCKET": "knowledge",
                "KNOWLEDGE_S3_SSE_MODE": "aws:kms",
                "KNOWLEDGE_S3_KMS_KEY_ID": "alias/semora-knowledge",
                "KNOWLEDGE_S3_REQUIRE_VERSIONING": "true",
            },
            clear=False,
        )
        self.environment.start()

    def tearDown(self) -> None:
        self.environment.stop()
        sys.modules.pop("app.object_store", None)

    @patch("boto3.client")
    def test_put_applies_kms_and_verifies_object(self, client_factory: MagicMock) -> None:
        client = client_factory.return_value
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        client.head_object.return_value = {
            "ServerSideEncryption": "aws:kms",
            "SSEKMSKeyId": "arn:aws:kms:region:account:key/example",
        }
        module = importlib.import_module("app.object_store")
        store = module.ObjectStore()

        uri = store.put_bytes("doc-1", "policy.pdf", b"content", "application/pdf")

        self.assertEqual(uri, "s3://knowledge/documents/doc-1/policy.pdf")
        kwargs = client.put_object.call_args.kwargs
        self.assertEqual(kwargs["ServerSideEncryption"], "aws:kms")
        self.assertEqual(kwargs["SSEKMSKeyId"], "alias/semora-knowledge")
        self.assertTrue(kwargs["BucketKeyEnabled"])

    @patch("boto3.client")
    def test_failed_encryption_verification_removes_object(self, client_factory: MagicMock) -> None:
        client = client_factory.return_value
        client.get_bucket_versioning.return_value = {"Status": "Enabled"}
        client.head_object.return_value = {}
        module = importlib.import_module("app.object_store")
        store = module.ObjectStore()

        with self.assertRaises(module.ObjectStoreError):
            store.put_bytes("doc-2", "policy.pdf", b"content")

        client.delete_object.assert_called_once_with(
            Bucket="knowledge", Key="documents/doc-2/policy.pdf"
        )

    @patch("boto3.client")
    def test_versioning_is_required_at_startup(self, client_factory: MagicMock) -> None:
        client = client_factory.return_value
        client.get_bucket_versioning.return_value = {"Status": "Suspended"}
        module = importlib.import_module("app.object_store")
        store = module.ObjectStore()

        with self.assertRaisesRegex(module.ObjectStoreError, "versioning enabled"):
            store.ensure_ready()

    @patch("boto3.client")
    def test_runtime_versioning_drift_blocks_write(self, client_factory: MagicMock) -> None:
        client = client_factory.return_value
        client.get_bucket_versioning.return_value = {"Status": "Suspended"}
        module = importlib.import_module("app.object_store")
        store = module.ObjectStore()

        with self.assertRaises(module.ObjectStoreError):
            store.put_bytes("doc-3", "policy.pdf", b"content")

        client.put_object.assert_not_called()
        client.delete_object.assert_not_called()


if __name__ == "__main__":
    unittest.main()
