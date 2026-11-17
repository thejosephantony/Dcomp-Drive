"""RF9: criptografia AES-256-GCM autenticada, com hash sobre texto claro."""
import asyncio
import base64
import hashlib

import pytest

from storage.crypto import MAGIC
from storage.store import CorruptedObjectError, ObjectStore


async def payload_stream(data: bytes):
    yield data[:4]
    yield data[4:]


def test_aes_gcm_at_rest_read_scrub_and_restart(tmp_path, monkeypatch):
    key = base64.b64encode(b"J" * 32).decode("ascii")
    monkeypatch.setenv("ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv("ENCRYPTION_KEY_BASE64", key)
    plain = b"arquivo cifrado\x00com bytes binarios"
    expected = hashlib.sha256(plain).hexdigest()

    store = ObjectStore(tmp_path, "storage-cipher", "storage-cipher-disk0")
    meta = asyncio.run(store.put("object-cipher", payload_stream(plain), expected))
    assert meta.encryption == "AES-256-GCM"
    disk = (tmp_path / "objects" / "object-cipher").read_bytes()
    assert disk.startswith(MAGIC)
    assert disk != plain
    assert plain not in disk
    assert len(disk) == len(plain) + 32

    first = ObjectStore(tmp_path, "storage-cipher", "storage-cipher-disk0")
    assert first.get_metadata("object-cipher").encryption == "AES-256-GCM"
    meta2, chunks = asyncio.run(first.get_stream("object-cipher"))
    async def consume():
        return b"".join([chunk async for chunk in chunks])
    assert asyncio.run(consume()) == plain
    assert meta2.sha256 == expected
    assert asyncio.run(first.scrub()).healthy_objects == 1
    assert asyncio.run(first.verify_object_integrity("object-cipher"))[0]

    first.corrupt_for_testing("object-cipher")
    scrubbed = asyncio.run(first.scrub())
    assert scrubbed.corrupted_objects == 1
    assert scrubbed.corrupted_ids == ["object-cipher"]
    assert not asyncio.run(first.verify_object_integrity("object-cipher"))[0]
    with pytest.raises(CorruptedObjectError):
        asyncio.run(first.get_stream("object-cipher"))


def test_encryption_configuration_and_legacy_plaintext(tmp_path, monkeypatch):
    monkeypatch.setenv("ENCRYPTION_ENABLED", "true")
    monkeypatch.delenv("ENCRYPTION_KEY_BASE64", raising=False)
    with pytest.raises(ValueError, match="ENCRYPTION_KEY_BASE64"):
        ObjectStore(tmp_path, "storage-keyless", "disk-keyless")

    monkeypatch.setenv("ENCRYPTION_ENABLED", "false")
    unencrypted = ObjectStore(tmp_path, "storage-plain", "disk-plain")
    plain = b"legado"
    meta = asyncio.run(unencrypted.put("legacy", payload_stream(plain)))
    assert meta.encryption == "none"

    monkeypatch.setenv("ENCRYPTION_ENABLED", "true")
    monkeypatch.setenv(
        "ENCRYPTION_KEY_BASE64", base64.b64encode(b"K" * 32).decode("ascii")
    )
    switched = ObjectStore(tmp_path, "storage-plain", "disk-plain")
    legacy, stream = asyncio.run(switched.get_stream("legacy"))
    async def consume():
        return b"".join([chunk async for chunk in stream])
    assert asyncio.run(consume()) == plain
    assert legacy.encryption == "none"
