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
