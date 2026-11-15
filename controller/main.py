"""API do controlador: descoberta, heartbeat e visibilidade inicial."""

import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.models import Disk, Node
from controller.monitor import monitor_forever, recalculate_status
from controller.schemas import ClusterStatus, Heartbeat, NodeRegistration, NodeStatus

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(monitor_forever())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title="DComp Drive Controller", version="0.1.0", lifespan=lifespan)


def _write_disks(session: Session, node: Node, reports) -> None:
    # Não permitir que um disco já vinculado a outro nó seja sequestrado por id.
    ids = [item.disk_id for item in reports]
    existing = session.scalars(select(Disk).where(Disk.disk_id.in_(ids))).all()
    for disk in existing:
        if disk.node_id != node.node_id:
            raise HTTPException(409, detail=f"disco já registrado em outro nó: {disk.disk_id}")
    by_id = {d.disk_id: d for d in node.disks}
    reported = set(ids)
    for disk in list(node.disks):
        if disk.disk_id not in reported:
            session.delete(disk)
    for report in reports:
        disk = by_id.get(report.disk_id)
        if disk is None:
            disk = Disk(disk_id=report.disk_id, node_id=node.node_id)
            session.add(disk)
        disk.capacity_bytes = report.capacity_bytes
        disk.used_bytes = report.used_bytes


@app.get("/health")
def health(session: Session = Depends(get_session)) -> dict[str, str]:
    session.execute(text("SELECT 1"))
    return {"status": "ok"}


@app.post("/nodes/register")
def register(payload: NodeRegistration, session: Session = Depends(get_session)) -> dict[str, str]:
    node = session.get(Node, payload.node_id)
    if node is None:
        node = Node(node_id=payload.node_id)
        session.add(node)
        session.flush()
    node.machine_id = payload.machine_id
    node.address = str(payload.address)
    node.last_heartbeat = datetime.now(timezone.utc)
    node.state = "HEALTHY"
    _write_disks(session, node, payload.disks)
    session.commit()
    return {"node_id": node.node_id, "state": node.state}


@app.post("/nodes/{node_id}/heartbeat")
def heartbeat(node_id: str, payload: Heartbeat, session: Session = Depends(get_session)) -> dict[str, str]:
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, detail=f"nó não registrado: {node_id}")
    node.last_heartbeat = datetime.now(timezone.utc)
    node.state = "HEALTHY"
    _write_disks(session, node, payload.disks)
    session.commit()
    return {"node_id": node.node_id, "state": node.state}


@app.get("/status", response_model=ClusterStatus)
def status(session: Session = Depends(get_session)) -> ClusterStatus:
    # Atualiza o estado também na consulta, independentemente do timer.
    recalculate_status(session)
    nodes = session.scalars(select(Node).order_by(Node.node_id)).all()
    output: list[NodeStatus] = []
    counts = {"HEALTHY": 0, "SUSPECT": 0, "DEAD": 0}
    for node in nodes:
        counts[node.state] = counts.get(node.state, 0) + 1
        output.append(NodeStatus(
            node_id=node.node_id, machine_id=node.machine_id,
            address=node.address, state=node.state,
            last_heartbeat=node.last_heartbeat.isoformat(),
            capacity_bytes=sum(d.capacity_bytes for d in node.disks),
            used_bytes=sum(d.used_bytes for d in node.disks),
        ))
    return ClusterStatus(nodes=output, counts=counts)
