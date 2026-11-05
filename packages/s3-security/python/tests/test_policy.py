from __future__ import annotations

import pytest

from semora_s3_security import S3SecurityError, S3SecurityPolicy


class FakeClient:
    def __init__(self, status: str | None) -> None:
        self.status = status

    def get_bucket_versioning(self, *, Bucket: str) -> dict:
        return {"Status": self.status} if self.status else {}


def test_kms_policy_builds_encrypted_write_options(monkeypatch) -> None:
    monkeypatch.setenv("TEST_S3_SSE_MODE", "aws:kms")
    monkeypatch.setenv("TEST_S3_KMS_KEY_ID", "alias/semora-bank")
    monkeypatch.setenv("TEST_S3_REQUIRE_VERSIONING", "true")
    policy = S3SecurityPolicy.from_env("TEST_S3")

    assert policy.put_options() == {
        "ServerSideEncryption": "aws:kms",
        "SSEKMSKeyId": "alias/semora-bank",
        "BucketKeyEnabled": True,
    }
    policy.verify_bucket(FakeClient("Enabled"), "bank-bucket")
    policy.verify_object(
        {"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": "arn:bank:kms:key/123"}
    )


def test_kms_policy_fails_closed_without_key(monkeypatch) -> None:
    monkeypatch.setenv("TEST_S3_SSE_MODE", "aws:kms")
    monkeypatch.delenv("TEST_S3_KMS_KEY_ID", raising=False)
    with pytest.raises(S3SecurityError, match="KMS_KEY_ID"):
        S3SecurityPolicy.from_env("TEST_S3")


def test_versioning_and_object_encryption_are_verified() -> None:
    policy = S3SecurityPolicy("AES256", require_versioning=True)
    with pytest.raises(S3SecurityError, match="versioning"):
        policy.verify_bucket(FakeClient(None), "bank-bucket")
    with pytest.raises(S3SecurityError, match="encryption mismatch"):
        policy.verify_object({})
