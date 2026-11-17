"""Smoke tests da interface web sem depender de conexões reais com PostgreSQL."""
from fastapi.testclient import TestClient

from controller.main import app


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


def test_catalog_route_exists() -> None:
    assert any(getattr(route, "path", None) == "/files/catalog" for route in app.routes)
