"""Integração do gateway N=3 com banco real SQLite e Storage Nodes simulados.

Não substitui a validação com PostgreSQL/Docker: verifica seleção, ACKs,
gravação/consulta de bytes, falha parcial e recuperação por réplica alternativa.
"""
from datetime import datetime, timezone
import hashlib
from urllib.parse import urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from controller.database import get_session
from controller.file_api import acknowledge
from controller.file_models import Directory, FileEntry, FileVersion
from controller.file_schemas import ReplicaAck
from controller.main import app
from controller.models import Base, Disk, Node
import controller.file_transfer as transfer


class FakeStorageClient:
    def __init__(self, factory):
        self.factory = factory
        self.objects = {}
        self.fail_put_node = None
        self.read_attempts = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return False

    async def put(self, url, *, content, headers):
        node = urlsplit(url).hostname
        object_id = url.rsplit("/", 1)[-1]
        if node == self.fail_put_node:
            raise httpx.ConnectError("storage indisponível")
        data = bytes(content)
        digest = hashlib.sha256(data).hexdigest()
        if digest != headers["X-Expected-Sha256"]:
            return httpx.Response(422, json={"detail": "sha incorreto"})
        self.objects[(node, object_id)] = data
        acked = False
        if headers.get("X-Write-Id"):
            with self.factory() as session:
                result = acknowledge(
                    headers["X-Write-Id"],
                    ReplicaAck(
                        node_id=node,
                        disk_id=f"{node}-disk0",
                        size_bytes=len(data),
                        sha256=digest,
                    ),
                    session,
                )
                acked = result.status == "PENDING"
        return httpx.Response(
            201,
            json={
                "object_id": object_id, "node_id": node,
                "disk_id": f"{node}-disk0",
                "sha256": digest, "size_bytes": len(data),
                "controller_acked": acked,
            },
        )

    async def get(self, url):
        node = urlsplit(url).hostname
        object_id = url.rsplit("/", 1)[-1]
        self.read_attempts.append(node)
        contents = self.objects.get((node, object_id))
        if contents is None:
            return httpx.Response(404, json={"detail": "sem objeto"})
        return httpx.Response(200, content=contents)

    async def delete(self, url):
        node = urlsplit(url).hostname
        object_id = url.rsplit("/", 1)[-1]
        self.objects.pop((node, object_id), None)
        return httpx.Response(200, json={"deleted": True})


@pytest.fixture
def cluster(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite+pysqlite:///{(tmp_path / 'cluster.sqlite').as_posix()}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    with factory() as session:
        session.add(Directory(path="/", created_at=datetime.now(timezone.utc)))
        for i in range(1, 6):
            node_id = f"storage-{i}"
            node = Node(
                node_id=node_id,
                machine_id=node_id,
                state="HEALTHY",
                address=f"http://{node_id}:9000",
                last_heartbeat=datetime.now(timezone.utc),
            )
            node.disks.append(Disk(
                disk_id=f"{node_id}-disk0",
                node_id=node_id,
                capacity_bytes=1024 * 1024,
                used_bytes=0,
            ))
            session.add(node)
        session.commit()

    def override():
        with factory() as session:
            yield session

    app.dependency_overrides[get_session] = override
    fake = FakeStorageClient(factory)
    monkeypatch.setattr(transfer.httpx, "AsyncClient", lambda **kw: fake)
    try:
        yield TestClient(app), fake, factory
    finally:
        app.dependency_overrides.clear()
        engine.dispose()


def test_three_physical_replicas_then_download(cluster):
    client, fake, factory = cluster
    payload = b"ABC\x00" * 100

    uploaded = client.put("/files/content", params={"path": "/arquivo.bin"}, content=payload)
    assert uploaded.status_code == 201, uploaded.text
    body = uploaded.json()
    assert body["status"] == "HEALTHY"
    assert body["replication_factor"] == 3
    assert len(body["replicas"]) == 3

    with factory() as session:
        operation = session.get(FileVersion, session.query(FileVersion.version_id).first()[0])
        assert operation.status == "COMMITTED"
        assert operation.sha256 == hashlib.sha256(payload).hexdigest()
        assert len([key for key in fake.objects if key[1] == operation.version_id]) == 3

    downloaded = client.get("/files/content", params={"path": "/arquivo.bin"})
    assert downloaded.status_code == 200, downloaded.text
    assert downloaded.content == payload
    assert downloaded.headers["X-Sha256"] == hashlib.sha256(payload).hexdigest()


def test_failed_node_aborts_write_and_cleans_up(cluster):
    client, fake, factory = cluster
    fake.fail_put_node = "storage-2"
    response = client.put("/files/content", params={"path": "/falha.bin"}, content=b"teste")
    assert response.status_code == 502
    assert fake.objects == {}
    with factory() as session:
        versions = session.query(FileVersion).all()
        assert len(versions) == 1
        assert versions[0].status == "ABORTED"
    assert client.get("/files/metadata", params={"path": "/falha.bin"}).status_code == 404


def test_download_falls_back_when_first_replica_corrupt(cluster):
    client, fake, factory = cluster
    original = b"conteudo com copia confiavel" * 3
    response = client.put("/files/content", params={"path": "/fallback.bin"}, content=original)
    assert response.status_code == 201, response.text

    with factory() as session:
        write_id = session.query(FileVersion.version_id).one()[0]
    first_node = response.json()["replicas"][0]["node_id"]
    fake.objects[(first_node, write_id)] = b"CORROMPIDO"
    fake.read_attempts.clear()

    download = client.get("/files/content", params={"path": "/fallback.bin"})
    assert download.status_code == 200
    assert download.content == original
    assert fake.read_attempts[0] == first_node
    assert len(fake.read_attempts) >= 2
    assert fake.read_attempts[1] != first_node


def test_rejects_invalid_path_and_large_payload(cluster, monkeypatch):
    client, fake, factory = cluster
    assert client.put("/files/content", params={"path": "/a/../b"}, content=b"x").status_code == 422
    monkeypatch.setenv("MAX_TRANSFER_BYTES", "4")
    assert client.put("/files/content", params={"path": "/grande.bin"}, content=b"12345").status_code == 413
    with factory() as session:
        assert session.query(FileVersion).count() == 0


def test_overwrite_creates_distinct_physical_versions(cluster):
    client, fake, factory = cluster
    first = client.put("/files/content", params={"path": "/versoes.bin"}, content=b"primeira")
    assert first.status_code == 201
    second = client.put("/files/content", params={"path": "/versoes.bin"}, content=b"segunda")
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2
    with factory() as session:
        versions = session.query(FileVersion).order_by(FileVersion.version).all()
        assert len(versions) == 2
        assert len(set(v.version_id for v in versions)) == 2
        assert all(len([k for k in fake.objects if k[1] == v.version_id]) == 3 for v in versions)
    assert client.get("/files/content", params={"path": "/versoes.bin"}).content == b"segunda"


def test_auto_repair_restores_three_replicas(cluster, monkeypatch):
    import asyncio
    from controller import repair
    from controller.file_models import Replica

    client, fake, factory = cluster
    data = b"tolerancia a falha N=3" * 50
    uploaded = client.put("/files/content", params={"path": "/heal.bin"}, content=data)
    assert uploaded.status_code == 201
    failed = uploaded.json()["replicas"][0]["node_id"]
    with factory() as session:
        version = session.query(FileVersion).one()
        version_id = version.version_id
        node = session.get(Node, failed)
        node.state = "DEAD"
        session.commit()
    monkeypatch.setattr(repair, "SessionLocal", factory)

    before = client.get("/files/metadata", params={"path": "/heal.bin"}).json()
    assert before["status"] == "UNDER_REPLICATED"
    assert len(before["replicas"]) == 2
    assert asyncio.run(repair.repair_once()) is True

    after = client.get("/files/metadata", params={"path": "/heal.bin"}).json()
    assert after["status"] == "HEALTHY"
    assert len(after["replicas"]) == 3
    assert failed not in {x["node_id"] for x in after["replicas"]}
    assert len({x["machine_id"] for x in after["replicas"]}) == 3
    with factory() as session:
        reps = session.query(Replica).filter(Replica.version_id == version_id).all()
        assert len([r for r in reps if r.state == "ACKED"]) == 3
        assert len([r for r in reps if r.state == "LOST"]) == 1
    assert len([key for key in fake.objects if key[1] == version_id and key[0] != failed]) == 3
    assert client.get("/files/content", params={"path": "/heal.bin"}).content == data
    assert asyncio.run(repair.repair_once()) is False


def test_auto_repair_fails_closed_when_destination_unreachable(cluster, monkeypatch):
    import asyncio
    from controller import repair
    from controller.file_models import Replica

    client, fake, factory = cluster
    response = client.put("/files/content", params={"path": "/fails.bin"}, content=b"arquivo")
    assert response.status_code == 201
    failed = response.json()["replicas"][0]["node_id"]
    with factory() as session:
        session.get(Node, failed).state = "DEAD"
        session.commit()
    fake.fail_put_node = "storage-4"
    monkeypatch.setattr(repair, "SessionLocal", factory)

    assert asyncio.run(repair.repair_once()) is False
    meta = client.get("/files/metadata", params={"path": "/fails.bin"}).json()
    assert meta["status"] == "UNDER_REPLICATED"
    with factory() as session:
        assert session.query(Replica).filter(Replica.state == "REPAIRING").count() == 0
        assert session.query(Replica).filter(Replica.state == "ACKED").count() == 3

    fake.fail_put_node = None
    assert asyncio.run(repair.repair_once()) is True
    assert client.get("/files/metadata", params={"path": "/fails.bin"}).json()["status"] == "HEALTHY"


def test_auto_repair_falls_back_to_second_source(cluster, monkeypatch):
    import asyncio
    from controller import repair

    client, fake, factory = cluster
    data = b"origem valida" * 10
    response = client.put("/files/content", params={"path": "/origens.bin"}, content=data)
    assert response.status_code == 201
    replicas = response.json()["replicas"]
    dead = replicas[0]["node_id"]
    corrupted = replicas[1]["node_id"]
    with factory() as session:
        session.get(Node, dead).state = "DEAD"
        write_id = session.query(FileVersion.version_id).one()[0]
        session.commit()
    fake.objects[(corrupted, write_id)] = b"conteudo corrompido"
    fake.read_attempts.clear()
    monkeypatch.setattr(repair, "SessionLocal", factory)

    assert asyncio.run(repair.repair_once()) is True
    assert corrupted in fake.read_attempts
    assert replicas[2]["node_id"] in fake.read_attempts
    assert client.get("/files/metadata", params={"path": "/origens.bin"}).json()["status"] == "HEALTHY"


def test_auto_repair_waits_when_no_spare_machine(cluster, monkeypatch):
    import asyncio
    from controller import repair

    client, fake, factory = cluster
    response = client.put("/files/content", params={"path": "/sem-vaga.bin"}, content=b"abc")
    assert response.status_code == 201
    with factory() as session:
        for node_id in [response.json()["replicas"][0]["node_id"], "storage-4", "storage-5"]:
            session.get(Node, node_id).state = "DEAD"
        session.commit()
    monkeypatch.setattr(repair, "SessionLocal", factory)

    assert asyncio.run(repair.repair_once()) is False
    meta = client.get("/files/metadata", params={"path": "/sem-vaga.bin"}).json()
    assert meta["status"] == "UNDER_REPLICATED"
    assert len(meta["replicas"]) == 2


def test_auto_repair_reclaims_expired_claim_after_restart(cluster, monkeypatch):
    from datetime import timedelta
    from controller import repair
    from controller.file_models import Replica

    client, _fake, factory = cluster
    response = client.put("/files/content", params={"path": "/lease.bin"}, content=b"conteudo")
    assert response.status_code == 201
    with factory() as session:
        session.get(Node, response.json()["replicas"][0]["node_id"]).state = "DEAD"
        session.commit()

    with factory() as session:
        first = repair._claim_one(session)
    assert first is not None
    with factory() as session:
        assert repair._claim_one(session) is None
        stale = session.get(Replica, first.replica_id)
        stale.acked_at = repair.utcnow() - timedelta(seconds=300)
        session.commit()
    with factory() as session:
        second = repair._claim_one(session)
    assert second is not None
    assert second.replica_id != first.replica_id
    with factory() as session:
        assert session.get(Replica, first.replica_id) is None
        assert session.get(Replica, second.replica_id).state == "REPAIRING"


def test_corruption_report_drives_automatic_repair(cluster, monkeypatch):
    """Um scrub confirmado remove a cópia da contagem e aciona o reparo N=3."""
    import asyncio
    import controller.main as controller_api
    import controller.repair as repair
    from controller.file_models import Replica

    client, fake, factory = cluster
    data = b"teste de corrupcao controlada" * 2
    upload = client.put("/files/content", params={"path": "/corrompido.bin"}, content=data)
    assert upload.status_code == 201, upload.text

    first_node = upload.json()["replicas"][0]["node_id"]
    with factory() as session:
        write_id = session.query(FileVersion.version_id).one()[0]
    fake.objects[(first_node, write_id)] = b"CONTEUDO CORROMPIDO"

    def reported_metadata(url, *, timeout):
        assert url.endswith("/objects/" + write_id + "/meta")
        assert urlsplit(url).hostname == first_node
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "object_id": write_id,
            "sha256": hashlib.sha256(data).hexdigest(),
            "size_bytes": len(data),
            "status": "CORRUPTED",
        })

    monkeypatch.setattr(controller_api.httpx, "get", reported_metadata)
    notice = client.post(f"/nodes/{first_node}/objects/{write_id}/corruption")
    assert notice.status_code == 200, notice.text
    assert notice.json()["status"] == "CORRUPTED"

    with factory() as session:
        marked = session.query(Replica).filter_by(version_id=write_id, node_id=first_node).one()
        assert marked.state == "CORRUPTED"
    degraded = client.get("/files/metadata", params={"path": "/corrompido.bin"})
    assert degraded.json()["status"] == "UNDER_REPLICATED"
    assert len(degraded.json()["replicas"]) == 2

    # Repetir o relato não deve criar conflito nem gerar cópias duplicadas.
    again = client.post(f"/nodes/{first_node}/objects/{write_id}/corruption")
    assert again.status_code == 200
    assert again.json()["status"] == "ALREADY_CORRUPTED"

    monkeypatch.setattr(repair, "SessionLocal", factory)
    assert asyncio.run(repair.repair_once()) is True
    healthy = client.get("/files/metadata", params={"path": "/corrompido.bin"})
    assert healthy.status_code == 200
    assert healthy.json()["status"] == "HEALTHY"
    assert len(healthy.json()["replicas"]) == 3
    assert first_node not in {r["node_id"] for r in healthy.json()["replicas"]}
    # A copia defeituosa so e apagada apos 3 copias integras confirmadas.
    assert (first_node, write_id) not in fake.objects
    with factory() as session:
        assert session.query(Replica).filter_by(version_id=write_id, state="CORRUPTED").count() == 0
    download = client.get("/files/content", params={"path": "/corrompido.bin"})
    assert download.status_code == 200
    assert download.content == data


def test_corruption_report_rejects_healthy_replica(cluster, monkeypatch):
    """Um cliente não pode induzir reparo quando o nó diz que a cópia é íntegra."""
    import controller.main as controller_api
    from controller.file_models import Replica

    client, _fake, factory = cluster
    upload = client.put("/files/content", params={"path": "/integro.bin"}, content=b"OK")
    assert upload.status_code == 201
    first_node = upload.json()["replicas"][0]["node_id"]
    with factory() as session:
        write_id = session.query(FileVersion.version_id).one()[0]

    def healthy_metadata(url, *, timeout):
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "object_id": write_id,
            "sha256": hashlib.sha256(b"OK").hexdigest(),
            "size_bytes": 2,
            "status": "HEALTHY",
        })

    monkeypatch.setattr(controller_api.httpx, "get", healthy_metadata)
    response = client.post(f"/nodes/{first_node}/objects/{write_id}/corruption")
    assert response.status_code == 409
    with factory() as session:
        replica = session.query(Replica).filter_by(version_id=write_id, node_id=first_node).one()
        assert replica.state == "ACKED"



def test_rf1_namespace_full_lifecycle(cluster):
    """mkdir / ls / mv / rm / rmdir no PostgreSQL simulado e storage real fake."""
    client, fake, factory = cluster
    assert client.post("/files/directories", json={"path": "/trabalhos"}).status_code == 201
    assert client.post("/files/directories", json={"path": "/trabalhos/docs"}).status_code == 201

    root = client.get("/files/entries", params={"path": "/"})
    assert root.status_code == 200
    assert root.json() == [{"name": "trabalhos", "kind": "dir", "size": 0, "state": ""}]
    assert client.get("/files/entries", params={"path": "/trabalhos/docs"}).json() == []

    payload = b"rf1 - namespace"
    upload = client.put(
        "/files/content", params={"path": "/trabalhos/docs/relatorio.txt"}, content=payload,
    )
    assert upload.status_code == 201, upload.text
    before = client.get("/files/entries", params={"path": "/trabalhos/docs"})
    assert before.json() == [{
        "name": "relatorio.txt", "kind": "file", "size": len(payload),
        "state": "HEALTHY",
    }]

    with factory() as session:
        write_id = session.query(FileVersion.version_id).one()[0]
    copy_count = sum(1 for (_, obj_id) in fake.objects if obj_id == write_id)
    assert copy_count == 3

    moved = client.post("/files/move", json={
        "src": "/trabalhos/docs/relatorio.txt",
        "dst": "/trabalhos/renomeado.txt",
    })
    assert moved.status_code == 200, moved.text
    assert client.get("/files/metadata", params={"path": "/trabalhos/docs/relatorio.txt"}).status_code == 404
    assert client.get("/files/content", params={"path": "/trabalhos/renomeado.txt"}).content == payload
    assert sum(1 for (_, obj_id) in fake.objects if obj_id == write_id) == 3

    deleted = client.delete("/files/entry", params={"path": "/trabalhos/renomeado.txt"})
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["pending_cleanup"] == 0
    assert client.get("/files/metadata", params={"path": "/trabalhos/renomeado.txt"}).status_code == 404
    assert not any(obj_id == write_id for (_, obj_id) in fake.objects)

    assert client.delete("/files/directories", params={"path": "/trabalhos/docs"}).status_code == 200
    assert client.delete("/files/directories", params={"path": "/trabalhos"}).status_code == 200
    assert client.get("/files/entries", params={"path": "/"}).json() == []


def test_rf1_namespace_validates_paths_and_nonempty_dirs(cluster):
    client, _fake, _factory = cluster
    assert client.delete("/files/directories", params={"path": "/"}).status_code == 409
    assert client.post("/files/directories", json={"path": "/inexistente/docs"}).status_code == 404
    assert client.post("/files/directories", json={"path": "/a/../b"}).status_code == 422
    assert client.get("/files/entries", params={"path": "/nada"}).status_code == 404

    assert client.post("/files/directories", json={"path": "/dir"}).status_code == 201
    assert client.post("/files/directories", json={"path": "/dir"}).status_code == 409
    assert client.post("/files/directories", json={"path": "/dir/filha"}).status_code == 201
    assert client.delete("/files/directories", params={"path": "/dir"}).status_code == 409
    assert client.put("/files/content", params={"path": "/dir"}, content=b"X").status_code == 409

    payload = client.put("/files/content", params={"path": "/dir/arquivo"}, content=b"X")
    assert payload.status_code == 201
    assert client.delete("/files/directories", params={"path": "/dir"}).status_code == 409
    assert client.delete("/files/entry", params={"path": "/dir"}).status_code == 409
    assert client.post("/files/move", json={"src": "/dir", "dst": "/novo"}).status_code == 409
    assert client.post("/files/move", json={
        "src": "/dir/arquivo", "dst": "/dir/filha",
    }).status_code == 409
    assert client.post("/files/move", json={
        "src": "/dir/arquivo", "dst": "/inexistente/arquivo",
    }).status_code == 404


def test_rf1_remove_reclaims_all_published_versions(cluster):
    """rm libera as copias da versão atual e das versões anteriores."""
    client, fake, factory = cluster
    first = client.put("/files/content", params={"path": "/versoes-rf1"}, content=b"primeira")
    second = client.put("/files/content", params={"path": "/versoes-rf1"}, content=b"segunda")
    assert first.status_code == 201
    assert second.status_code == 201
    assert len(fake.objects) == 6

    result = client.delete("/files/entry", params={"path": "/versoes-rf1"})
    assert result.status_code == 200, result.text
    assert result.json()["pending_cleanup"] == 0
    assert fake.objects == {}
    with factory() as session:
        assert session.query(FileVersion).count() == 0
    assert client.get("/files/metadata", params={"path": "/versoes-rf1"}).status_code == 404



def test_drain_migrates_replicas_without_data_loss(cluster, monkeypatch):
    """RF7: nó em drain não é destino e tem suas cópias realocadas."""
    import asyncio
    import controller.repair as repair
    from controller.file_models import Replica

    client, fake, factory = cluster
    payload = b"conteudo de drain RF7"
    put = client.put("/files/content", params={"path": "/drain-rf7.bin"}, content=payload)
    assert put.status_code == 201, put.text
    first = put.json()["replicas"][0]["node_id"]
    with factory() as session:
        version_id = session.query(FileVersion.version_id).one()[0]

    drained = client.post(f"/nodes/{first}/drain")
    assert drained.status_code == 200, drained.text
    assert drained.json()["state"] == "DRAINING"

    report = client.get("/status")
    assert report.status_code == 200, report.text
    node = next(n for n in report.json()["nodes"] if n["node_id"] == first)
    assert node["state"] == "DRAINING"
    assert node["disks"][0]["free_bytes"] >= 0
    assert report.json()["repairs_in_progress"] == 0
    assert report.json()["files_by_state"]["UNDER_REPLICATED"] == 1

    # O heartbeat NÃO pode desfazer o drain em andamento.
    h = client.post(f"/nodes/{first}/heartbeat", json={"disks": [{
        "disk_id": f"{first}-disk0", "capacity_bytes": 1024 * 1024,
        "used_bytes": 0,
    }]})
    assert h.status_code == 200
    assert h.json()["state"] == "DRAINING"

    monkeypatch.setattr(repair, "SessionLocal", factory)
    assert asyncio.run(repair.repair_once()) is True

    state = client.get("/files/metadata", params={"path": "/drain-rf7.bin"})
    assert state.status_code == 200
    assert state.json()["status"] == "HEALTHY"
    assert len(state.json()["replicas"]) == 3
    assert first not in {r["node_id"] for r in state.json()["replicas"]}
    assert (first, version_id) not in fake.objects
    assert client.get("/files/content", params={"path": "/drain-rf7.bin"}).content == payload
    with factory() as session:
        assert session.query(Replica).filter_by(version_id=version_id).count() == 3


def test_rf5_repair_prioritizes_fewest_live_replicas(cluster):
    """RF5: com 1/3 o arquivo tem preferência sobre outro com 2/3."""
    import controller.repair as repair
    from controller.file_models import Replica

    client, _fake, factory = cluster
    assert client.put("/files/content", params={"path": "/menos-critico"}, content=b"a").status_code == 201
    assert client.put("/files/content", params={"path": "/mais-critico"}, content=b"b").status_code == 201
    with factory() as session:
        versions = {file.path: file.current_version_id
                    for file in session.query(FileEntry).all()}
        for path, missed in [("/menos-critico", 1), ("/mais-critico", 2)]:
            replicas = session.query(Replica).filter_by(version_id=versions[path]).order_by(Replica.node_id).all()
            for replica in replicas[:missed]:
                replica.state = "LOST"
        session.commit()

    with factory() as session:
        job = repair._claim_one(session)
        assert job is not None
        assert job.version_id == versions["/mais-critico"]


def test_rf11_metrics_exposes_file_states_and_repair_counts(cluster):
    client, _fake, _factory = cluster
    upload = client.put("/files/content", params={"path": "/metricas-rf11"}, content=b"OK")
    assert upload.status_code == 201
    metrics = client.get("/metrics")
    assert metrics.status_code == 200, metrics.text
    data = metrics.json()
    assert data["files_by_state"]["HEALTHY"] == 1
    assert data["replicas_by_state"]["ACKED"] == 3
    assert data["repairs_in_progress"] == 0
    assert data["requests_total"] >= 1
    assert data["upload_bytes_total"] >= 2
    assert data["request_latency_avg_ms"] >= 0



def test_rf6_returned_node_reuses_verified_replica(cluster, monkeypatch):
    """RF6: re-registro + inventário restabelecem uma cópia LOST íntegra."""
    import controller.main as api
    from controller.file_models import Replica

    client, fake, factory = cluster
    body = b"retorno de storage node"
    upload = client.put("/files/content", params={"path": "/retorno-rf6"}, content=body)
    assert upload.status_code == 201
    node_id = upload.json()["replicas"][0]["node_id"]
    with factory() as session:
        vid = session.query(FileVersion.version_id).one()[0]
        replica = session.query(Replica).filter_by(version_id=vid, node_id=node_id).one()
        replica.state = "LOST"
        node = session.get(Node, node_id)
        node.state = "DEAD"
        session.commit()

    registration = client.post("/nodes/register", json={
        "node_id": node_id, "machine_id": node_id,
        "address": f"http://{node_id}:9000",
        "disks": [{"disk_id": f"{node_id}-disk0",
                   "capacity_bytes": 1024 * 1024, "used_bytes": len(body)}],
    })
    assert registration.status_code == 200, registration.text
    assert registration.json()["state"] == "HEALTHY"

    def verify(url, *, timeout):
        assert url.endswith(f"/objects/{vid}/verify")
        assert urlsplit(url).hostname == node_id
        assert fake.objects[(node_id, vid)] == body
        return httpx.Response(200, request=httpx.Request("GET", url), json={
            "object_id": vid, "match": True,
            "expected_sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body),
        })

    monkeypatch.setattr(api.httpx, "get", verify)
    result = client.post(f"/nodes/{node_id}/reconcile", json={
        "disk_id": f"{node_id}-disk0",
        "objects": [{
            "object_id": vid, "sha256": hashlib.sha256(body).hexdigest(),
            "size_bytes": len(body), "status": "HEALTHY",
        }],
    })
    assert result.status_code == 200, result.text
    assert result.json()["restored"] == 1
    assert result.json()["discarded"] == 0
    with factory() as session:
        replica = session.query(Replica).filter_by(version_id=vid, node_id=node_id).one()
        assert replica.state == "ACKED"
    meta = client.get("/files/metadata", params={"path": "/retorno-rf6"})
    assert meta.status_code == 200
    assert meta.json()["status"] == "HEALTHY"


def test_rf6_rejects_unregistered_disk(cluster):
    client, _, _ = cluster
    result = client.post("/nodes/storage-1/reconcile", json={
        "disk_id": "outro-disco",
        "objects": [],
    })
    assert result.status_code == 409
