"""Testes dos nós de armazenamento (Storage Nodes):

Valida:
- Gravação com cálculo de SHA-256 e validação de hash esperado;
- Leitura com verificação de integridade e cabeçalhos ETag / X-Sha256;
- Detecção de corrupção na leitura e durante o scrubbing;
- Remoção e consulta de metadados;
- Rejeição de identificadores inválidos (prevenção de path traversal);
- Limitação de capacidade do disco (HTTP 507);
- Replicação direta entre nós (push e pull);
- Envio automático de ACK ao controlador quando informado write_id.
"""

import hashlib
from pathlib import Path
import pytest
from fastapi.testclient import TestClient
import httpx

from storage.main import app, store
from storage.schemas import ReplicateRequest
from storage.store import ObjectStore


@pytest.fixture()
def clean_store(tmp_path: Path):
    """Configura um ObjectStore isolado em diretório temporário para os testes."""
    original_data_dir = store.data_dir
    original_objects_dir = store.objects_dir
    original_meta_dir = store.meta_dir
    original_tmp_dir = store.tmp_dir
    original_cache = dict(store._cache)
    original_capacity = store._capacity_bytes
    original_node_id = store.node_id
    original_disk_id = store.disk_id

    # Redireciona o store global para o diretório de teste
    test_store = ObjectStore(
        data_dir=tmp_path,
        node_id="storage-test-1",
        disk_id="storage-test-1-disk0",
        capacity_bytes=1000000,
        max_disk_usage_percent=90,
    )
    store.data_dir = test_store.data_dir
    store.objects_dir = test_store.objects_dir
    store.meta_dir = test_store.meta_dir
    store.tmp_dir = test_store.tmp_dir
    store._cache = test_store._cache
    store._capacity_bytes = test_store._capacity_bytes
    store.max_disk_usage_percent = test_store.max_disk_usage_percent
    store.node_id = test_store.node_id
    store.disk_id = test_store.disk_id
    store.last_scrub_report = None

    client = TestClient(app)
    try:
        yield client, tmp_path
    finally:
        store.data_dir = original_data_dir
        store.objects_dir = original_objects_dir
        store.meta_dir = original_meta_dir
        store.tmp_dir = original_tmp_dir
        store._cache = original_cache
        store._capacity_bytes = original_capacity
        store.node_id = original_node_id
        store.disk_id = original_disk_id
        store._capacity_bytes = original_capacity


def test_health(clean_store):
    client, _ = clean_store
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_put_and_get_object(clean_store):
    client, _ = clean_store
    content = b"Conteudo de teste para o DComp Drive - No de Armazenamento"
    expected_sha = hashlib.sha256(content).hexdigest()

    # Gravação (PUT)
    put_resp = client.put(
        "/objects/obj-1",
        content=content,
        headers={"Content-Type": "application/octet-stream"},
    )
    assert put_resp.status_code == 201, put_resp.text
    data = put_resp.json()
    assert data["object_id"] == "obj-1"
    assert data["sha256"] == expected_sha
    assert data["size_bytes"] == len(content)

    # Leitura (GET)
    get_resp = client.get("/objects/obj-1")
    assert get_resp.status_code == 200
    assert get_resp.content == content
    assert get_resp.headers["x-sha256"] == expected_sha
    assert get_resp.headers["etag"] == f'"{expected_sha}"'
    assert int(get_resp.headers["content-length"]) == len(content)


def test_put_with_expected_sha256_match(clean_store):
    client, _ = clean_store
    content = b"Dados com validacao de integridade"
    expected_sha = hashlib.sha256(content).hexdigest()

    put_resp = client.put(
        "/objects/obj-hash-ok",
        content=content,
        headers={"X-Expected-Sha256": expected_sha},
    )
    assert put_resp.status_code == 201
    assert put_resp.json()["sha256"] == expected_sha


def test_put_with_expected_sha256_mismatch(clean_store):
    client, _ = clean_store
    content = b"Dados que vao falhar"
    wrong_sha = "0" * 64

    put_resp = client.put(
        "/objects/obj-hash-fail",
        content=content,
        headers={"X-Expected-Sha256": wrong_sha},
    )
    assert put_resp.status_code == 422
    assert "diverge" in put_resp.json()["detail"].lower()

    # O objeto não deve ter sido criado
    get_resp = client.get("/objects/obj-hash-fail")
    assert get_resp.status_code == 404


def test_get_nonexistent_object(clean_store):
    client, _ = clean_store
    resp = client.get("/objects/nao-existe")
    assert resp.status_code == 404


def test_head_and_meta_endpoints(clean_store):
    client, _ = clean_store
    content = b"Metadados sem corpo"
    sha = hashlib.sha256(content).hexdigest()

    client.put("/objects/obj-meta", content=content)

    # HEAD
    head_resp = client.head("/objects/obj-meta")
    assert head_resp.status_code == 200
    assert head_resp.headers["x-sha256"] == sha
    assert int(head_resp.headers["content-length"]) == len(content)
    assert head_resp.content == b""

    # GET /objects/{id}/meta
    meta_resp = client.get("/objects/obj-meta/meta")
    assert meta_resp.status_code == 200
    meta = meta_resp.json()
    assert meta["object_id"] == "obj-meta"
    assert meta["sha256"] == sha
    assert meta["status"] == "HEALTHY"


def test_delete_object(clean_store):
    client, _ = clean_store
    content = b"Objeto a ser removido"
    client.put("/objects/obj-del", content=content)

    status_before = client.get("/status").json()
    assert status_before["total_objects"] == 1
    assert status_before["used_bytes"] == len(content)

    del_resp = client.delete("/objects/obj-del")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted"] is True

    # Após exclusão, GET retorna 404
    assert client.get("/objects/obj-del").status_code == 404

    # Espaço utilizado atualizado
    status_after = client.get("/status").json()
    assert status_after["total_objects"] == 0
    assert status_after["used_bytes"] == 0


def test_corruption_detection_on_get(clean_store):
    client, _ = clean_store
    content = b"Conteudo original e integro"
    client.put("/objects/obj-corrupt", content=content)

    # Injeta corrupção
    corrupt_resp = client.post("/objects/obj-corrupt/corrupt")
    assert corrupt_resp.status_code == 200
    assert corrupt_resp.json()["corrupted"] is True

    # Tentativa de leitura deve falhar com HTTP 500
    get_resp = client.get("/objects/obj-corrupt")
    assert get_resp.status_code == 500
    assert "corrompido" in get_resp.json()["detail"].lower()

    # Metadados registram o status CORRUPTED
    meta_resp = client.get("/objects/obj-corrupt/meta")
    assert meta_resp.json()["status"] == "CORRUPTED"


def test_scrubbing_detects_corruption(clean_store):
    client, _ = clean_store
    client.put("/objects/obj-1", content=b"arquivo um")
    client.put("/objects/obj-2", content=b"arquivo dois")
    client.put("/objects/obj-3", content=b"arquivo tres")

    # Corrompe apenas o obj-2
    client.post("/objects/obj-2/corrupt")

    # Executa scrubbing
    scrub_resp = client.post("/scrub")
    assert scrub_resp.status_code == 200
    report = scrub_resp.json()
    assert report["scanned_objects"] == 3
    assert report["healthy_objects"] == 2
    assert report["corrupted_objects"] == 1
    assert "obj-2" in report["corrupted_ids"]

    # Consulta status de scrubbing
    status_resp = client.get("/scrub/status")
    assert status_resp.status_code == 200
    assert status_resp.json()["corrupted_objects"] == 1


def test_invalid_object_id_rejected(clean_store):
    client, _ = clean_store
    resp = client.put("/objects/invalido*id", content=b"ataque")
    assert resp.status_code == 422
    assert "object_id inválido" in resp.json()["detail"].lower()


def test_insufficient_storage(clean_store):
    client, _ = clean_store
    # Define capacidade bem pequena: 50 bytes com limite de 90% (45 bytes)
    store._capacity_bytes = 50

    resp = client.put("/objects/obj-grande", content=b"A" * 100)
    assert resp.status_code == 507
    assert "capacidade" in resp.json()["detail"].lower()


def test_storage_status(clean_store):
    client, _ = clean_store
    client.put("/objects/obj-status-1", content=b"12345")
    client.put("/objects/obj-status-2", content=b"67890")

    status = client.get("/status").json()
    assert status["node_id"] == "storage-test-1"
    assert status["total_objects"] == 2
    assert status["used_bytes"] == 10
    assert status["free_bytes"] > 0
    assert status["usage_percent"] >= 0.0


def test_replication_push_and_pull(clean_store, monkeypatch):
    """Testa replicação direta entre nós (push e pull)."""
    client, _ = clean_store
    content = b"Dados para teste de replicacao direta"
    sha = hashlib.sha256(content).hexdigest()

    # Grava objeto original
    client.put("/objects/obj-rep", content=content)

    pushed_requests = []

    class MockAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def put(self, url, content=None, headers=None, params=None):
            body = b""
            if hasattr(content, "__aiter__"):
                async for chunk in content:
                    body += chunk
            pushed_requests.append({"url": url, "body": body, "headers": headers})
            return httpx.Response(201, json={"status": "ok"})

        def stream(self, method, url):
            class StreamContext:
                async def __aenter__(ctx):
                    class MockStreamResponse:
                        status_code = 200
                        headers = {"X-Sha256": sha}

                        async def aiter_bytes(self):
                            yield content

                    return MockStreamResponse()

                async def __aexit__(ctx, *args):
                    pass

            return StreamContext()

    monkeypatch.setattr("storage.main.httpx.AsyncClient", MockAsyncClient)

    # 1. PUSH: envia objeto local para nó remoto
    resp_push = client.post(
        "/objects/obj-rep/replicate",
        json={"target_address": "http://storage-2:9000"},
    )
    assert resp_push.status_code == 200
    assert resp_push.json()["action"] == "pushed"
    assert resp_push.json()["sha256"] == sha
    assert len(pushed_requests) == 1
    assert pushed_requests[0]["body"] == content
    assert pushed_requests[0]["headers"]["X-Expected-Sha256"] == sha

    # 2. PULL: busca objeto de nó remoto e grava localmente
    resp_pull = client.post(
        "/objects/obj-pulled/replicate",
        json={"source_address": "http://storage-3:9000"},
    )
    assert resp_pull.status_code == 200
    assert resp_pull.json()["action"] == "pulled"
    assert resp_pull.json()["sha256"] == sha

    # Verifica que o objeto pullado foi gravado localmente
    get_resp = client.get("/objects/obj-pulled")
    assert get_resp.status_code == 200
    assert get_resp.content == content


def test_put_with_write_id_acks_controller(clean_store, monkeypatch):
    """Testa que ao fornecer write_id na gravação, o ACK é enviado ao controlador."""
    client, _ = clean_store

    acked_calls = []

    async def fake_notify(write_id: str, sha256: str, size_bytes: int) -> bool:
        acked_calls.append({"write_id": write_id, "sha256": sha256, "size_bytes": size_bytes})
        return True

    monkeypatch.setattr("storage.main.notify_controller_ack", fake_notify)

    content = b"Conteudo ack"
    resp = client.put(
        "/objects/obj-ack",
        content=content,
        params={"write_id": "op-write-123"},
    )
    assert resp.status_code == 201
    assert resp.json()["controller_acked"] is True
    assert len(acked_calls) == 1
    assert acked_calls[0]["write_id"] == "op-write-123"
    assert acked_calls[0]["size_bytes"] == len(content)
