"""Transferência direta cliente -> Storage Nodes, sem bytes no controlador."""
import hashlib

import httpx
import pytest

from client.errors import CLIError, UnavailableError
from client.http_backend import HttpBackend


def test_direct_client_put_get_without_gateway(tmp_path, monkeypatch):
    monkeypatch.setenv("DCOMP_DIRECT_STORAGE", "true")
    payload = b"direto aos storages\x00" * 8
    sha = hashlib.sha256(payload).hexdigest()
    oid = "a" * 32
    state = {"objects": {}, "acks": set(), "committed": False, "requests": []}

    def handler(request: httpx.Request) -> httpx.Response:
        state["requests"].append((request.method, request.url.host, request.url.path))
        if request.url.host == "controller":
            if request.url.path == "/files/prepare":
                return httpx.Response(201, json={
                    "write_id": oid,
                    "targets": [
                        {"node_id": f"storage-{i}", "disk_id": f"storage-{i}-disk0",
                         "address": f"http://storage-{i}:9000/"}
                        for i in (1, 2, 3)
                    ],
                })
            if request.url.path == f"/files/uploads/{oid}/commit":
                assert len(state["acks"]) == 3
                state["committed"] = True
                return httpx.Response(200, json={"status": "COMMITTED"})
            if request.url.path == "/files/metadata":
                return httpx.Response(200, json={
                    "path": "/direto.bin", "version": 1, "version_id": oid,
                    "sha256": sha, "size_bytes": len(payload),
                    "status": "HEALTHY",
                    "replicas": [
                        {"node_id": f"storage-{i}", "address": f"http://storage-{i}:9000"}
                        for i in (1, 2, 3)
                    ],
                })
            return httpx.Response(404, json={"detail": "não encontrado"})
        node = request.url.host
        if request.url.path == f"/objects/{oid}" and request.method == "PUT":
            body = request.read()
            assert request.headers["X-Write-Id"] == oid
            assert request.headers["X-Expected-Sha256"] == sha
            assert body == payload
            state["objects"][node] = body
            state["acks"].add(node)
            return httpx.Response(201, json={
                "node_id": node, "sha256": sha, "size_bytes": len(body),
                "controller_acked": True,
            })
        if request.url.path == f"/objects/{oid}/verify":
            return httpx.Response(200, json={
                "match": True, "size_bytes": len(payload),
                "expected_sha256": sha,
            })
        if request.url.path == f"/objects/{oid}" and request.method == "GET":
            if node == "storage-1":
                return httpx.Response(500, json={"detail": "réplica indisponível"})
            return httpx.Response(
                200, content=state["objects"][node],
                headers={"X-Sha256": sha},
            )
        return httpx.Response(404, json={"detail": "não encontrado"})

    backend = HttpBackend(
        controller_url="http://controller:8000", transport=httpx.MockTransport(handler),
    )
    original = tmp_path / "origem.bin"
    original.write_bytes(payload)
    uploaded = backend.put(original, "/direto.bin")
    assert uploaded.sha256 == sha
    assert state["committed"]
    assert len(state["objects"]) == 3

    downloaded = tmp_path / "recuperado.bin"
    result = backend.get("/direto.bin", downloaded)
    assert downloaded.read_bytes() == payload
    assert result.sha256 == sha
    # NENHUMA requisição de conteúdo trafegou pelo controlador.
    assert all(path != "/files/content" for _, _, path in state["requests"])
    storage_puts = [r for r in state["requests"]
                    if r[0] == "PUT" and r[1].startswith("storage-")]
    assert len(storage_puts) == 3


def test_direct_client_rejects_untrusted_storage_address(monkeypatch):
    monkeypatch.setenv("DCOMP_DIRECT_STORAGE", "true")
    backend = HttpBackend()
    with pytest.raises(CLIError) as err:
        backend._storage_address("http://169.254.169.254/latest/")
    assert err.value.code == "SECURITY"
    assert backend._storage_address("http://storage-6:9000/") == "http://storage-6:9000"


def test_direct_get_does_not_replace_local_file_when_all_replicas_bad(tmp_path, monkeypatch):
    monkeypatch.setenv("DCOMP_DIRECT_STORAGE", "true")
    expected = b"arquivo real"
    oid = "b" * 32
    sha = hashlib.sha256(expected).hexdigest()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "controller":
            return httpx.Response(200, json={
                "path": "/falhou", "version": 1, "version_id": oid,
                "sha256": sha, "size_bytes": len(expected), "status": "HEALTHY",
                "replicas": [{"node_id": "storage-1", "address": "http://storage-1:9000"}],
            })
        return httpx.Response(200, content=b"CORROMPIDO")

    destination = tmp_path / "importante.txt"
    destination.write_bytes(b"NAO SOBRESCREVER")
    backend = HttpBackend(
        controller_url="http://controller:8000", transport=httpx.MockTransport(handler),
    )
    with pytest.raises(UnavailableError):
        backend.get("/falhou", destination)
    assert destination.read_bytes() == b"NAO SOBRESCREVER"
