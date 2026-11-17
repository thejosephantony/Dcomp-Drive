"""Backend HTTP da CLI: transfere arquivos pelo gateway do controlador.

O computador do usuário não precisa alcançar os endereços privados dos
Storage Nodes. Operações de namespace usam metadados PostgreSQL; drain ainda indisponível.
"""
import hashlib
import os
from pathlib import Path
import tempfile

import httpx

from client.errors import (
    CLIError, ConflictError, InsufficientCapacityError, NotFoundError,
    UnavailableError,
)
from client.models import (
    ClusterStatus, DirEntry, FileMetadata, FileState, NodeInfo, NodeState, ReplicaInfo,
)


class HttpBackend:
    def __init__(
        self,
        controller_url: str | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.controller_url = (
            controller_url or os.getenv("DCOMP_CONTROLLER_URL", "http://127.0.0.1:8000")
        ).rstrip("/")
        self.transport = transport

    def _request(self, method: str, endpoint: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(
                base_url=self.controller_url,
                timeout=httpx.Timeout(65.0, connect=5.0),
                transport=self.transport,
            ) as client:
                response = client.request(method, endpoint, **kwargs)
        except httpx.RequestError as exc:
            raise UnavailableError(f"Controlador indisponível: {exc}") from exc
        if response.status_code >= 400:
            try:
                detail = response.json().get("detail", response.text)
            except ValueError:
                detail = response.text
            detail = str(detail)
            if response.status_code == 404:
                raise NotFoundError(detail)
            if response.status_code == 409:
                raise ConflictError(detail)
            if response.status_code == 507:
                raise InsufficientCapacityError(detail)
            if response.status_code == 503:
                raise UnavailableError(detail)
            raise CLIError(str(response.status_code), detail)
        return response

    def _file_meta(self, payload: dict) -> FileMetadata:
        return FileMetadata(
            path=payload["path"],
            version=payload["version"],
            size=payload["size_bytes"],
            sha256=payload["sha256"],
            state=FileState(payload["status"]),
            replicas=[
                ReplicaInfo(node_id=item["node_id"], state=NodeState.HEALTHY)
                for item in payload.get("replicas", [])
            ],
        )

    def put(self, local_path: Path, remote_path: str) -> FileMetadata:
        if not local_path.is_file():
            raise NotFoundError(f"Arquivo local inexistente: {local_path}")
        try:
            contents = local_path.read_bytes()
        except OSError as exc:
            raise CLIError("LOCAL", f"Falha ao ler {local_path}: {exc}") from exc
        local_sha256 = hashlib.sha256(contents).hexdigest()
        response = self._request(
            "PUT", "/files/content",
            params={"path": remote_path}, content=contents,
        )
        info = self._file_meta(response.json())
        if info.sha256 != local_sha256 or info.size != len(contents):
            raise CLIError("INTEGRITY", "Metadados retornados não conferem com o upload")
        return info

    def get(self, remote_path: str, local_path: Path) -> FileMetadata:
        info = self.stat(remote_path)
        response = self._request("GET", "/files/content", params={"path": remote_path})
        payload = response.content
        if (
            hashlib.sha256(payload).hexdigest() != info.sha256
            or len(payload) != info.size
            or response.headers.get("X-Sha256", info.sha256) != info.sha256
        ):
            raise CLIError("INTEGRITY", "Arquivo baixado não passou pela verificação SHA-256")

        # Só substitui o destino depois da verificação integral.
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb", prefix=".dcomp-", suffix=".tmp",
                dir=local_path.parent, delete=False,
            ) as destination:
                temporary = Path(destination.name)
                destination.write(payload)
                destination.flush()
                os.fsync(destination.fileno())
            temporary.replace(local_path)
        except OSError as exc:
            raise CLIError("LOCAL", f"Falha ao gravar {local_path}: {exc}") from exc
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return info

    def stat(self, remote_path: str) -> FileMetadata:
        response = self._request("GET", "/files/metadata", params={"path": remote_path})
        return self._file_meta(response.json())

    def status(self) -> ClusterStatus:
        response = self._request("GET", "/status")
        payload = response.json()
        return ClusterStatus(
            nodes=[
                NodeInfo(
                    node_id=n["node_id"],
                    state=NodeState(n["state"]),
                    capacity_bytes=n["capacity_bytes"],
                    used_bytes=n["used_bytes"],
                )
                for n in payload["nodes"]
            ],
            files_by_state={},  # O controlador ainda não oferece contagem global.
        )

    @staticmethod
    def _unsupported(operation: str):
        raise CLIError("501", f"{operation} ainda não implementado no backend HTTP")

    def ls(self, remote_path: str) -> list[DirEntry]:
        response = self._request("GET", "/files/entries", params={"path": remote_path})
        return [DirEntry(**item) for item in response.json()]

    def mkdir(self, remote_path: str) -> None:
        self._request("POST", "/files/directories", json={"path": remote_path})

    def rmdir(self, remote_path: str) -> None:
        self._request("DELETE", "/files/directories", params={"path": remote_path})

    def mv(self, src: str, dst: str) -> None:
        self._request("POST", "/files/move", json={"src": src, "dst": dst})

    def rm(self, remote_path: str) -> None:
        response = self._request("DELETE", "/files/entry", params={"path": remote_path})
        pending = response.json().get("pending_cleanup", 0)
        if pending:
            raise CLIError(
                "CLEANUP",
                f"Arquivo removido do catálogo, mas {pending} réplica(s) ainda aguardam limpeza física",
            )

    def drain(self, node_id: str) -> None:
        self._unsupported("drain")
