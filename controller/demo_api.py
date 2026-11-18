"""Controles autenticados para demonstrações em laboratório isolado.

Não expor em uma API pública sem autenticação de usuários. A simulação de falha
bloqueia operações no Storage Node e suspende heartbeats, sem usar Docker socket.
"""
import hmac
import os
import re

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from controller.database import get_session
from controller.file_api import metadata
from controller.file_transfer import _storage_url
from controller.models import Node

router = APIRouter(prefix="/demo", tags=["Demonstração de laboratório"])


def _enabled() -> bool:
    return os.getenv("DEMO_MODE", "false").lower() == "true"


def _secret() -> str:
    return os.getenv("DEMO_ADMIN_TOKEN", "")


def require_demo_admin(x_demo_token: str | None = Header(default=None)) -> None:
    if not _enabled():
        raise HTTPException(404, "Modo de demonstração desativado")
    secret = _secret()
    if len(secret) < 32:
        raise HTTPException(503, "DEMO_ADMIN_TOKEN não configurado com segurança")
    if x_demo_token is None or not hmac.compare_digest(x_demo_token, secret):
        raise HTTPException(403, "Token administrativo inválido")


@router.get("/config")
def demo_config() -> dict[str, bool]:
    """Não expõe segredos; público só em stack explicitamente isolada."""
    public = _public_enabled()
    return {"enabled": public or (_enabled() and len(_secret()) >= 32), "public": public}


def _public_enabled() -> bool:
    return (
        _enabled()
        and os.getenv("PUBLIC_DEMO_MODE", "false").lower() == "true"
        and os.getenv("PUBLIC_DEMO_ISOLATED", "false").lower() == "true"
        and len(_secret()) >= 32
    )


def require_public_demo() -> None:
    if not _public_enabled():
        raise HTTPException(404, "Demonstração pública não disponível")


def _require_node(session: Session, node_id: str) -> Node:
    if not re.fullmatch(r"storage-[1-5]", node_id):
        raise HTTPException(422, "Selecione um storage do laboratório")
    node = session.get(Node, node_id)
    if node is None:
        raise HTTPException(404, "Storage não registrado")
    return node


async def _send(node: Node, suffix: str, secret: str) -> dict:
    try:
        base = _storage_url(node.address)
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                base + suffix,
                headers={"X-Demo-Token": secret},
            )
        response.raise_for_status()
        return response.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise HTTPException(502, f"Falha ao acionar storage: {type(exc).__name__}") from exc


@router.post("/nodes/{node_id}/offline")
async def simulate_node_failure(
    node_id: str,
    _: None = Depends(require_demo_admin),
    session: Session = Depends(get_session),
) -> dict:
    node = _require_node(session, node_id)
    healthy = session.query(Node).filter(Node.state == "HEALTHY").count()
    if node.state != "HEALTHY" or healthy < 5:
        raise HTTPException(409, "O laboratório deve ter cinco nós saudáveis antes da falha")
    result = await _send(node, "/_demo/offline", _secret())
    return {"action": "offline", "kind": "falha lógica controlada", **result}


@router.post("/nodes/{node_id}/restore")
async def restore_node(
    node_id: str,
    _: None = Depends(require_demo_admin),
    session: Session = Depends(get_session),
) -> dict:
    node = _require_node(session, node_id)
    result = await _send(node, "/_demo/restore", _secret())
    return {"action": "restore", **result}


class CorruptDemoRequest(BaseModel):
    path: str


@router.post("/files/corrupt")
async def corrupt_demo_replica(
    request: CorruptDemoRequest,
    _: None = Depends(require_demo_admin),
    session: Session = Depends(get_session),
) -> dict:
    # Impede alterar objetos reais: exclusivamente nomes novos de demonstração.
    if not re.fullmatch(r"/demo-[A-Za-z0-9_.-]{1,100}", request.path):
        raise HTTPException(422, "Só é permitido corromper /demo-* descartável")
    healthy_nodes = session.query(Node).filter(Node.state == "HEALTHY").count()
    if healthy_nodes < 5:
        raise HTTPException(409, "Restabeleça os cinco storages antes do teste de corrupção")
    info = metadata(path=request.path, session=session)
    if info.status != "HEALTHY" or len(info.replicas) < 3 or not info.version_id:
        raise HTTPException(409, "O arquivo deve possuir ao menos três réplicas saudáveis")
    node = _require_node(session, info.replicas[0].node_id)
    result = await _send(node, "/_demo/corrupt/" + info.version_id, _secret())
    return {"action": "corruption", "path": request.path, "node_id": node.node_id, **result}


@router.post("/nodes/{node_id}/drain")
def drain_demo_node(
    node_id: str,
    _: None = Depends(require_demo_admin),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    """RF7: drenagem somente via controles administrativos do laboratório."""
    node = _require_node(session, node_id)
    if node.state != "HEALTHY":
        raise HTTPException(409, "O nó deve estar saudável antes da drenagem")
    if session.query(Node).filter(Node.state == "HEALTHY").count() < 5:
        raise HTTPException(409, "Requer cinco nós saudáveis antes da drenagem")
    node.state = "DRAINING"
    session.commit()
    return {"node_id": node_id, "state": node.state}


@router.post("/nodes/{node_id}/undrain")
def undrain_demo_node(
    node_id: str,
    _: None = Depends(require_demo_admin),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    node = _require_node(session, node_id)
    if node.state != "DRAINING":
        raise HTTPException(409, "Nó não está sendo drenado")
    node.state = "HEALTHY"
    session.commit()
    return {"node_id": node_id, "state": node.state}


# A interface pública não possui token. A autorização para acionar nós
# ocorre exclusivamente do controlador para o storage, na rede Docker da
# stack descartável. Não publicar essas rotas na stack principal.
@router.post("/public/nodes/{node_id}/offline")
async def public_offline(
    node_id: str,
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict:
    return await simulate_node_failure(node_id, None, session)


@router.post("/public/nodes/{node_id}/restore")
async def public_restore(
    node_id: str,
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict:
    return await restore_node(node_id, None, session)


@router.post("/public/nodes/{node_id}/drain")
def public_drain(
    node_id: str,
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    return drain_demo_node(node_id, None, session)


@router.post("/public/nodes/{node_id}/undrain")
def public_undrain(
    node_id: str,
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict[str, str]:
    return undrain_demo_node(node_id, None, session)


@router.post("/public/files/corrupt")
async def public_corrupt(
    request: CorruptDemoRequest,
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict:
    return await corrupt_demo_replica(request, None, session)


@router.post("/public/reset")
async def public_reset(
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict:
    """Reabilita os cinco nós do laboratório (nunca apaga arquivos)."""
    results: list[str] = []
    for node_id in ("storage-1", "storage-2", "storage-3", "storage-4", "storage-5"):
        node = session.get(Node, node_id)
        if node is None:
            continue
        if node.state == "DRAINING":
            node.state = "HEALTHY"
        try:
            await _send(node, "/_demo/restore", _secret())
            results.append(node_id)
        except HTTPException:
            pass
    session.commit()
    return {"restored": results, "total": len(results)}



@router.post("/files/encryption")
async def private_encryption_evidence(
    request: CorruptDemoRequest,
    _: None = Depends(require_demo_admin),
    session: Session = Depends(get_session),
) -> dict:
    if not re.fullmatch(r"/demo-[A-Za-z0-9_.-]{1,100}", request.path):
        raise HTTPException(422, "Use um arquivo descartável /demo-*")
    info = metadata(path=request.path, session=session)
    if not info.version_id or not info.replicas:
        raise HTTPException(409, "Arquivo não possui réplica acessível")
    node = _require_node(session, info.replicas[0].node_id)
    return await _send(node, "/_demo/encryption/" + info.version_id, _secret())


@router.post("/public/files/encryption")
async def public_encryption_evidence(
    request: CorruptDemoRequest,
    _: None = Depends(require_public_demo),
    session: Session = Depends(get_session),
) -> dict:
    return await private_encryption_evidence(request, None, session)
