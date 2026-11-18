"""A porta pública nunca expõe os endpoints internos do controlador."""
from fastapi.testclient import TestClient
import controller.public_gateway as public


def test_public_gateway_only_allows_explicit_browser_routes():
    for method, path in [
        ("GET", "/"), ("GET", "/ui/app.js"),
        ("GET", "/status"), ("GET", "/files/content"),
        ("PUT", "/files/content"),
        ("DELETE", "/files/entry"),
        ("POST", "/files/move"),
        ("POST", "/demo/public/nodes/storage-1/offline"),
        ("POST", "/demo/public/nodes/storage-6/restore"),
    ]:
        assert public._allowed(method, path)

    for method, path in [
        ("POST", "/nodes/register"),
        ("POST", "/nodes/storage-1/heartbeat"),
        ("POST", "/files/prepare"),
        ("POST", "/demo/nodes/storage-1/offline"),
        ("POST", "/demo/files/corrupt"),
        ("GET", "/docs"), ("GET", "/openapi.json"),
        ("GET", "/_demo/restore"),
        ("GET", "/ui/../../etc/passwd"),
        ("POST", "/demo/public/nodes/storage-7/offline"),
        ("PUT", "/demo/public/nodes/storage-1/offline"),
    ]:
        assert not public._allowed(method, path)


def test_public_gateway_rejects_admin_paths_without_network():
    client = TestClient(public.app)
    for path in ["/nodes/register", "/files/prepare", "/docs", "/demo/nodes/storage-1/offline"]:
        response = client.post(path)
        assert response.status_code == 404
        assert response.headers.get("x-frame-options") == "DENY"
        assert response.headers.get("cache-control") == "no-store"


def test_public_gateway_rejects_payload_too_large_without_proxy(monkeypatch):
    monkeypatch.setattr(public, "_LIMIT_BYTES", 16)
    client = TestClient(public.app)
    response = client.put(
        "/files/content?path=/demo-too-big",
        content=b"x" * 17,
        headers={"content-type": "application/octet-stream"},
    )
    assert response.status_code == 413
