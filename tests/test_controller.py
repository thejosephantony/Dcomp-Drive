"""Testes isolados da API, com SQLite apenas como banco descartável de teste.

A configuração real do DComp Drive utiliza PostgreSQL; o objetivo aqui é
validar contratos HTTP sem precisar de um servidor externo.
"""

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from controller.database import get_session
from controller.main import app
from controller.models import Base, Node
from controller.monitor import recalculate_status


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)

    def override_session():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override_session
    try:
        # Sem lifespan: o loop de monitoramento de produção não inicia no teste.
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def node_payload(node_id="storage-1"):
    return {
        "node_id": node_id,
        "machine_id": node_id,
        "address": f"http://{node_id}:9000",
        "disks": [{"disk_id": f"{node_id}-disk0", "capacity_bytes": 1000, "used_bytes": 100}],
    }


def test_register_heartbeat_and_status(api):
    client, factory = api
    response = client.post("/nodes/register", json=node_payload())
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "HEALTHY"
    status = client.get("/status")
    assert status.status_code == 200
    assert status.json()["counts"]["HEALTHY"] == 1
    assert status.json()["nodes"][0]["used_bytes"] == 100

    response = client.post(
        "/nodes/storage-1/heartbeat",
        json={"disks": [{"disk_id": "storage-1-disk0", "capacity_bytes": 1000, "used_bytes": 250}]},
    )
    assert response.status_code == 200, response.text
    assert client.get("/status").json()["nodes"][0]["used_bytes"] == 250


def test_node_becomes_dead_and_returns(api):
    client, factory = api
    client.post("/nodes/register", json=node_payload())
    with factory() as session:
        node = session.get(Node, "storage-1")
        node.last_heartbeat = datetime.now(timezone.utc) - timedelta(seconds=120)
        session.commit()
    status = client.get("/status")
    assert status.json()["counts"]["DEAD"] == 1
    response = client.post(
        "/nodes/storage-1/heartbeat", json={"disks": node_payload()["disks"]}
    )
    assert response.status_code == 200
    assert client.get("/status").json()["counts"]["HEALTHY"] == 1


def test_validation_and_unknown_nodes(api):
    client, _ = api
    bad = node_payload()
    bad["disks"][0]["used_bytes"] = 1001
    assert client.post("/nodes/register", json=bad).status_code == 422
    assert client.post(
        "/nodes/unknown/heartbeat", json={"disks": node_payload()["disks"]}
    ).status_code == 404


def test_disk_id_cannot_belong_to_two_nodes(api):
    client, _ = api
    assert client.post("/nodes/register", json=node_payload()).status_code == 200
    other = node_payload("storage-2")
    other["disks"][0]["disk_id"] = "storage-1-disk0"
    assert client.post("/nodes/register", json=other).status_code == 409
