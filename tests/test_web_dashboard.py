"""Testes independentes do dashboard e do contrato HTTP do catálogo.

O catálogo é validado em uma aplicação isolada com SQLite temporário.
Isso evita depender do estado global do app usado por outros testes.
"""
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from controller.database import get_session
from controller.file_api import router as files_router
from controller.main import app
from controller.models import Base


def test_web_dashboard_and_assets_are_served() -> None:
    client = TestClient(app)

    home = client.get("/")
    assert home.status_code == 200
    assert "text/html" in home.headers["content-type"]
    assert "DComp Drive" in home.text
    assert "/ui/app.js" in home.text

    styles = client.get("/ui/styles.css")
    assert styles.status_code == 200
    assert ".nodes-grid" in styles.text

    script = client.get("/ui/app.js")
    assert script.status_code == 200
    assert "/files/catalog" in script.text
    assert 'id="upload-replicas"' in home.text
    assert 'id="namespace-path"' in home.text
    assert 'id="cluster-metrics"' in home.text
    assert 'id="requirements"' in home.text
    assert "/files/entries" in script.text
    assert "/files/directories" in script.text
    assert "/files/move" in script.text
    assert "replication_factor=" in script.text


def test_catalog_endpoint_returns_empty_list_when_no_files(tmp_path) -> None:
    """Testa uma requisição HTTP de verdade, não a lista interna app.routes."""
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'catalog.sqlite').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def session_override():
        with session_factory() as session:
            yield session

    isolated_app = FastAPI()
    isolated_app.include_router(files_router)
    isolated_app.dependency_overrides[get_session] = session_override
    try:
        response = TestClient(isolated_app).get("/files/catalog")
        assert response.status_code == 200, response.text
        assert response.json() == []
    finally:
        engine.dispose()



def test_demo_controls_fail_closed_by_default(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "false")
    monkeypatch.delenv("DEMO_ADMIN_TOKEN", raising=False)
    client = TestClient(app)
    assert client.get("/demo/config").json() == {"enabled": False}
    assert client.post("/demo/nodes/storage-1/offline").status_code == 404
    assert client.post("/demo/files/corrupt", json={"path": "/demo-test.txt"}).status_code == 404


def test_demo_requires_valid_token_and_restricts_corruption(monkeypatch) -> None:
    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_ADMIN_TOKEN", "a" * 40)
    client = TestClient(app)
    assert client.get("/demo/config").json() == {"enabled": True}
    assert client.post("/demo/nodes/storage-1/offline").status_code == 403
    headers = {"X-Demo-Token": "a" * 40}
    assert client.post(
        "/demo/files/corrupt",
        json={"path": "/documento-importante.pdf"},
        headers=headers,
    ).status_code == 422


def test_storage_failure_simulation_is_authenticated_and_reversible(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from storage.main import app as storage_app

    monkeypatch.setenv("DEMO_MODE", "true")
    monkeypatch.setenv("DEMO_ADMIN_TOKEN", "b" * 40)
    client = TestClient(storage_app)
    headers = {"X-Demo-Token": "b" * 40}
    assert client.post("/_demo/offline").status_code == 403
    try:
        paused = client.post("/_demo/offline", headers=headers)
        assert paused.status_code == 200
        assert paused.json()["paused"] is True
        assert client.get("/health").status_code == 503
    finally:
        restored = client.post("/_demo/restore", headers=headers)
        assert restored.status_code == 200
    assert client.get("/health").status_code == 200
