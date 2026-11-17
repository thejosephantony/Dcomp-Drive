"""Proteções de operação que precisam funcionar antes da implantação na AWS."""
import hashlib

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from storage.store import ObjectStore


def test_replication_peer_must_be_allowlisted(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("ALLOWED_STORAGE_PEERS", "http://storage-2:9000")
    from storage import main as storage_api

    assert storage_api._validate_peer_url("http://storage-2:9000") == "http://storage-2:9000"
    with pytest.raises(HTTPException) as exc:
        storage_api._validate_peer_url("http://169.254.169.254:80")
    assert exc.value.status_code == 403
    with pytest.raises(HTTPException) as exc:
        storage_api._validate_peer_url("http://storage-2:9000/other")
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        storage_api._validate_peer_url("http://storage-2:9000@127.0.0.1:80")
    assert exc.value.status_code == 422


def test_corruption_endpoint_is_opt_in_only(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.delenv("ENABLE_TEST_ENDPOINTS", raising=False)
    from storage import main as storage_api

    isolated = ObjectStore(tmp_path, "test-node", "test-disk")
    monkeypatch.setattr(storage_api, "store", isolated)
    client = TestClient(storage_api.app)

    payload = b"teste de protecao"
    response = client.put(
        "/objects/security-object",
        content=payload,
        headers={"X-Expected-Sha256": hashlib.sha256(payload).hexdigest()},
    )
    assert response.status_code == 201, response.text

    response = client.post("/objects/security-object/corrupt")
    assert response.status_code == 404
    assert client.get("/objects/security-object").content == payload

    monkeypatch.setenv("ENABLE_TEST_ENDPOINTS", "true")
    assert client.post("/objects/security-object/corrupt").status_code == 200
    assert client.get("/objects/security-object").status_code == 500
