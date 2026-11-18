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
import hmac
import os
import re
import time
from pathlib import Path

from fastapi import FastAPI, Depends, Header, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
import httpx

from storage.schemas import (
    ObjectMetadata,
    ObjectVerifyResponse,
    PutObjectResponse,
    ReplicateRequest,
    ReplicateResponse,
    ScrubReport,
    StorageStatus,
    validate_sha256_digest,
)
from storage.crypto import EncryptionUnavailableError
from storage.store import (
    CorruptedObjectError,
    HashMismatchError,
    InsufficientStorageError,
    ObjectNotFoundError,
    ObjectStore,
    StorageError,
)

from controller.logging_config import configure_logging

configure_logging()
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
    reconciled = False
    async with httpx.AsyncClient(timeout=3) as client:
        while True:
            if DEMO_PAUSED and DEMO_OFFLINE_AT is not None:
                maximum = float(os.getenv("DEMO_AUTO_RESTORE_SECONDS", "60"))
                if maximum > 0 and time.monotonic() - DEMO_OFFLINE_AT >= maximum:
                    restore_demo_node_automatically()
            if DEMO_PAUSED:
                registered = False
                reconciled = False
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                continue
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
                    reconciled = False
                    logger.info("registered node=%s controller=%s", NODE_ID, CONTROLLER_URL)
                else:
                    response = await client.post(
                        f"{CONTROLLER_URL}/nodes/{NODE_ID}/heartbeat",
                        json={"disks": [_disk_report()]},
                    )
                    if response.status_code == 404:
                        registered = False
                        reconciled = False
                    else:
                        response.raise_for_status()
                if registered and not reconciled:
                    inventory = {
                        "disk_id": DISK_ID,
                        "objects": [
                            {
                                "object_id": meta.object_id,
                                "sha256": meta.sha256,
                                "size_bytes": meta.size_bytes,
                                "status": meta.status,
                            }
                            for meta in store.list_objects()
                        ],
                    }
                    # Após um restart, reconciliar as cópias que sobraram
                    # sem exigir edição manual do controlador.
                    reconciled_response = await client.post(
                        f"{CONTROLLER_URL}/nodes/{NODE_ID}/reconcile",
                        json=inventory, timeout=30,
                    )
                    reconciled_response.raise_for_status()
                    reconciled = True
                    logger.info(
                        "node_reconciled node=%s result=%s",
                        NODE_ID, reconciled_response.json(),
                    )
            except (httpx.HTTPError, OSError):
                registered = False
                logger.exception("heartbeat_or_registration_failed node=%s", NODE_ID)
            await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)


async def notify_corrupted_replicas(report: ScrubReport) -> None:
    """Notifica o controlador sem bloquear o scrubbing em caso de falha de rede.

    A verificação física segue válida mesmo quando o controlador está fora;
    ocorrências persistentes são reenviadas no próximo ciclo periódico.
    """
    if not report.corrupted_ids:
        return
    async with httpx.AsyncClient(timeout=5) as client:
        for object_id in set(report.corrupted_ids):
            try:
                response = await client.post(
                    f"{CONTROLLER_URL}/nodes/{NODE_ID}/objects/{object_id}/corruption"
                )
                if response.status_code == 200:
                    logger.warning(
                        "scrub_corruption_reported node=%s object_id=%s",
                        NODE_ID, object_id,
                    )
                else:
                    logger.warning(
                        "scrub_corruption_report_rejected node=%s object_id=%s code=%s",
                        NODE_ID, object_id, response.status_code,
                    )
            except (httpx.HTTPError, OSError) as exc:
                logger.warning(
                    "scrub_corruption_report_failed node=%s object_id=%s error=%s",
                    NODE_ID, object_id, exc,
                )


async def scrub_forever() -> None:
    """Execução periódica em segundo plano de scrubbing, quando configurada."""
    if SCRUB_INTERVAL_SECONDS <= 0:
        logger.info("scrub_background_disabled node=%s (SCRUB_INTERVAL_SECONDS=0)", NODE_ID)
        return
    logger.info("scrub_background_started node=%s interval=%.1fs", NODE_ID, SCRUB_INTERVAL_SECONDS)
    while True:
        try:
            await asyncio.sleep(SCRUB_INTERVAL_SECONDS)
            if DEMO_PAUSED:
                continue
            logger.info("scrub_periodic_trigger node=%s", NODE_ID)
            report = await store.scrub()
            await notify_corrupted_replicas(report)
        except asyncio.CancelledError:
            logger.info("scrub_background_cancelled node=%s", NODE_ID)
            break
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

# Falha lógica: bloqueia tráfego do nó e suspende heartbeats. Nunca para Docker.
# Somente em ambiente de laboratório isolado, desativado por padrão.
DEMO_PAUSED = False
DEMO_OFFLINE_AT: float | None = None


def restore_demo_node_automatically() -> None:
    global DEMO_PAUSED, DEMO_OFFLINE_AT
    if not DEMO_PAUSED:
        return
    DEMO_PAUSED = False
    DEMO_OFFLINE_AT = None
    logger.warning("demo_node_automatically_restored node=%s", NODE_ID)


def _require_demo_admin(x_demo_token: str | None = Header(default=None)) -> None:
    if os.getenv("DEMO_MODE", "false").lower() != "true":
        raise HTTPException(404, "Modo de demonstração desativado")
    secret = os.getenv("DEMO_ADMIN_TOKEN", "")
    if len(secret) < 32:
        raise HTTPException(503, "Token de demonstração não configurado")
    if x_demo_token is None or not hmac.compare_digest(x_demo_token, secret):
        raise HTTPException(403, "Token administrativo inválido")


@app.middleware("http")
async def demo_node_outage(request: Request, call_next):
    if DEMO_PAUSED and not request.url.path.startswith("/_demo/"):
        return JSONResponse(status_code=503, content={"detail": "Storage temporariamente indisponível (demonstração)"})
    return await call_next(request)


@app.post("/_demo/offline")
def demo_offline(_: None = Depends(_require_demo_admin)) -> dict:
    global DEMO_PAUSED, DEMO_OFFLINE_AT
    DEMO_PAUSED = True
    DEMO_OFFLINE_AT = time.monotonic()
    logger.warning("demo_node_failure_injected node=%s", NODE_ID)
    return {"node_id": NODE_ID, "paused": True}


@app.post("/_demo/restore")
def demo_restore(_: None = Depends(_require_demo_admin)) -> dict:
    global DEMO_PAUSED, DEMO_OFFLINE_AT
    DEMO_PAUSED = False
    DEMO_OFFLINE_AT = None
    logger.info("demo_node_restored node=%s", NODE_ID)
    return {"node_id": NODE_ID, "paused": False}


@app.post("/_demo/encryption/{object_id}")
def demo_encryption_evidence(
    object_id: str,
    _: None = Depends(_require_demo_admin),
) -> dict:
    """RF9: comprova formato cifrado no disco sem expor bytes ou chaves."""
    import hashlib
    from storage.crypto import MAGIC

    meta = store.get_metadata(object_id)
    path = store.objects_dir / object_id
    if not path.is_file():
        raise HTTPException(404, "Objeto físico não localizado")
    with path.open("rb") as file:
        prefix = file.read(len(MAGIC))
    # Hash do arquivo armazenado difere do hash do plaintext quando há cifra.
    physical_digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "object_id": object_id,
        "node_id": store.node_id,
        "encryption": meta.encryption,
        "encrypted_on_disk": prefix == MAGIC and meta.encryption == "AES-256-GCM",
        "disk_sha256": physical_digest,
        "plaintext_sha256": meta.sha256,
        "physical_size_bytes": path.stat().st_size,
        "plaintext_size_bytes": meta.size_bytes,
    }


@app.post("/_demo/corrupt/{object_id}")
async def demo_corrupt(object_id: str, _: None = Depends(_require_demo_admin)) -> dict:
    if DEMO_PAUSED:
        raise HTTPException(409, "Restaure o nó antes de corromper objeto")
    store.corrupt_for_testing(object_id)
    report = await store.scrub(specific_object_id=object_id)
    await notify_corrupted_replicas(report)
    return {"object_id": object_id, "corrupted_ids": report.corrupted_ids}



@app.exception_handler(ObjectNotFoundError)
async def handle_not_found(request: Request, exc: ObjectNotFoundError):
    return JSONResponse(status_code=404, content={"detail": str(exc)})


@app.exception_handler(HashMismatchError)
async def handle_hash_mismatch(request: Request, exc: HashMismatchError):
    return JSONResponse(status_code=422, content={"detail": str(exc)})


@app.exception_handler(EncryptionUnavailableError)
async def handle_missing_key(request: Request, exc: EncryptionUnavailableError):
    return JSONResponse(status_code=503, content={"detail": str(exc)})


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
    if target_sha is not None:
        try:
            target_sha = validate_sha256_digest(target_sha)
        except ValueError as exc:
            raise HTTPException(422, detail=f"Hash SHA-256 esperado inválido: {exc}")

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
    if meta.status == "CORRUPTED":
        raise HTTPException(
            status_code=500,
            detail=f"Objeto {object_id} corrompido: hash não confere",
            headers={"X-Object-Status": "CORRUPTED", "X-Sha256": meta.sha256},
        )
    return Response(
        status_code=200,
        headers={
            "ETag": f'"{meta.sha256}"',
            "X-Sha256": meta.sha256,
            "Content-Length": str(meta.size_bytes),
            "X-Object-Status": meta.status,
        },
    )


@app.get("/objects/{object_id}/meta", response_model=ObjectMetadata)
def get_object_meta(object_id: str) -> ObjectMetadata:
    return store.get_metadata(object_id)


@app.get("/objects/{object_id}/verify", response_model=ObjectVerifyResponse)
async def verify_object(object_id: str) -> ObjectVerifyResponse:
    """Verifica explicitamente a integridade por SHA-256 de um objeto no disco."""
    is_valid, expected, computed, meta = await store.verify_object_integrity(object_id)
    if not is_valid:
        raise HTTPException(
            status_code=500,
            detail=f"Integridade violada para {object_id}: esperado {expected}, calculado {computed}",
        )
    return ObjectVerifyResponse(
        object_id=object_id,
        status=meta.status,
        expected_sha256=expected,
        computed_sha256=computed,
        size_bytes=meta.size_bytes,
        match=is_valid,
    )


@app.delete("/objects/{object_id}")
async def delete_object(object_id: str) -> dict[str, str | bool]:
    await store.delete(object_id)
    return {"object_id": object_id, "deleted": True}


def _is_self_address(target_addr: str) -> bool:
    """Verifica se o endereço alvo aponta para este próprio nó de armazenamento."""
    cleaned = target_addr.strip().rstrip("/")
    self_addr = STORAGE_ADDRESS.strip().rstrip("/")
    if cleaned == self_addr:
        return True
    try:
        from urllib.parse import urlparse
        target_p = urlparse(cleaned)
        self_p = urlparse(self_addr)
        if target_p.hostname in (store.node_id, NODE_ID) and (target_p.port or 9000) == (self_p.port or 9000):
            return True
    except Exception:
        pass
    return False


def _validate_peer_url(address: str) -> str:
    """Permite replicação apenas entre destinos explicitamente conhecidos.

    Impede que o endpoint de replicação vire um proxy HTTP para serviços
    arbitrários (SSRF). Ex.: ALLOWED_STORAGE_PEERS=http://storage-1:9000,...
    """
    from urllib.parse import urlsplit

    cleaned = address.strip().rstrip("/")
    try:
        parsed = urlsplit(cleaned)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.path
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("URL de nó inválida")
    except ValueError as exc:
        raise HTTPException(
            422, detail="Endereço inválido: use uma URL HTTP(S) de nó com porta explícita"
        ) from exc

    configured = os.getenv("ALLOWED_STORAGE_PEERS", "").strip()
    if configured:
        allowed = {item.strip().rstrip("/") for item in configured.split(",") if item.strip()}
        authorized = cleaned in allowed
    else:
        # RF6: aceita novos storages numerados na rede interna sem atualizar
        # a allowlist central; rejeita serviços externos e outras portas.
        authorized = (
            parsed.scheme == "http"
            and parsed.port == 9000
            and re.fullmatch(r"storage-[1-9][0-9]*", parsed.hostname or "")
        )
    if not authorized:
        raise HTTPException(403, detail="Destino de replicação não autorizado")

    if _is_self_address(cleaned):
        raise HTTPException(400, detail="Não é permitido replicar para o próprio nó")
    return cleaned

@app.post("/objects/{object_id}/replicate", response_model=ReplicateResponse)
async def replicate_object(object_id: str, payload: ReplicateRequest) -> ReplicateResponse:
    if payload.target_address and payload.source_address:
        raise HTTPException(422, detail="Informe target_address OU source_address, não ambos")
    if not payload.target_address and not payload.source_address:
        raise HTTPException(422, detail="Necessário informar target_address ou source_address")

    if payload.target_address:
        # PUSH: Nó de origem envia objeto local para nó de destino
        target_addr = _validate_peer_url(payload.target_address)
        meta = store.get_metadata(object_id)
        if meta.status == "CORRUPTED":
            raise HTTPException(409, detail=f"Não é possível replicar objeto corrompido: {object_id}")

        # Validação física de integridade local antes de iniciar a transferência
        is_valid, expected, computed, meta = await store.verify_object_integrity(object_id)
        if not is_valid:
            raise HTTPException(409, detail=f"Arquivo local está corrompido (esperado {expected}, obtido {computed}), replicação abortada")

        target_url = f"{target_addr}/objects/{object_id}"
        logger.info("replicate_push_start node=%s target=%s object_id=%s size=%d", store.node_id, target_addr, object_id, meta.size_bytes)

        async def file_stream():
            _, chunks = await store.get_stream(object_id, verify=False)
            async for chunk in chunks:
                yield chunk

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                headers = {
                    "X-Expected-Sha256": meta.sha256,
                    "Content-Length": str(meta.size_bytes),
                }
                params = {}
                if payload.write_id:
                    params["write_id"] = payload.write_id
                response = await client.put(target_url, content=file_stream(), headers=headers, params=params)
                if response.status_code not in (200, 201):
                    logger.error("replicate_push_peer_error target=%s status=%d body=%s", target_addr, response.status_code, response.text)
                    raise HTTPException(
                        status_code=502,
                        detail=f"Falha na replicação remota ({response.status_code}): {response.text}",
                    )

                peer_acked = False
                try:
                    resp_data = response.json()
                    peer_acked = bool(resp_data.get("controller_acked", False))
                except Exception:
                    pass

        except httpx.TimeoutException as exc:
            logger.error("replicate_push_timeout target=%s error=%s", target_addr, exc)
            raise HTTPException(504, detail=f"Timeout na comunicação com o nó remoto: {target_addr}") from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            logger.error("replicate_push_connect_error target=%s error=%s", target_addr, exc)
            raise HTTPException(502, detail=f"Falha de conexão com o nó remoto: {target_addr}") from exc

        logger.info("replicate_push_success node=%s target=%s object_id=%s peer_acked=%s", store.node_id, target_addr, object_id, peer_acked)
        return ReplicateResponse(
            object_id=object_id,
            action="pushed",
            peer_address=target_addr,
            sha256=meta.sha256,
            size_bytes=meta.size_bytes,
            status="SUCCESS",
            controller_acked=peer_acked,
        )

    else:
        # PULL: Nó de destino solicita objeto do nó de origem e grava localmente
        source_addr = _validate_peer_url(payload.source_address)
        source_url = f"{source_addr}/objects/{object_id}"
        logger.info("replicate_pull_start node=%s source=%s object_id=%s", store.node_id, source_addr, object_id)

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                async with client.stream("GET", source_url) as resp:
                    if resp.status_code == 404:
                        logger.error("replicate_pull_not_found source=%s object_id=%s", source_addr, object_id)
                        raise HTTPException(404, detail=f"Objeto não encontrado no nó de origem: {object_id}")
                    if resp.status_code != 200:
                        logger.error("replicate_pull_peer_error source=%s status=%d", source_addr, resp.status_code)
                        raise HTTPException(502, detail=f"Erro no nó de origem ({resp.status_code})")

                    expected_sha = resp.headers.get("X-Sha256") or payload.expected_sha256
                    content_len_hdr = resp.headers.get("Content-Length")
                    expected_size = int(content_len_hdr) if content_len_hdr and content_len_hdr.isdigit() else payload.expected_size_bytes

                    # Idempotência: se este nó já tem o objeto idêntico e saudável, evita re-download
                    if object_id in store._cache:
                        existing = store._cache[object_id]
                        if existing.status == "HEALTHY" and expected_sha and existing.sha256 == expected_sha.lower():
                            if expected_size is None or existing.size_bytes == expected_size:
                                logger.info("replicate_pull_already_exists node=%s object_id=%s", store.node_id, object_id)
                                acked = False
                                if payload.write_id:
                                    acked = await notify_controller_ack(payload.write_id, existing.sha256, existing.size_bytes)
                                return ReplicateResponse(
                                    object_id=object_id,
                                    action="pulled",
                                    peer_address=source_addr,
                                    sha256=existing.sha256,
                                    size_bytes=existing.size_bytes,
                                    status="ALREADY_EXISTS",
                                    controller_acked=acked,
                                )

                    meta = await store.put(
                        object_id=object_id,
                        stream=resp.aiter_bytes(),
                        expected_sha256=expected_sha,
                        content_length=expected_size,
                    )

        except httpx.TimeoutException as exc:
            logger.error("replicate_pull_timeout source=%s error=%s", source_addr, exc)
            raise HTTPException(504, detail=f"Timeout na comunicação com o nó remoto: {source_addr}") from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            logger.error("replicate_pull_connect_error source=%s error=%s", source_addr, exc)
            raise HTTPException(502, detail=f"Falha de conexão com o nó remoto: {source_addr}") from exc

        # Confirmação efetiva do ACK junto ao controlador após persistência física
        acked = False
        if payload.write_id:
            acked = await notify_controller_ack(payload.write_id, meta.sha256, meta.size_bytes)

        logger.info("replicate_pull_success node=%s source=%s object_id=%s controller_acked=%s", store.node_id, source_addr, object_id, acked)
        return ReplicateResponse(
            object_id=object_id,
            action="pulled",
            peer_address=source_addr,
            sha256=meta.sha256,
            size_bytes=meta.size_bytes,
            status="SUCCESS",
            controller_acked=acked,
        )


@app.post("/scrub", response_model=ScrubReport)
async def scrub_all() -> ScrubReport:
    report = await store.scrub()
    await notify_corrupted_replicas(report)
    return report


@app.post("/objects/{object_id}/scrub", response_model=ScrubReport)
async def scrub_single(object_id: str) -> ScrubReport:
    report = await store.scrub(specific_object_id=object_id)
    await notify_corrupted_replicas(report)
    return report


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
    """Injeção de corrupção habilitada somente em ambientes de teste."""
    if os.getenv("ENABLE_TEST_ENDPOINTS", "").lower() not in ("1", "true", "yes"):
        raise HTTPException(404, detail="Endpoint de teste desabilitado")
    store.corrupt_for_testing(object_id)
    return {"object_id": object_id, "corrupted": True}
