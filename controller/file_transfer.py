"""Gateway HTTP para transferência de arquivos (primeiro fluxo N=3).

O cliente fala apenas com o controlador. Os endereços internos dos Storage
Nodes não precisam ser acessíveis pelo computador que executa a CLI.

Limites desta fase: payloads de até MAX_TRANSFER_BYTES (32 MiB por padrão),
sem autenticação de usuário e sem recomposição automática de réplicas.
"""
import hashlib
import logging
import os
from urllib.parse import urlsplit

import httpx
from controller.telemetry import record_transfer
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from sqlalchemy import select
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.file_api import abort, commit, metadata, prepare
from controller.file_models import FileEntry, FileVersion
from controller.file_schemas import FileMetadataResponse, PrepareRequest, validate_remote_file_path

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["transfer"])


def _limit() -> int:
    limit = int(os.getenv("MAX_TRANSFER_BYTES", str(32 * 1024 * 1024)))
    if limit < 1:
        raise HTTPException(500, "MAX_TRANSFER_BYTES deve ser positivo")
    return limit


def _path(path: str) -> str:
    try:
        return validate_remote_file_path(path)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


def _storage_url(address: str) -> str:
    """Autoriza exclusivamente endereços internos declarados pelo operador.

    Evita que registros arbitrários de nós virem requisições HTTP do
    controlador contra serviços internos (SSRF).
    """
    allowed = {
        item.strip().rstrip("/")
        for item in os.getenv(
            "ALLOWED_STORAGE_ADDRESSES",
            ",".join(f"http://storage-{i}:9000" for i in range(1, 6)),
        ).split(",")
        if item.strip()
    }
    cleaned = address.strip().rstrip("/")
    try:
        parsed = urlsplit(cleaned)
        valid = (
            parsed.scheme in ("http", "https")
            and parsed.hostname is not None
            and parsed.port is not None
            and parsed.username is None
            and parsed.password is None
            and not parsed.path
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        valid = False
    if not valid or cleaned not in allowed:
        raise HTTPException(502, "Endereço de Storage Node não autorizado")
    return cleaned


def _timeout() -> httpx.Timeout:
    return httpx.Timeout(45.0, connect=5.0)


@router.put("/content", response_model=FileMetadataResponse, status_code=201)
async def upload_content(
    request: Request,
    path: str = Query(...),
    session: Session = Depends(get_session),
) -> FileMetadataResponse:
    """Reserva N réplicas, grava bytes, verifica cada nó e publica a versão."""
    path = _path(path)
    maximum = _limit()
    buffer = bytearray()
    async for chunk in request.stream():
        if len(buffer) + len(chunk) > maximum:
            raise HTTPException(413, f"Arquivo ultrapassa o limite de {maximum} bytes")
        buffer.extend(chunk)

    data = bytes(buffer)
    sha256 = hashlib.sha256(data).hexdigest()
    operation = prepare(
        PrepareRequest(path=path, size_bytes=len(data), sha256=sha256),
        session,
    )
    stored_at: list[str] = []
    try:
        async with httpx.AsyncClient(timeout=_timeout()) as client:
            for target in operation.targets:
                base = _storage_url(target.address)
                response = await client.put(
                    f"{base}/objects/{operation.write_id}",
                    content=data,
                    headers={"X-Expected-Sha256": sha256, "X-Write-Id": operation.write_id},
                )
                if response.status_code != 201:
                    raise HTTPException(
                        502, f"{target.node_id}: gravação recusada (HTTP {response.status_code})"
                    )
                report = response.json()
                if (
                    report.get("node_id") != target.node_id
                    or report.get("disk_id") != target.disk_id
                    or report.get("sha256") != sha256
                    or report.get("size_bytes") != len(data)
                ):
                    raise HTTPException(502, f"{target.node_id}: resposta de gravação inconsistente")
                stored_at.append(base)
                if report.get("controller_acked") is not True:
                    raise HTTPException(502, f"{target.node_id}: ACK não recebido pelo controlador")

                # Não confiar só em ACKs: ler os bytes físicos antes do commit.
                check = await client.get(f"{base}/objects/{operation.write_id}")
                if (
                    check.status_code != 200
                    or len(check.content) != len(data)
                    or hashlib.sha256(check.content).hexdigest() != sha256
                ):
                    raise HTTPException(502, f"{target.node_id}: validação física da réplica falhou")

        commit(operation.write_id, session)
        record_transfer(upload_bytes=len(data))
        return metadata(path, session)

    except Exception as exc:
        session.rollback()
        # Uma gravação já publicada nunca pode ser apagada pelo cleanup.
        current = session.get(FileVersion, operation.write_id)
        still_pending = current is not None and current.status == "PENDING"
        if still_pending:
            try:
                abort(operation.write_id, session)
            except Exception:
                logger.exception("failed_to_abort_upload write_id=%s", operation.write_id)
            # Remover apenas a versão nova, por seu ID imutável.
            async with httpx.AsyncClient(timeout=_timeout()) as client:
                for base in stored_at:
                    try:
                        await client.delete(f"{base}/objects/{operation.write_id}")
                    except httpx.RequestError:
                        logger.warning("cleanup_failed storage=%s write_id=%s", base, operation.write_id)
        if isinstance(exc, HTTPException):
            raise
        if isinstance(exc, (httpx.RequestError, ValueError)):
            raise HTTPException(502, f"Falha na transferência: {exc}") from exc
        logger.exception("upload_failed path=%s write_id=%s", path, operation.write_id)
        raise HTTPException(500, "Erro inesperado na transferência") from exc


@router.get("/content")
async def download_content(
    path: str = Query(...),
    session: Session = Depends(get_session),
) -> Response:
    """Busca uma réplica física íntegra; tenta a seguinte se houver falha."""
    path = _path(path)
    info = metadata(path, session)
    if info.size_bytes > _limit():
        raise HTTPException(413, "Arquivo excede o limite deste gateway")
    entry = session.scalar(select(FileEntry).where(FileEntry.path == path))
    if entry is None or not entry.current_version_id:
        raise HTTPException(404, "Arquivo não encontrado")
    if not info.replicas:
        raise HTTPException(503, "Nenhuma réplica saudável disponível")

    async with httpx.AsyncClient(timeout=_timeout()) as client:
        for replica in info.replicas:
            try:
                base = _storage_url(replica.address)
                response = await client.get(f"{base}/objects/{entry.current_version_id}")
                if response.status_code != 200:
                    continue
                data = response.content
                if (
                    len(data) != info.size_bytes
                    or hashlib.sha256(data).hexdigest() != info.sha256
                ):
                    logger.error("replica_digest_mismatch node=%s path=%s", replica.node_id, path)
                    continue
                record_transfer(download_bytes=len(data))
                return Response(
                    content=data,
                    media_type="application/octet-stream",
                    headers={
                        "X-Sha256": info.sha256,
                        "X-Version": str(info.version),
                        "X-Replication-Status": info.status,
                    },
                )
            except httpx.RequestError:
                logger.warning("replica_unreachable node=%s path=%s", replica.node_id, path)

    raise HTTPException(503, "Nenhuma réplica física íntegra disponível")
