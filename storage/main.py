"""Nó de armazenamento inicial: saúde e registro/heartbeat automáticos.

A transferência de arquivos e a re-replicação NÃO estão implementadas neste PR.
"""

import asyncio
import logging
import os
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NODE_ID = os.environ.get("NODE_ID", "storage-1")
MACHINE_ID = os.environ.get("MACHINE_ID", NODE_ID)
STORAGE_ADDRESS = os.environ.get("STORAGE_ADDRESS", f"http://{NODE_ID}:9000")
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8000").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DISK_ID = os.environ.get("DISK_ID", f"{NODE_ID}-disk0")
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "2"))


def _disk_report() -> dict:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    usage = shutil.disk_usage(DATA_DIR)
    return {
        "disk_id": DISK_ID,
        "capacity_bytes": usage.total,
        "used_bytes": usage.used,
    }


async def report_forever() -> None:
    registered = False
    async with httpx.AsyncClient(timeout=3) as client:
        while True:
            try:
                if not registered:
                    response = await client.post(
                        f"{CONTROLLER_URL}/nodes/register",
                        json={
                            "node_id": NODE_ID,
                            "machine_id": MACHINE_ID,
                            "address": STORAGE_ADDRESS,
                            "disks": [_disk_report()],
                        },
                    )
                    response.raise_for_status()
                    registered = True
                    logger.info("registered node=%s controller=%s", NODE_ID, CONTROLLER_URL)
                else:
                    response = await client.post(
                        f"{CONTROLLER_URL}/nodes/{NODE_ID}/heartbeat",
                        json={"disks": [_disk_report()]},
                    )
                    if response.status_code == 404:
                        registered = False
                    else:
                        response.raise_for_status()
            except (httpx.HTTPError, OSError):
                registered = False
                logger.exception("heartbeat_or_registration_failed node=%s", NODE_ID)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    task = asyncio.create_task(report_forever())
    try:
        yield
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title=f"DComp Storage {NODE_ID}", version="0.1.0", lifespan=lifespan)


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "node_id": NODE_ID}
