from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Iterator
from urllib.parse import urlparse

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from semora_s3_security import S3SecurityError, S3SecurityPolicy


class ObjectStoreError(RuntimeError):
    pass


class ObjectStore:
    def __init__(self) -> None:
        self.mode = os.getenv("KNOWLEDGE_OBJECT_STORE", "filesystem").strip().lower()
        if self.mode not in {"filesystem", "s3"}:
            raise RuntimeError("KNOWLEDGE_OBJECT_STORE must be filesystem or s3")
        self.data_dir = Path(os.getenv("KNOWLEDGE_DATA_DIR", "/data/uploads"))
        self.bucket = os.getenv("KNOWLEDGE_S3_BUCKET", "semora-knowledge")
        self.security = S3SecurityPolicy.from_env("KNOWLEDGE_S3")
        self._client = None
        if self.mode == "s3":
            self._client = boto3.client(
                "s3",
                endpoint_url=os.getenv("KNOWLEDGE_S3_ENDPOINT") or None,
                aws_access_key_id=os.getenv("KNOWLEDGE_S3_ACCESS_KEY") or None,
                aws_secret_access_key=os.getenv("KNOWLEDGE_S3_SECRET_KEY") or None,
                region_name=os.getenv("KNOWLEDGE_S3_REGION", "us-east-1"),
                config=Config(signature_version="s3v4", s3={"addressing_style": "path"}),
            )

    def ensure_ready(self) -> None:
        if self.mode == "filesystem":
            self.data_dir.mkdir(parents=True, exist_ok=True)
            return
        assert self._client is not None
        try:
            self._client.head_bucket(Bucket=self.bucket)
        except ClientError as exc:
            status = int(exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode", 0))
            code = str(exc.response.get("Error", {}).get("Code", ""))
            if status not in {403, 404} and code not in {"404", "NoSuchBucket", "NotFound"}:
                raise ObjectStoreError(f"cannot inspect S3 bucket: {code or status}") from exc
            if status == 403:
                raise ObjectStoreError("S3 bucket access denied") from exc
            try:
                self._client.create_bucket(Bucket=self.bucket)
            except ClientError as create_exc:
                create_code = str(create_exc.response.get("Error", {}).get("Code", ""))
                if create_code not in {"BucketAlreadyOwnedByYou", "BucketAlreadyExists"}:
                    raise ObjectStoreError(f"cannot create S3 bucket: {create_code}") from create_exc
        self.verify_security()

    def verify_security(self) -> None:
        if self.mode != "s3":
            return
        assert self._client is not None
        try:
            self._client.head_bucket(Bucket=self.bucket)
            self.security.verify_bucket(self._client, self.bucket)
        except (ClientError, S3SecurityError) as exc:
            raise ObjectStoreError(f"S3 security verification failed: {exc}") from exc

    def _key(self, document_id: str, file_name: str) -> str:
        safe_name = Path(file_name).name
        if not safe_name or safe_name in {".", ".."}:
            raise ObjectStoreError("invalid object file name")
        return f"documents/{document_id}/{safe_name}"

    def put_bytes(self, document_id: str, file_name: str, content: bytes, content_type: str | None = None) -> str:
        if self.mode == "filesystem":
            target_dir = self.data_dir / document_id
            target_dir.mkdir(parents=True, exist_ok=False)
            target = target_dir / Path(file_name).name
            target.write_bytes(content)
            return str(target)
        assert self._client is not None
        key = self._key(document_id, file_name)
        kwargs = {"Bucket": self.bucket, "Key": key, "Body": content}
        kwargs.update(self.security.put_options())
        if content_type:
            kwargs["ContentType"] = content_type
        uploaded = False
        try:
            # Re-check versioning before every protected write so a runtime
            # bucket-policy drift cannot silently weaken retention controls.
            self.verify_security()
            self._client.put_object(**kwargs)
            uploaded = True
            self.security.verify_object(self._client.head_object(Bucket=self.bucket, Key=key))
        except (ClientError, S3SecurityError, ObjectStoreError) as exc:
            # A failed encryption check must not leave an object that the
            # application will never reference. Deletion is best effort: the
            # original verification error is the useful failure to report.
            if uploaded:
                try:
                    self._client.delete_object(Bucket=self.bucket, Key=key)
                except ClientError:
                    pass
            raise ObjectStoreError("failed to store source object") from exc
        return f"s3://{self.bucket}/{key}"

    def put_file(self, document_id: str, source: Path, content_type: str | None = None) -> str:
        return self.put_bytes(document_id, source.name, source.read_bytes(), content_type)

    def delete(self, source_uri: str) -> None:
        if source_uri.startswith("s3://"):
            bucket, key = self._parse_s3_uri(source_uri)
            assert self._client is not None
            self._client.delete_object(Bucket=bucket, Key=key)
            return
        path = Path(source_uri)
        if path.is_file():
            path.unlink()
        try:
            path.parent.rmdir()
        except OSError:
            pass

    def read_bytes(self, source_uri: str) -> bytes:
        if not source_uri.startswith("s3://"):
            path = Path(source_uri)
            if not path.is_file():
                raise ObjectStoreError(f"source object is missing: {path.name}")
            return path.read_bytes()
        bucket, key = self._parse_s3_uri(source_uri)
        assert self._client is not None
        try:
            response = self._client.get_object(Bucket=bucket, Key=key)
            self.security.verify_object(response)
            return response["Body"].read()
        except (ClientError, S3SecurityError) as exc:
            raise ObjectStoreError("source object is missing or inaccessible") from exc

    @contextmanager
    def materialize(self, source_uri: str) -> Iterator[Path]:
        if not source_uri.startswith("s3://"):
            path = Path(source_uri)
            if not path.is_file():
                raise ObjectStoreError(f"source object is missing: {path.name}")
            yield path
            return
        _, key = self._parse_s3_uri(source_uri)
        with TemporaryDirectory(prefix="semora-knowledge-") as temporary:
            target = Path(temporary) / Path(key).name
            target.write_bytes(self.read_bytes(source_uri))
            yield target

    @staticmethod
    def _parse_s3_uri(source_uri: str) -> tuple[str, str]:
        parsed = urlparse(source_uri)
        bucket = parsed.netloc
        key = parsed.path.lstrip("/")
        if parsed.scheme != "s3" or not bucket or not key:
            raise ObjectStoreError("invalid S3 object URI")
        return bucket, key


object_store = ObjectStore()
