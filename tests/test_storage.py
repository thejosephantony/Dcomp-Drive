"""Testes permanentes do storage real, sem Docker nem PostgreSQL.

Complementam a suite de controlador e os cenários de queda/retorno de nós.
Os testes HTTP usam TestClient sem ativar o lifespan, impedindo heartbeats
com serviços externos. Cada caso opera em um tmp_path descartável.
"""
import asyncio
import hashlib

import pytest
from fastapi.testclient import TestClient

from storage.store import (
    CorruptedObjectError,
    HashMismatchError,
    InsufficientStorageError,
    ObjectNotFoundError,
    ObjectStore,
)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def chunks(data: bytes):
    midpoint = len(data) // 2
    yield data[:midpoint]
    yield data[midpoint:]


async def read_all(store: ObjectStore, object_id: str, verify: bool = True) -> bytes:
    _metadata, stream = await store.get_stream(object_id, verify=verify)
    return b"".join([piece async for piece in stream])


@pytest.fixture
def store(tmp_path):
    return ObjectStore(
        data_dir=tmp_path,
        node_id="storage-test-1",
        disk_id="storage-test-1-disk0",
        capacity_bytes=4096,
        max_disk_usage_percent=100,
    )


def test_write_read_and_verify_hash(store):
    data = b"arquivo binario\x00" + bytes(range(256)) * 4
    meta = asyncio.run(store.put("obj-1", chunks(data), digest(data), len(data)))

    assert meta.sha256 == digest(data)
    assert meta.size_bytes == len(data)
    assert asyncio.run(read_all(store, "obj-1")) == data
    valid, expected, computed, _ = asyncio.run(store.verify_object_integrity("obj-1"))
    assert valid
    assert expected == computed == digest(data)


def test_wrong_sha_rejects_write_without_overwriting_previous(store):
    original = b"versao original"
    asyncio.run(store.put("obj-2", chunks(original), digest(original)))

    with pytest.raises(HashMismatchError):
        asyncio.run(store.put("obj-2", chunks(b"sobrescrita invalida"), digest(b"outro")))

    assert asyncio.run(read_all(store, "obj-2")) == original
    assert store.get_metadata("obj-2").sha256 == digest(original)


def test_interrupted_upload_preserves_previous_version(store):
    original = b"gravacao anterior"
    asyncio.run(store.put("obj-3", chunks(original), digest(original)))

    async def broken_stream():
        yield b"dados incompletos"
        raise ConnectionError("upload interrompido")

    with pytest.raises(ConnectionError, match="upload interrompido"):
        asyncio.run(store.put("obj-3", broken_stream()))

    assert asyncio.run(read_all(store, "obj-3")) == original
    assert list(store.tmp_dir.iterdir()) == []


def test_corruption_is_detected_by_read_and_scrubbing(store):
    data = b"conteudo integro"
    asyncio.run(store.put("obj-4", chunks(data), digest(data)))
    store.corrupt_for_testing("obj-4")

    with pytest.raises(CorruptedObjectError):
        asyncio.run(read_all(store, "obj-4"))

    report = asyncio.run(store.scrub())
    assert report.corrupted_objects == 1
    assert "obj-4" in report.corrupted_ids
    assert store.get_metadata("obj-4").status == "CORRUPTED"


def test_metadata_and_bytes_survive_store_restart(tmp_path):
    data = b"arquivo persistente" * 10
    first = ObjectStore(tmp_path, "node-reboot", "node-reboot-disk0")
    asyncio.run(first.put("obj-reboot", chunks(data), digest(data)))

    second = ObjectStore(tmp_path, "node-reboot", "node-reboot-disk0")
    assert second.get_metadata("obj-reboot").sha256 == digest(data)
    assert asyncio.run(read_all(second, "obj-reboot")) == data


def test_capacity_limit_rejects_upload_with_or_without_length(tmp_path):
    store = ObjectStore(
        tmp_path, "node-small", "node-small-disk0",
        capacity_bytes=100, max_disk_usage_percent=100,
    )
    payload = b"Z" * 101
    with pytest.raises(InsufficientStorageError):
        asyncio.run(store.put("too-large", chunks(payload), digest(payload), 101))
    with pytest.raises(InsufficientStorageError):
        asyncio.run(store.put("too-large", chunks(payload), digest(payload)))
    assert store.list_objects() == []
    assert list(store.tmp_dir.iterdir()) == []


def test_delete_removes_object_and_metadata(store):
    data = b"remover"
    asyncio.run(store.put("obj-delete", chunks(data), digest(data)))
    assert asyncio.run(store.delete("obj-delete")) is True
    with pytest.raises(ObjectNotFoundError):
        store.get_metadata("obj-delete")
    with pytest.raises(ObjectNotFoundError):
        asyncio.run(read_all(store, "obj-delete"))


def test_object_id_rejects_path_traversal(store):
    with pytest.raises(ValueError):
        asyncio.run(store.put("../escape", chunks(b"x")))
    with pytest.raises(ValueError):
        store.get_metadata("um/diretorio")


@pytest.fixture
def api(tmp_path, monkeypatch):
    # Importar depois do DATA_DIR para não inicializar o armazenamento global
    # de storage.main em /data quando o pytest rodar sem Docker.
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from storage import main as storage_api

    active_store = ObjectStore(
        tmp_path, "http-storage-1", "http-storage-1-disk0",
        capacity_bytes=4096, max_disk_usage_percent=100,
    )
    monkeypatch.setattr(storage_api, "store", active_store)
    # Sem context manager: não inicia tarefas de heartbeat nem de scrub.
    yield TestClient(storage_api.app), active_store


def test_http_put_get_verify_and_delete(api):
    client, _store = api
    data = b"HTTP upload e download" * 8
    response = client.put(
        "/objects/http-object",
        content=data,
        headers={"X-Expected-Sha256": digest(data)},
    )
    assert response.status_code == 201, response.text
    assert response.json()["size_bytes"] == len(data)
    assert response.json()["sha256"] == digest(data)
    assert response.json()["controller_acked"] is False

    response = client.get("/objects/http-object")
    assert response.status_code == 200
    assert response.content == data
    assert response.headers["X-Sha256"] == digest(data)

    verified = client.get("/objects/http-object/verify")
    assert verified.status_code == 200, verified.text
    assert verified.json()["match"] is True

    deleted = client.delete("/objects/http-object")
    assert deleted.status_code == 200
    assert client.get("/objects/http-object").status_code == 404


def test_http_rejects_bad_digest_and_corrupted_read(api):
    client, active_store = api
    data = b"HTTP corrupcao"
    response = client.put(
        "/objects/http-corrupted",
        content=data,
        headers={"X-Expected-Sha256": digest(b"errado")},
    )
    assert response.status_code == 422
    assert client.get("/objects/http-corrupted").status_code == 404

    response = client.put(
        "/objects/http-corrupted",
        content=data,
        headers={"X-Expected-Sha256": digest(data)},
    )
    assert response.status_code == 201
    active_store.corrupt_for_testing("http-corrupted")
    assert client.get("/objects/http-corrupted").status_code == 500

    scrubbed = client.post("/scrub")
    assert scrubbed.status_code == 200
    assert "http-corrupted" in scrubbed.json()["corrupted_ids"]


def test_scrub_notifies_controller_about_corruption(api, monkeypatch):
    """O endpoint /scrub informa ao controlador o ID físico corrompido."""
    import httpx
    from storage import main as storage_api

    client, active_store = api
    data = b"conteudo de teste para notificacao de integridade"
    response = client.put(
        "/objects/test-corrupted-report",
        content=data,
        headers={"X-Expected-Sha256": digest(data)},
    )
    assert response.status_code == 201
    active_store.corrupt_for_testing("test-corrupted-report")

    destinations = []

    class FakeControllerClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_):
            return False

        async def post(self, url, **kwargs):
            destinations.append(url)
            return httpx.Response(200, json={"status": "CORRUPTED"})

    monkeypatch.setattr(
        storage_api.httpx, "AsyncClient",
        lambda **kwargs: FakeControllerClient(),
    )
    report = client.post("/scrub")
    assert report.status_code == 200
    assert report.json()["corrupted_ids"] == ["test-corrupted-report"]
    assert destinations == [
        f"{storage_api.CONTROLLER_URL}/nodes/{storage_api.NODE_ID}/objects/test-corrupted-report/corruption"
    ]
