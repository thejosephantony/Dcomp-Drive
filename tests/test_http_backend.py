"""Contratos HTTP da CLI, sem precisar subir servidor nem Docker."""
import hashlib

import httpx
import pytest

from client.errors import CLIError
from client.http_backend import HttpBackend


def _sha(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def test_put_get_stat_and_status_over_http(tmp_path):
    data = b"CLI ligada ao gateway HTTP\x00" * 5
    state = {"body": b"", "calls": []}

    def metadata():
        return {
            "path": "/documento.bin",
            "version": 1,
            "size_bytes": len(data),
            "sha256": _sha(data),
            "status": "HEALTHY",
            "replicas": [{"node_id": f"storage-{i}"} for i in range(1, 4)],
        }

    def handler(request: httpx.Request):
        state["calls"].append((request.method, request.url.path))
        if request.url.path == "/files/content" and request.method == "PUT":
            assert request.url.params["path"] == "/documento.bin"
            state["body"] = request.read()
            return httpx.Response(201, json=metadata())
        if request.url.path == "/files/metadata":
            return httpx.Response(200, json=metadata())
        if request.url.path == "/files/content" and request.method == "GET":
            return httpx.Response(200, content=state["body"], headers={"X-Sha256": _sha(data)})
        if request.url.path == "/status":
            return httpx.Response(200, json={
                "nodes": [
                    {
                        "node_id": "storage-1", "state": "HEALTHY",
                        "capacity_bytes": 1000, "used_bytes": len(data),
                    }
                ],
                "counts": {"HEALTHY": 1},
            })
        return httpx.Response(404, json={"detail": "não encontrado"})

    backend = HttpBackend(
        controller_url="http://controller:8000",
        transport=httpx.MockTransport(handler),
    )
    original = tmp_path / "entrada.bin"
    original.write_bytes(data)

    uploaded = backend.put(original, "/documento.bin")
    assert uploaded.sha256 == _sha(data)
    assert uploaded.version == 1
    assert len(uploaded.replicas) == 3

    downloaded = tmp_path / "saida.bin"
    retrieved = backend.get("/documento.bin", downloaded)
    assert downloaded.read_bytes() == data
    assert retrieved.sha256 == uploaded.sha256
    assert backend.stat("/documento.bin").size == len(data)
    assert backend.status().nodes[0].node_id == "storage-1"
    assert ("PUT", "/files/content") in state["calls"]


def test_bad_download_never_overwrites_local_file(tmp_path):
    expected = b"original correto"
    existing = tmp_path / "arquivo.bin"
    existing.write_bytes(b"ARQUIVO LOCAL IMPORTANTE")

    def handler(request):
        if request.url.path == "/files/metadata":
            return httpx.Response(200, json={
                "path": "/arquivo.bin", "version": 1,
                "size_bytes": len(expected), "sha256": _sha(expected),
                "status": "HEALTHY", "replicas": [],
            })
        return httpx.Response(200, content=b"CORROMPIDO")

    backend = HttpBackend(transport=httpx.MockTransport(handler))
    with pytest.raises(CLIError) as err:
        backend.get("/arquivo.bin", existing)
    assert err.value.code == "INTEGRITY"
    assert existing.read_bytes() == b"ARQUIVO LOCAL IMPORTANTE"


def test_unavailable_directory_operations_and_http_errors(tmp_path):
    def handler(request):
        return httpx.Response(507, json={"detail": "nós insuficientes"})

    backend = HttpBackend(transport=httpx.MockTransport(handler))
    with pytest.raises(CLIError) as err:
        backend.stat("/sem-destinos")
    assert err.value.code == "507"

    with pytest.raises(CLIError) as err:
        backend.mkdir("/novo")
    assert err.value.code == "501"
