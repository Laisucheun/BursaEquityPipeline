"""R2 storage, materialize() and the local->R2 migration - fully offline.

No real endpoint is ever contacted: boto3 clients are wrapped in botocore's
Stubber (which intercepts before any HTTP), or replaced by an in-memory fake.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import boto3
import pytest
from botocore.exceptions import ClientError
from botocore.response import StreamingBody
from botocore.stub import ANY, Stubber

from bursa.config import Settings
from bursa.db.enums import DocSource
from bursa.db.models import Document
from bursa.storage import is_remote, materialize
from bursa.storage.base import content_key
from bursa.storage.migrate import migrate_to_r2, verify
from bursa.storage.r2 import R2BlobStore, parse_r2_uri, r2_uri

BUCKET = "test-bucket"


def _settings(tmp_path: Path) -> Settings:
    return Settings(_env_file=None, r2_bucket=BUCKET, blob_cache_dir=tmp_path / "cache")


class FakeS3:
    """Minimal in-memory S3 client covering the calls R2BlobStore makes."""

    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.calls: list[str] = []

    def _missing(self, op: str) -> ClientError:
        return ClientError(
            {"Error": {"Code": "404", "Message": "Not Found"},
             "ResponseMetadata": {"HTTPStatusCode": 404}},
            op,
        )

    def head_object(self, Bucket: str, Key: str) -> dict:
        self.calls.append("head_object")
        if Key not in self.objects:
            raise self._missing("HeadObject")
        data, meta = self.objects[Key]
        return {"ContentLength": len(data), "ETag": f'"{hashlib.md5(data).hexdigest()}"',
                "Metadata": dict(meta)}

    def put_object(self, Bucket: str, Key: str, Body, Metadata=None, **_kw) -> dict:
        self.calls.append("put_object")
        data = Body if isinstance(Body, bytes) else Body.read()
        self.objects[Key] = (data, dict(Metadata or {}))
        return {}

    def get_object(self, Bucket: str, Key: str) -> dict:
        self.calls.append("get_object")
        if Key not in self.objects:
            raise ClientError({"Error": {"Code": "NoSuchKey", "Message": "x"}}, "GetObject")
        return {"Body": io.BytesIO(self.objects[Key][0])}


def _pdf(tmp_path: Path, name: str, payload: bytes) -> tuple[Path, str]:
    path = tmp_path / "local" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return path, hashlib.sha256(payload).hexdigest()


def _doc(session, path: Path, sha: str) -> Document:
    doc = Document(
        source=DocSource.UPLOAD,
        original_filename=path.name,
        file_sha256=sha,
        file_size=path.stat().st_size,
        storage_path=str(path),
    )
    session.add(doc)
    session.flush()
    return doc


# -- R2BlobStore against a stubbed real boto3 client ----------------------------


def test_r2_put_get_exists_with_stubber(tmp_path: Path) -> None:
    client = boto3.client(
        "s3", endpoint_url="https://stub.invalid", region_name="auto",
        aws_access_key_id="x", aws_secret_access_key="y",
    )
    store = R2BlobStore(_settings(tmp_path), client=client)
    src, sha = _pdf(tmp_path, "a.pdf", b"%PDF-1.4 hello")
    key = content_key(sha)

    with Stubber(client) as stub:
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404,
                              expected_params={"Bucket": BUCKET, "Key": key})
        stub.add_response("put_object", {"ETag": '"e"'}, {
            "Bucket": BUCKET, "Key": key, "Body": ANY,
            "ContentType": "application/pdf", "Metadata": {"sha256": sha},
        })
        assert store.put(key, src) == f"r2://{BUCKET}/{key}"

        data = b"%PDF-1.4 hello"
        stub.add_response("get_object", {"Body": StreamingBody(io.BytesIO(data), len(data))},
                          {"Bucket": BUCKET, "Key": key})
        assert store.get_bytes(key) == data

        stub.add_response("head_object", {"ContentLength": len(data)}, {"Bucket": BUCKET, "Key": key})
        assert store.exists(key) is True
        stub.add_client_error("head_object", service_error_code="404", http_status_code=404)
        assert store.exists("nope.pdf") is False
        # A non-404 error (e.g. bad credentials) must not read as "absent".
        stub.add_client_error("head_object", service_error_code="403", http_status_code=403)
        with pytest.raises(ClientError):
            store.exists(key)
        stub.assert_no_pending_responses()


def test_r2_put_skips_identical_object(tmp_path: Path) -> None:
    fake = FakeS3()
    store = R2BlobStore(_settings(tmp_path), client=fake)
    src, sha = _pdf(tmp_path, "a.pdf", b"%PDF same")
    store.put(content_key(sha), src)
    store.put(content_key(sha), src)
    assert fake.calls.count("put_object") == 1


def test_r2_uri_roundtrip() -> None:
    assert parse_r2_uri(r2_uri("b", "aa/bb/x.pdf")) == ("b", "aa/bb/x.pdf")
    with pytest.raises(ValueError):
        parse_r2_uri("/local/path.pdf")


def test_r2_requires_credentials_without_client() -> None:
    with pytest.raises(ValueError, match="R2_ACCESS_KEY_ID"):
        R2BlobStore(Settings(_env_file=None, r2_bucket=BUCKET, r2_account_id="acct"))


# -- materialize ----------------------------------------------------------------


def test_materialize_local_is_passthrough(tmp_path: Path) -> None:
    path, _ = _pdf(tmp_path, "a.pdf", b"x")
    assert materialize(str(path)) == path
    assert not is_remote(str(path))


def test_materialize_r2_downloads_once_and_caches(tmp_path: Path) -> None:
    fake = FakeS3()
    store = R2BlobStore(_settings(tmp_path), client=fake)
    payload = b"%PDF remote"
    sha = hashlib.sha256(payload).hexdigest()
    key = content_key(sha)
    fake.objects[key] = (payload, {"sha256": sha})

    class Doc:
        storage_path = r2_uri(BUCKET, key)

    assert is_remote(Doc())
    first = materialize(Doc(), store=store)
    assert first.read_bytes() == payload
    assert first.is_relative_to(tmp_path / "cache")
    second = materialize(Doc(), store=store)
    assert second == first
    assert fake.calls.count("get_object") == 1  # second call served from cache


def test_materialize_r2_missing_object_is_nonexistent_path(tmp_path: Path) -> None:
    store = R2BlobStore(_settings(tmp_path), client=FakeS3())
    path = materialize(r2_uri(BUCKET, content_key("0" * 64)), store=store)
    assert not path.is_file()


def test_materialize_r2_rejects_corrupt_bytes(tmp_path: Path) -> None:
    fake = FakeS3()
    store = R2BlobStore(_settings(tmp_path), client=fake)
    key = content_key("a" * 64)
    fake.objects[key] = (b"not the right bytes", {})
    with pytest.raises(OSError, match="sha256 mismatch"):
        materialize(r2_uri(BUCKET, key), store=store)
    assert not (tmp_path / "cache" / key).exists()  # nothing half-published


# -- migration ------------------------------------------------------------------


def test_migration_dry_run_changes_nothing(session, tmp_path: Path) -> None:
    fake = FakeS3()
    store = R2BlobStore(_settings(tmp_path), client=fake)
    path, sha = _pdf(tmp_path, "a.pdf", b"%PDF one")
    doc = _doc(session, path, sha)
    session.commit()

    report = migrate_to_r2(session, store)  # default: dry run
    assert report.counts == {"would_upload": 1}
    assert fake.objects == {}
    assert "put_object" not in fake.calls
    assert not session.dirty and not session.new
    session.expire_all()
    assert session.get(Document, doc.id).storage_path == str(path)
    with pytest.raises(ValueError):
        migrate_to_r2(session, store, rewrite_paths=True)


def test_migration_execute_is_idempotent_and_never_deletes(session, tmp_path: Path) -> None:
    fake = FakeS3()
    store = R2BlobStore(_settings(tmp_path), client=fake)
    p1, s1 = _pdf(tmp_path, "a.pdf", b"%PDF one")
    p2, s2 = _pdf(tmp_path, "b.pdf", b"%PDF two")
    d1, d2 = _doc(session, p1, s1), _doc(session, p2, s2)

    first = migrate_to_r2(session, store, execute=True, limit=1)
    assert first.counts == {"uploaded": 1}
    second = migrate_to_r2(session, store, execute=True)
    assert second.counts == {"present": 1, "uploaded": 1}
    third = migrate_to_r2(session, store, execute=True)
    assert third.counts == {"present": 2}
    assert fake.calls.count("put_object") == 2
    assert fake.objects[content_key(s1)] == (b"%PDF one", {"sha256": s1})
    # Paths untouched without --rewrite-paths; local files kept.
    assert d1.storage_path == str(p1) and d2.storage_path == str(p2)
    assert p1.is_file() and p2.is_file()
    assert verify(session, store).counts == {"ok": 2}

    rewritten = migrate_to_r2(session, store, execute=True, rewrite_paths=True)
    assert rewritten.counts == {"present+rewritten": 2}
    assert d1.storage_path == r2_uri(BUCKET, content_key(s1))
    assert p1.is_file()
    assert migrate_to_r2(session, store, execute=True).counts == {"already_remote": 2}
    assert verify(session, store).counts == {"ok": 2}
    # The repointed document still materializes to the right bytes.
    assert materialize(d1, store=store).read_bytes() == b"%PDF one"


def test_migration_reports_missing_and_corrupt_local(session, tmp_path: Path) -> None:
    fake = FakeS3()
    store = R2BlobStore(_settings(tmp_path), client=fake)
    p1, s1 = _pdf(tmp_path, "a.pdf", b"%PDF one")
    _doc(session, p1, s1)
    p2, s2 = _pdf(tmp_path, "b.pdf", b"%PDF two")
    _doc(session, p2, s2)
    p2.write_bytes(b"%PDF tampered")  # same name, different bytes than recorded
    p3, s3 = _pdf(tmp_path, "c.pdf", b"%PDF three")
    _doc(session, p3, s3)
    p3.unlink()

    report = migrate_to_r2(session, store, execute=True, rewrite_paths=True)
    assert report.counts == {"uploaded+rewritten": 1, "hash_mismatch_local": 1, "missing_local": 1}
    assert not report.ok
    assert list(fake.objects) == [content_key(s1)]
    v = verify(session, store)
    assert v.counts == {"ok": 1, "missing_remote": 2}
