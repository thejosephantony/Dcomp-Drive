"""Nó de armazenamento (Storage Node) do DComp Drive.

Responsabilidades:
- Persistência física de dados em disco;
- Cálculo e validação de integridade por SHA-256 em escrita e leitura;
- Detecção e prevenção de corrupção e leituras parciais;
- Verificação periódica de integridade (Scrubbing);
- Replicação e cópia direta de objetos entre nós de armazenamento;
- Notificação de ACKs e heartbeats ao controlador central.
"""

import asyncio
from contextlib import asynccontextmanager
import logging
import os
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from storage.schemas import (
    ObjectMetadata,
    PutObjectResponse,
    ReplicateRequest,
    ReplicateResponse,
    ScrubReport,
    StorageStatus,
)
from storage.store import (
    CorruptedObjectError,
    HashMismatchError,
    InsufficientStorageError,
    ObjectNotFoundError,
    ObjectStore,
    StorageError,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

NODE_ID = os.environ.get("NODE_ID", "storage-1")
MACHINE_ID = os.environ.get("MACHINE_ID", NODE_ID)
STORAGE_ADDRESS = os.environ.get("STORAGE_ADDRESS", f"http://{NODE_ID}:9000")
CONTROLLER_URL = os.environ.get("CONTROLLER_URL", "http://controller:8000").rstrip("/")
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DISK_ID = os.environ.get("DISK_ID", f"{NODE_ID}-disk0")
HEARTBEAT_INTERVAL_SECONDS = float(os.environ.get("HEARTBEAT_INTERVAL_SECONDS", "2"))
SCRUB_INTERVAL_SECONDS = float(os.environ.get("SCRUB_INTERVAL_SECONDS", "0"))
MAX_DISK_USAGE_PERCENT = int(os.environ.get("MAX_DISK_USAGE_PERCENT", "90"))
STORAGE_CAPACITY_BYTES = (
    int(os.environ["STORAGE_CAPACITY_BYTES"])
    if "STORAGE_CAPACITY_BYTES" in os.environ
    else None
)

store = ObjectStore(
    data_dir=DATA_DIR,
    node_id=NODE_ID,
    disk_id=DISK_ID,
    capacity_bytes=STORAGE_CAPACITY_BYTES,
    max_disk_usage_percent=MAX_DISK_USAGE_PERCENT,
)


def _disk_report() -> dict:
    return {
        "disk_id": DISK_ID,
        "capacity_bytes": store.get_capacity_bytes(),
        "used_bytes": store.get_used_bytes(),
    }


async def notify_controller_ack(write_id: str, sha256: str, size_bytes: int) -> bool:
    """Envia confirmação (ACK) física de réplica persistida ao controlador."""
    try:
        async with httpx.AsyncClient(timeout=3) as client:
            resp = await client.post(
                f"{CONTROLLER_URL}/files/uploads/{write_id}/ack",
                json={
                    "node_id": store.node_id,
                    "disk_id": store.disk_id,
                    "sha256": sha256,
                    "size_bytes": size_bytes,
                },
            )
            return resp.status_code == 200
    except (httpx.HTTPError, OSError) as exc:
        logger.warning("Falha ao enviar ACK ao controlador (write_id=%s): %s", write_id, exc)
        return False


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


async def scrub_forever() -> None:
    """Execução periódica em segundo plano de scrubbing, quando configurada."""
    if SCRUB_INTERVAL_SECONDS <= 0:
        return
    while True:
        await asyncio.sleep(SCRUB_INTERVAL_SECONDS)
        try:
            await store.scrub()
        except Exception:
            logger.exception("periodic_scrub_failed node=%s", NODE_ID)


@asynccontextmanager
async def lifespan(app: FastAPI):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    report_task = asyncio.create_task(report_forever())
    scrub_task = asyncio.create_task(scrub_forever())
    try:
        yield
    finally:
        report_task.cancel()
        scrub_task.cancel()
        for task in (report_task, scrub_task):
            try:
                await task
            except asyncio.CancelledError:
                pass


app = FastAPI(title=f"DComp Storage {NODE_ID}", version="0.2.0", lifespan=lifespan)


@app.exception_handler(ObjectNotFoundError)
async def handle_not_found(request: Request, exc: ObjectNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(HashMismatchError)
async def handle_hash_mismatch(request: Request, exc: HashMismatchError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(CorruptedObjectError)
async def handle_corrupted(request: Request, exc: CorruptedObjectError):
    return JSONResponse(status_code=500, content={"detail": str(exc)})


@app.exception_handler(InsufficientStorageError)
async def handle_insufficient_storage(request: Request, exc: InsufficientStorageError):
    return JSONResponse(status_code=507, content={"detail": str(exc)})


@app.exception_handler(ValueError)
async def handle_value_error(request: Request, exc: ValueError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "node_id": NODE_ID}


@app.get("/status", response_model=StorageStatus)
def status() -> StorageStatus:
    cap = store.get_capacity_bytes()
    used = store.get_used_bytes()
    free = store.get_free_bytes()
    pct = round((used / cap * 100) if cap > 0 else 0.0, 2)
    return StorageStatus(
        node_id=store.node_id,
        machine_id=MACHINE_ID,
        disk_id=store.disk_id,
        address=STORAGE_ADDRESS,
        capacity_bytes=cap,
        used_bytes=used,
        free_bytes=free,
        usage_percent=pct,
        total_objects=len(store.list_objects()),
        last_scrub=store.last_scrub_report,
    )


@app.put("/objects/{object_id}", response_model=PutObjectResponse, status_code=201)
async def put_object(
    object_id: str,
    request: Request,
    expected_sha256: str | None = Query(None),
    write_id: str | None = Query(None),
    x_expected_sha256: str | None = Header(None),
    x_write_id: str | None = Header(None),
) -> PutObjectResponse:
    target_sha = expected_sha256 or x_expected_sha256
    target_write_id = write_id or x_write_id

    content_length_header = request.headers.get("content-length")
    content_length = int(content_length_header) if content_length_header and content_length_header.isdigit() else None

    meta = await store.put(
        object_id=object_id,
        stream=request.stream(),
        expected_sha256=target_sha,
        content_length=content_length,
    )

    controller_acked = False
    if target_write_id:
        controller_acked = await notify_controller_ack(
            write_id=target_write_id,
            sha256=meta.sha256,
            size_bytes=meta.size_bytes,
        )

    return PutObjectResponse(
        object_id=meta.object_id,
        node_id=store.node_id,
        disk_id=store.disk_id,
        sha256=meta.sha256,
        size_bytes=meta.size_bytes,
        controller_acked=controller_acked,
    )


@app.get("/objects/{object_id}")
async def get_object(object_id: str, verify: bool = Query(True)):
    meta, stream = await store.get_stream(object_id, verify=verify)
    return StreamingResponse(
        stream,
        media_type="application/octet-stream",
        headers={
            "ETag": f'"{meta.sha256}"',
            "X-Sha256": meta.sha256,
            "Content-Length": str(meta.size_bytes),
        },
    )


@app.head("/objects/{object_id}")
async def head_object(object_id: str):
    meta = store.get_metadata(object_id)
    return Response(
        status_code=200,
        headers={
            "ETag": f'"{meta.sha256}"',
            "X-Sha256": meta.sha256,
            "Content-Length": str(meta.size_bytes),
        },
    )


@app.get("/objects/{object_id}/meta", response_model=ObjectMetadata)
def get_object_meta(object_id: str) -> ObjectMetadata:
    return store.get_metadata(object_id)


@app.delete("/objects/{object_id}")
async def delete_object(object_id: str) -> dict[str, str | bool]:
    await store.delete(object_id)
    return {"object_id": object_id, "deleted": True}


@app.post("/objects/{object_id}/replicate", response_model=ReplicateResponse)
async def replicate_object(object_id: str, payload: ReplicateRequest) -> ReplicateResponse:
    if payload.target_address and payload.source_address:
        raise HTTPException(422, detail="Informe target_address OU source_address, não ambos")
    if not payload.target_address and not payload.source_address:
        raise HTTPException(422, detail="Necessário informar target_address ou source_address")

    if payload.target_address:
        meta = store.get_metadata(object_id)
        target_url = f"{payload.target_address.rstrip('/')}/objects/{object_id}"

        async def file_stream():
            _, chunks = await store.get_stream(object_id, verify=True)
            async for chunk in chunks:
                yield chunk

        async with httpx.AsyncClient(timeout=30) as client:
            headers = {"X-Expected-Sha256": meta.sha256}
            params = {}
            if payload.write_id:
                params["write_id"] = payload.write_id
            response = await client.put(target_url, content=file_stream(), headers=headers, params=params)
            if response.status_code not in (200, 201):
                raise HTTPException(
                    status_code=502,
                    detail=f"Falha na replicação remota: status={response.status_code} corpo={response.text}",
                )

        return ReplicateResponse(
            object_id=object_id,
            action="pushed",
            peer_address=payload.target_address,
            sha256=meta.sha256,
            size_bytes=meta.size_bytes,
            status="SUCCESS",
            controller_acked=bool(payload.write_id),
        )

    else:
        source_url = f"{payload.source_address.rstrip('/')}/objects/{object_id}"
        async with httpx.AsyncClient(timeout=30) as client:
            async with client.stream("GET", source_url) as resp:
                if resp.status_code == 404:
                    raise HTTPException(404, detail="Objeto não encontrado no nó de origem")
                if resp.status_code != 200:
                    raise HTTPException(502, detail=f"Erro no nó de origem: status={resp.status_code}")
                expected_sha = resp.headers.get("X-Sha256")
                meta = await store.put(object_id, resp.aiter_bytes(), expected_sha256=expected_sha)

        acked = False
        if payload.write_id:
            acked = await notify_controller_ack(payload.write_id, meta.sha256, meta.size_bytes)

        return ReplicateResponse(
            object_id=object_id,
            action="pulled",
            peer_address=payload.source_address,
            sha256=meta.sha256,
            size_bytes=meta.size_bytes,
            status="SUCCESS",
            controller_acked=acked,
        )


@app.post("/scrub", response_model=ScrubReport)
async def scrub_all() -> ScrubReport:
    return await store.scrub()


@app.post("/objects/{object_id}/scrub", response_model=ScrubReport)
async def scrub_single(object_id: str) -> ScrubReport:
    return await store.scrub(specific_object_id=object_id)


@app.get("/scrub/status", response_model=ScrubReport | dict)
def scrub_status():
    if store.last_scrub_report is None:
        return {
            "node_id": store.node_id,
            "disk_id": store.disk_id,
            "status": "NO_SCRUB_YET",
            "total_objects": len(store.list_objects()),
        }
    return store.last_scrub_report


@app.post("/objects/{object_id}/corrupt")
def inject_corruption(object_id: str) -> dict[str, str | bool]:
    """Endpoint de injeção de corrupção para testes de falha e demonstração."""
    store.corrupt_for_testing(object_id)
    return {"object_id": object_id, "corrupted": True}
