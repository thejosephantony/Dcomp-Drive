"""Detecção de nós SUSPECT/DEAD por ausência de heartbeats."""

import asyncio
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from controller.database import SessionLocal
from controller.models import Node

logger = logging.getLogger(__name__)

NODE_TIMEOUT_SECONDS = float(os.getenv("NODE_TIMEOUT_SECONDS", "6"))
MONITOR_INTERVAL_SECONDS = float(os.getenv("MONITOR_INTERVAL_SECONDS", "2"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def recalculate_status(session: Session, now: datetime | None = None) -> None:
    now = now or utcnow()
    nodes = session.scalars(select(Node)).all()
    for node in nodes:
        last = node.last_heartbeat
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        elapsed = max((now - last).total_seconds(), 0.0)
        # RF7: o nó segue em drain enquanto enviar heartbeats.
        if node.state == "DRAINING" and elapsed < NODE_TIMEOUT_SECONDS:
            continue
        next_state = (
            "DEAD"
            if elapsed >= NODE_TIMEOUT_SECONDS
            else "SUSPECT"
            if elapsed >= NODE_TIMEOUT_SECONDS / 2
            else "HEALTHY"
        )
        if node.state != next_state:
            logger.warning("node_state_changed node=%s from=%s to=%s", node.node_id, node.state, next_state)
            node.state = next_state
    session.commit()


async def monitor_forever() -> None:
    while True:
        await asyncio.sleep(MONITOR_INTERVAL_SECONDS)
        try:
            # Execute a consulta síncrona fora do event loop.
            await asyncio.to_thread(_monitor_once)
        except Exception:
            logger.exception("node_monitor_failed")


def _monitor_once() -> None:
    with SessionLocal() as session:
        recalculate_status(session)
