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


def test_http_errors_and_unsupported_drain(tmp_path):
    def handler(request):
        return httpx.Response(507, json={"detail": "nós insuficientes"})

    backend = HttpBackend(transport=httpx.MockTransport(handler))
    with pytest.raises(CLIError) as err:
        backend.stat("/sem-destinos")
    assert err.value.code == "507"

    with pytest.raises(CLIError) as err:
        backend.drain("storage-1")
    assert err.value.code == "501"



def test_namespace_commands_over_http_transport():
    """O backend HTTP chama as rotas reais do controlador, sem cair no mock."""
    received = []

    def handler(request: httpx.Request):
        received.append((request.method, request.url.path))
        if request.url.path == "/files/entries":
            assert request.url.params["path"] == "/"
            return httpx.Response(200, json=[
                {"name": "equipe", "kind": "dir", "size": 0, "state": ""},
                {"name": "a.txt", "kind": "file", "size": 3, "state": "HEALTHY"},
            ])
        if request.url.path == "/files/directories" and request.method == "POST":
            assert request.read()
            return httpx.Response(201, json={"status": "CREATED"})
        if request.url.path == "/files/directories" and request.method == "DELETE":
            assert request.url.params["path"] == "/equipe"
            return httpx.Response(200, json={"status": "DELETED"})
        if request.url.path == "/files/move":
            assert request.method == "POST"
            return httpx.Response(200, json={"status": "MOVED"})
        if request.url.path == "/files/entry":
            assert request.method == "DELETE"
            assert request.url.params["path"] == "/b.txt"
            return httpx.Response(200, json={"status": "DELETED", "pending_cleanup": 0})
        return httpx.Response(404, json={"detail": "rota não esperada"})

    backend = HttpBackend(transport=httpx.MockTransport(handler))
    backend.mkdir("/equipe")
    listed = backend.ls("/")
    assert [entry.name for entry in listed] == ["equipe", "a.txt"]
    assert listed[1].size == 3 and listed[1].state == "HEALTHY"
    backend.mv("/a.txt", "/b.txt")
    backend.rm("/b.txt")
    backend.rmdir("/equipe")
    assert received == [
        ("POST", "/files/directories"),
        ("GET", "/files/entries"),
        ("POST", "/files/move"),
        ("DELETE", "/files/entry"),
        ("DELETE", "/files/directories"),
    ]


def test_remove_reports_pending_physical_cleanup():
    def handler(request: httpx.Request):
        return httpx.Response(200, json={"status": "DELETED", "pending_cleanup": 1})

    backend = HttpBackend(transport=httpx.MockTransport(handler))
    with pytest.raises(CLIError) as err:
        backend.rm("/exemplo")
    assert err.value.code == "CLEANUP"
    assert "removido do catálogo" in err.value.message
