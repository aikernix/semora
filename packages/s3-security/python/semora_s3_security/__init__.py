from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


class S3SecurityError(RuntimeError):
    pass


def _enabled(value: str | None, default: bool) -> bool:
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class S3SecurityPolicy:
    mode: str = "disabled"
    kms_key_id: str = ""
    require_versioning: bool = False
    verify_object_encryption: bool = True

    @classmethod
    def from_env(
        cls,
        prefix: str,
        *,
        default_mode: str = "disabled",
        default_require_versioning: bool = False,
    ) -> "S3SecurityPolicy":
        raw_mode = os.getenv(f"{prefix}_SSE_MODE", default_mode).strip().lower()
        modes = {"disabled": "disabled", "aes256": "AES256", "aws:kms": "aws:kms"}
        if raw_mode not in modes:
            raise S3SecurityError(f"{prefix}_SSE_MODE must be disabled, AES256 or aws:kms")
        mode = modes[raw_mode]
        kms_key_id = os.getenv(f"{prefix}_KMS_KEY_ID", "").strip()
        if mode == "aws:kms" and not kms_key_id:
            raise S3SecurityError(f"{prefix}_KMS_KEY_ID is required for aws:kms")
        return cls(
            mode=mode,
            kms_key_id=kms_key_id,
            require_versioning=_enabled(
                os.getenv(f"{prefix}_REQUIRE_VERSIONING"), default_require_versioning
            ),
            verify_object_encryption=_enabled(
                os.getenv(f"{prefix}_VERIFY_ENCRYPTION"), mode != "disabled"
            ),
        )

    def put_options(self) -> dict[str, Any]:
        if self.mode == "disabled":
            return {}
        options: dict[str, Any] = {"ServerSideEncryption": self.mode}
        if self.mode == "aws:kms":
            options.update(SSEKMSKeyId=self.kms_key_id, BucketKeyEnabled=True)
        return options

    def verify_bucket(self, client: Any, bucket: str) -> None:
        if not self.require_versioning:
            return
        response = client.get_bucket_versioning(Bucket=bucket)
        if response.get("Status") != "Enabled":
            raise S3SecurityError(f"S3 bucket {bucket} must have versioning enabled")

    def verify_object(self, response: dict[str, Any]) -> None:
        if not self.verify_object_encryption or self.mode == "disabled":
            return
        actual = response.get("ServerSideEncryption")
        if actual != self.mode:
            raise S3SecurityError(
                f"S3 object encryption mismatch: expected {self.mode}, received {actual or 'none'}"
            )
        if self.mode == "aws:kms" and not response.get("SSEKMSKeyId"):
            raise S3SecurityError("S3 object is missing a KMS key identifier")

    def public_status(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "kms_key_configured": bool(self.kms_key_id),
            "versioning_required": self.require_versioning,
            "verification_enabled": self.verify_object_encryption,
        }
