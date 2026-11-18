"""Operações de namespace do RF1, persistidas em PostgreSQL.

O rename é exclusivamente de metadados. Exclusões removem primeiro a entrada
lógica; a limpeza das cópias físicas é best effort, com falhas registradas.
"""
import logging
import os
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.file_api import aware, metadata
from controller.file_models import Directory, FileEntry, FileVersion, Replica
from controller.file_schemas import validate_remote_file_path
from controller.file_transfer import _storage_url
from controller.models import Node

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/files", tags=["namespace"])


class NamespacePath(BaseModel):
    path: str


class MoveRequest(BaseModel):
    src: str
    dst: str


class NamespaceEntry(BaseModel):
    name: str
    kind: str
    size: int = 0
    state: str = ""


def _valid(path: str, *, allow_root: bool = False) -> str:
    if allow_root and path == "/":
        return path
    try:
        return validate_remote_file_path(path)
    except (ValueError, AttributeError) as exc:
        raise HTTPException(422, detail=str(exc)) from exc


def _parent(path: str) -> str:
    return path.rsplit("/", 1)[0] or "/"


def _has_pending_write(session: Session, entry: FileEntry) -> bool:
    versions = session.scalars(
        select(FileVersion)
        .where(FileVersion.file_id == entry.file_id, FileVersion.status == "PENDING")
    ).all()
    now = datetime.now(timezone.utc)
    return any(aware(version.expires_at) > now for version in versions)


@router.get("/entries", response_model=list[NamespaceEntry])
def list_entries(
    path: str = Query("/"),
    session: Session = Depends(get_session),
) -> list[NamespaceEntry]:
    path = _valid(path, allow_root=True)
    if session.get(Directory, path) is None:
        raise HTTPException(404, detail="diretório não encontrado")

    children: list[NamespaceEntry] = []
    # Só mostra entradas imediatas, mantendo a mesma regra do backend mock.
    for dirname in session.scalars(select(Directory.path)).all():
        if dirname != path and _parent(dirname) == path:
            children.append(NamespaceEntry(
                name=dirname.rsplit("/", 1)[-1], kind="dir"
            ))

    for file in session.scalars(
        select(FileEntry).where(FileEntry.current_version_id.is_not(None))
    ).all():
        if _parent(file.path) != path:
            continue
        info = metadata(path=file.path, session=session)
        children.append(NamespaceEntry(
            name=file.path.rsplit("/", 1)[-1], kind="file",
            size=info.size_bytes, state=info.status,
        ))
    return sorted(children, key=lambda item: (item.kind, item.name))


@router.post("/directories", status_code=201)
def make_directory(
    payload: NamespacePath,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    path = _valid(payload.path)
    if session.get(Directory, path) is not None:
        raise HTTPException(409, detail="diretório já existe")
    if session.scalar(select(FileEntry).where(FileEntry.path == path)) is not None:
        raise HTTPException(409, detail="um arquivo já ocupa o caminho")
    if session.get(Directory, _parent(path)) is None:
        raise HTTPException(404, detail="diretório pai não existe")
    if os.getenv("PUBLIC_DEMO_MODE", "false").lower() == "true":
        if os.getenv("PUBLIC_DEMO_ISOLATED", "false").lower() != "true":
            raise HTTPException(503, "Namespace público exige stack isolada")
        limit = int(os.getenv("PUBLIC_DEMO_MAX_DIRECTORIES", "100"))
        count = session.scalar(select(func.count(Directory.path))) or 0
        if count >= limit:
            raise HTTPException(507, "Laboratório atingiu limite de diretórios")
    session.add(Directory(path=path))
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, detail="diretório criado por outra operação") from exc
    return {"path": path, "status": "CREATED"}


@router.delete("/directories")
def remove_directory(
    path: str = Query(...),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    path = _valid(path, allow_root=True)
    if path == "/":
        raise HTTPException(409, detail="não é possível remover o diretório raiz")
    directory = session.get(Directory, path)
    if directory is None:
        raise HTTPException(404, detail="diretório não existe")
    prefix = path + "/"
    # Inclui arquivos ainda pendentes para não invalidar escritas em curso.
    if (
        any(p.startswith(prefix) for p in session.scalars(select(Directory.path)).all())
        or any(p.startswith(prefix) for p in session.scalars(select(FileEntry.path)).all())
    ):
        raise HTTPException(409, detail="diretório não está vazio")
    session.delete(directory)
    session.commit()
    return {"path": path, "status": "DELETED"}


@router.post("/move")
def move_file(
    payload: MoveRequest,
    session: Session = Depends(get_session),
) -> dict[str, str]:
    src = _valid(payload.src)
    dst = _valid(payload.dst)
    entry = session.scalar(
        select(FileEntry).where(FileEntry.path == src).with_for_update()
    )
    if entry is None or entry.current_version_id is None:
        if session.get(Directory, src) is not None:
            raise HTTPException(409, detail="mv de diretórios não é suportado")
        raise HTTPException(404, detail="arquivo não encontrado")
    if src == dst:
        raise HTTPException(409, detail="origem e destino são iguais")
    if _has_pending_write(session, entry):
        raise HTTPException(409, detail="arquivo possui escrita pendente")
    if (
        session.get(Directory, dst) is not None
        or session.scalar(select(FileEntry).where(FileEntry.path == dst)) is not None
    ):
        raise HTTPException(409, detail="destino já existe")
    if session.get(Directory, _parent(dst)) is None:
        raise HTTPException(404, detail="diretório de destino não existe")
    entry.path = dst
    try:
        session.commit()
    except IntegrityError as exc:
        session.rollback()
        raise HTTPException(409, detail="destino ocupado por operação concorrente") from exc
    logger.info("file_moved from=%s to=%s", src, dst)
    return {"src": src, "dst": dst, "status": "MOVED"}


@router.delete("/entry")
async def remove_file(
    path: str = Query(...),
    session: Session = Depends(get_session),
) -> dict[str, str | int]:
    path = _valid(path)
    entry = session.scalar(
        select(FileEntry).where(FileEntry.path == path).with_for_update()
    )
    if entry is None or entry.current_version_id is None:
        if session.get(Directory, path) is not None:
            raise HTTPException(409, detail="o caminho é um diretório")
        raise HTTPException(404, detail="arquivo não encontrado")
    if _has_pending_write(session, entry):
        raise HTTPException(409, detail="arquivo possui escrita pendente")

    versions = session.scalars(
        select(FileVersion).where(FileVersion.file_id == entry.file_id)
    ).all()
    version_ids = [version.version_id for version in versions]
    copies = session.scalars(
        select(Replica).where(Replica.version_id.in_(version_ids))
    ).all() if version_ids else []
    node_ids = {copy.node_id for copy in copies}
    nodes = {
        node.node_id: node
        for node in session.scalars(select(Node).where(Node.node_id.in_(node_ids))).all()
    } if node_ids else {}
    targets: set[tuple[str, str]] = set()
    for copy in copies:
        node = nodes.get(copy.node_id)
        if node is None:
            continue
        try:
            targets.add((_storage_url(node.address), copy.version_id))
        except (ValueError, HTTPException):
            logger.warning(
                "remove_cleanup_untrusted_storage node=%s path=%s", copy.node_id, path
            )

    # A entrada fica invisível imediatamente; limpeza remota ocorre depois.
    for copy in copies:
        session.delete(copy)
    for version in versions:
        session.delete(version)
    session.delete(entry)
    session.commit()

    failed = 0
    async with httpx.AsyncClient(timeout=5.0) as client:
        for address, version_id in sorted(targets):
            try:
                response = await client.delete(f"{address}/objects/{version_id}")
                if response.status_code not in (200, 404):
                    failed += 1
                    logger.warning(
                        "remove_cleanup_http_error path=%s node=%s code=%d",
                        path, address, response.status_code,
                    )
            except (httpx.RequestError, OSError):
                failed += 1
                logger.exception(
                    "remove_cleanup_unavailable path=%s storage=%s",
                    path, address,
                )
    return {
        "path": path,
        "status": "DELETED",
        "pending_cleanup": failed,
    }
