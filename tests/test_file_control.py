"""Contrato de preparação, confirmação e publicação sem storage real.

Em produção, o controlador não deve confiar em ACKs enviados pelo cliente;
a verificação física precisa ser realizada nos nós de armazenamento.
"""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from controller.database import get_session
from controller.file_models import Directory, FileVersion
from controller.main import app
from controller.models import Base, Disk, Node


@pytest.fixture()
def api():
    engine = create_engine(
        "sqlite+pysqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Directory(path="/", created_at=datetime.now(timezone.utc)))
        now = datetime.now(timezone.utc)
        for i in range(1, 6):
            node_id = f"storage-{i}"
            node = Node(
                node_id=node_id, machine_id=node_id,
                address=f"http://{node_id}:9000",
                state="HEALTHY", last_heartbeat=now,
            )
            node.disks.append(Disk(
                disk_id=f"{node_id}-disk0", node_id=node_id,
                capacity_bytes=1000000, used_bytes=0,
            ))
            session.add(node)
        session.commit()

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    try:
        yield TestClient(app), factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def body(path="/exemplo.pdf", n=3, sha=None):
    return {
        "path": path,
        "size_bytes": 100,
        "sha256": sha or "a" * 64,
        "replication_factor": n,
    }


def prepare(client, payload=None):
    response = client.post("/files/prepare", json=payload or body())
    assert response.status_code == 201, response.text
    return response.json()


def acknowledge_all(client, operation):
    for target in operation["targets"]:
        response = client.post(
            f"/files/uploads/{operation['write_id']}/ack",
            json={
                "node_id": target["node_id"],
                "disk_id": target["disk_id"],
                "size_bytes": 100,
                "sha256": "a" * 64,
            },
        )
        assert response.status_code == 200, response.text


def test_prepare_unique_nodes_and_publish_only_after_ack(api):
    client, _ = api
    operation = prepare(client)
    assert len(operation["targets"]) == 3
    assert len({r["machine_id"] for r in operation["targets"]}) == 3
    assert len({r["disk_id"] for r in operation["targets"]}) == 3
    assert client.get("/files/metadata", params={"path": "/exemplo.pdf"}).status_code == 404
    assert client.post(f"/files/uploads/{operation['write_id']}/commit").status_code == 409
    acknowledge_all(client, operation)
    commit = client.post(f"/files/uploads/{operation['write_id']}/commit")
    assert commit.status_code == 200, commit.text
    assert commit.json()["acked"] == 3
    assert client.post(f"/files/uploads/{operation['write_id']}/commit").status_code == 200
    metadata = client.get("/files/metadata", params={"path": "/exemplo.pdf"})
    assert metadata.status_code == 200
    assert metadata.json()["version"] == 1
    assert metadata.json()["status"] == "HEALTHY"
    assert len(metadata.json()["replicas"]) == 3


def test_pending_overwrite_does_not_hide_last_committed_version(api):
    client, _ = api
    first = prepare(client)
    acknowledge_all(client, first)
    assert client.post(f"/files/uploads/{first['write_id']}/commit").status_code == 200
    second = prepare(client)
    assert second["version"] == 2
    assert client.get("/files/metadata", params={"path": "/exemplo.pdf"}).json()["version"] == 1
    assert client.post("/files/prepare", json=body()).status_code == 409
    assert client.post(f"/files/uploads/{second['write_id']}/abort").status_code == 200
    third = prepare(client)
    assert third["version"] == 3
    assert client.get("/files/metadata", params={"path": "/exemplo.pdf"}).json()["version"] == 1


def test_ack_must_match_expected_replica_and_digest(api):
    client, _ = api
    operation = prepare(client)
    ack = operation["targets"][0]
    data = {
        "node_id": ack["node_id"], "disk_id": ack["disk_id"],
        "size_bytes": 100, "sha256": "b" * 64,
    }
    response = client.post(f"/files/uploads/{operation['write_id']}/ack", json=data)
    assert response.status_code == 409
    data["sha256"] = "a" * 64
    data["node_id"] = "unknown"
    response = client.post(f"/files/uploads/{operation['write_id']}/ack", json=data)
    assert response.status_code == 404


def test_invalid_paths_and_insufficient_nodes(api):
    client, _ = api
    assert client.post("/files/prepare", json=body("../bad")).status_code == 422
    assert client.post("/files/prepare", json=body("/a/../bad")).status_code == 422
    assert client.post("/files/prepare", json=body("/pastas/inexistente")).status_code == 404
    assert client.post("/files/prepare", json=body(n=6)).status_code == 507


def test_expired_pending_can_be_replaced(api):
    client, factory = api
    operation = prepare(client)
    with factory() as session:
        version = session.get(FileVersion, operation["write_id"])
        version.expires_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        session.commit()
    new_operation = prepare(client)
    assert new_operation["version"] == 2
    with factory() as session:
        assert session.get(FileVersion, operation["write_id"]).status == "EXPIRED"


def test_each_replica_is_on_distinct_machine(api):
    client, factory = api
    with factory() as session:
        node = session.get(Node, "storage-2")
        node.machine_id = "storage-1"
        session.commit()
    operation = prepare(client, body(path="/outro.bin", n=4))
    assert len({t["machine_id"] for t in operation["targets"]}) == 4
    response = client.post("/files/prepare", json=body(path="/nao-cabe.bin", n=5))
    assert response.status_code == 507
