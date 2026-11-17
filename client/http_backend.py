"""Backend HTTP da CLI: transfere arquivos pelo gateway do controlador.

O computador do usuário não precisa alcançar os endereços privados dos
Storage Nodes. Operações de namespace usam metadados PostgreSQL; drain ainda indisponível.
"""
import hashlib
import os
import re
from urllib.parse import urlsplit
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
        self.direct_storage = os.getenv("DCOMP_DIRECT_STORAGE", "false").lower() in (
            "1", "true", "yes",
        )

    def _storage_address(self, address: str) -> str:
        """Somente nós do espaço de nomes interno Docker são elegíveis (anti-SSRF)."""
        cleaned = address.strip().rstrip("/")
        parsed = urlsplit(cleaned)
        if not (
            parsed.scheme == "http" and parsed.port == 9000
            and parsed.hostname is not None
            and re.fullmatch(r"storage-[1-9][0-9]*", parsed.hostname)
            and parsed.username is None and parsed.password is None
            and not parsed.path and not parsed.query and not parsed.fragment
        ):
            raise CLIError("SECURITY", "Endereço de Storage Node não autorizado")
        return cleaned

    def _request_storage(self, method: str, address: str, **kwargs) -> httpx.Response:
        try:
            with httpx.Client(
                timeout=httpx.Timeout(65.0, connect=5.0),
                transport=self.transport,
            ) as client:
                return client.request(method, address, **kwargs)
        except httpx.RequestError as exc:
            raise UnavailableError(f"Storage Node indisponível: {exc}") from exc

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
            created_at=payload.get("created_at"),
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
        if self.direct_storage:
            return self._put_direct(remote_path, contents, local_sha256)
        response = self._request(
            "PUT", "/files/content",
            params={"path": remote_path}, content=contents,
        )
        info = self._file_meta(response.json())
        if info.sha256 != local_sha256 or info.size != len(contents):
            raise CLIError("INTEGRITY", "Metadados retornados não conferem com o upload")
        return info

    def _put_direct(self, remote_path: str, data: bytes, sha256: str) -> FileMetadata:
        """Fluxo RF3: controlador só aloca e publica metadados.

        Todos os PUTs e verificações físicas são realizados pela CLI nos nós.
        """
        prepared = self._request(
            "POST", "/files/prepare",
            json={"path": remote_path, "sha256": sha256, "size_bytes": len(data)},
        ).json()
        write_id = prepared["write_id"]
        stored_at: list[str] = []
        try:
            for target in prepared["targets"]:
                address = self._storage_address(target["address"])
                url = f"{address}/objects/{write_id}"
                result = self._request_storage(
                    "PUT", url,
                    content=data,
                    headers={"X-Expected-Sha256": sha256, "X-Write-Id": write_id},
                )
                if result.status_code != 201:
                    raise CLIError("STORAGE", f"Falha de gravação no {target['node_id']} (HTTP {result.status_code})")
                stored_at.append(address)
                info = result.json()
                if (
                    info.get("node_id") != target["node_id"]
                    or info.get("sha256") != sha256
                    or info.get("size_bytes") != len(data)
                    or info.get("controller_acked") is not True
                ):
                    raise CLIError("INTEGRITY", f"ACK inválido no {target['node_id']}")

                check = self._request_storage("GET", f"{url}/verify")
                if check.status_code != 200:
                    raise CLIError("INTEGRITY", f"Verificação física falhou no {target['node_id']}")
                proof = check.json()
                if (
                    proof.get("match") is not True
                    or proof.get("expected_sha256") != sha256
                    or proof.get("size_bytes") != len(data)
                ):
                    raise CLIError("INTEGRITY", f"Hash físico inválido no {target['node_id']}")

            self._request("POST", f"/files/uploads/{write_id}/commit")
            return self.stat(remote_path)
        except Exception:
            # Nunca apagar objetos de uma versão possivelmente publicada.
            try:
                state = self._request("GET", f"/files/uploads/{write_id}").json()
            except CLIError:
                state = {"status": "UNKNOWN"}
            if state.get("status") == "PENDING":
                try:
                    self._request("POST", f"/files/uploads/{write_id}/abort")
                except CLIError:
                    pass
                for address in stored_at:
                    try:
                        self._request_storage("DELETE", f"{address}/objects/{write_id}")
                    except (CLIError, httpx.HTTPError):
                        pass
            raise

    def get(self, remote_path: str, local_path: Path) -> FileMetadata:
        info = self.stat(remote_path)
        if self.direct_storage:
            meta = self._request(
                "GET", "/files/metadata", params={"path": remote_path}
            ).json()
            object_id = meta.get("version_id")
            if not object_id:
                raise CLIError("PROTOCOL", "Controlador não informou version_id")
            payload = None
            for replica in meta.get("replicas", []):
                try:
                    base = self._storage_address(replica["address"])
                    response = self._request_storage("GET", f"{base}/objects/{object_id}")
                    if (
                        response.status_code == 200
                        and len(response.content) == info.size
                        and hashlib.sha256(response.content).hexdigest() == info.sha256
                        and response.headers.get("X-Sha256", info.sha256) == info.sha256
                    ):
                        payload = response.content
                        break
                except (CLIError, ValueError):
                    continue
            if payload is None:
                raise UnavailableError("Nenhuma réplica física íntegra disponível")
        else:
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
            files_by_state=payload.get("files_by_state", {}),
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
        self._request("POST", f"/nodes/{node_id}/drain")
