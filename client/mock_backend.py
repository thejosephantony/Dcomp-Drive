"""Backend simulado do controlador + storage nodes.

Existe para que a CLI possa ser construída e demonstrada (put/get/ls/...)
sem esperar o controlador real (FastAPI) nem os 5 storage nodes ficarem
prontos — é a tarefa combinada no plano para o dia 12/11: "esqueleto CLI,
testes e mocks". Quando o controlador real existir, este arquivo é
substituído por um HttpBackend que implementa o mesmo client.backend.Backend.

Simplificações assumidas aqui (documentar no relatório da Parte 1):
- replicação escolhe sempre os primeiros N nós "HEALTHY", sem política de
  domínio de falha nem balanceamento de capacidade;
- "drain" só marca o nó, não redistribui réplicas;
- "mv" de diretórios não é suportado.
"""

import hashlib
import json
import uuid
from pathlib import Path

from client.config import HASH_ALGORITHM, MOCK_NODE_COUNT, MOCK_STATE_DIR, REPLICATION_FACTOR
from client.errors import ConflictError, InsufficientCapacityError, NotFoundError, UnavailableError
from client.models import ClusterStatus, DirEntry, FileMetadata, FileState, NodeInfo, NodeState, ReplicaInfo

# Capacidade simulada por nó (10 GiB); só usada para compor o relatório de
# status, não há verificação real de espaço em disco no mock.
DEFAULT_CAPACITY_BYTES = 10 * 1024 * 1024 * 1024


def _normalize(path: str) -> str:
    """Remove barras duplicadas/finais e garante que o caminho comece com "/"."""
    parts = [p for p in path.split("/") if p]
    return "/" + "/".join(parts)


def _parent(path: str) -> str:
    if path == "/":
        return "/"
    idx = path.rfind("/")
    return path[:idx] or "/"


class MockBackend:
    def __init__(self) -> None:
        self._state_dir = MOCK_STATE_DIR
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / "metadata.json"
        self._objects_dir = self._state_dir / "objects"
        self._state = self._load()

    # -- persistência: cada invocação da CLI é um processo novo, então o
    # estado precisa sobreviver em disco entre um comando e o próximo --

    def _load(self) -> dict:
        if self._state_path.exists():
            return json.loads(self._state_path.read_text(encoding="utf-8"))
        nodes = {
            f"storage-{i + 1}": {
                "state": NodeState.HEALTHY.value,
                "capacity_bytes": DEFAULT_CAPACITY_BYTES,
                "used_bytes": 0,
            }
            for i in range(MOCK_NODE_COUNT)
        }
        return {"dirs": ["/"], "files": {}, "nodes": nodes}

    def _save(self) -> None:
        self._state_path.write_text(json.dumps(self._state, indent=2), encoding="utf-8")

    # -- diretórios --

    def mkdir(self, remote_path: str) -> None:
        path = _normalize(remote_path)
        if path in self._state["dirs"]:
            raise ConflictError(f"diretório já existe: {path}")
        parent = _parent(path)
        if parent not in self._state["dirs"]:
            raise NotFoundError(f"diretório pai não existe: {parent}")
        self._state["dirs"].append(path)
        self._save()

    def rmdir(self, remote_path: str) -> None:
        path = _normalize(remote_path)
        if path == "/":
            raise ConflictError("não é possível remover o diretório raiz")
        if path not in self._state["dirs"]:
            raise NotFoundError(f"diretório não existe: {path}")
        has_children = any(d.startswith(path + "/") for d in self._state["dirs"]) or any(
            f.startswith(path + "/") for f in self._state["files"]
        )
        if has_children:
            raise ConflictError(f"diretório não está vazio: {path}")
        self._state["dirs"].remove(path)
        self._save()

    def ls(self, remote_path: str) -> list[DirEntry]:
        path = _normalize(remote_path)
        if path not in self._state["dirs"]:
            raise NotFoundError(f"diretório não existe: {path}")
        entries: list[DirEntry] = []
        for d in self._state["dirs"]:
            if d != path and _parent(d) == path:
                entries.append(DirEntry(name=d.rsplit("/", 1)[-1], kind="dir"))
        for f, meta in self._state["files"].items():
            if _parent(f) == path:
                entries.append(
                    DirEntry(name=f.rsplit("/", 1)[-1], kind="file", size=meta["size"], state=meta["state"])
                )
        return sorted(entries, key=lambda e: (e.kind, e.name))

    # -- arquivos --

    def put(self, local_path: Path, remote_path: str) -> FileMetadata:
        if not local_path.is_file():
            raise NotFoundError(f"arquivo local não encontrado: {local_path}")
        path = _normalize(remote_path)
        parent = _parent(path)
        if parent not in self._state["dirs"]:
            raise NotFoundError(f"diretório de destino não existe: {parent}")

        content = local_path.read_bytes()
        digest = hashlib.new(HASH_ALGORITHM, content).hexdigest()

        # Seleção de destinos: só nós "HEALTHY", os primeiros N por ordem de
        # nome. Real: escolheria por domínio de falha e espaço disponível.
        healthy_nodes = sorted(
            node_id for node_id, info in self._state["nodes"].items() if info["state"] == NodeState.HEALTHY.value
        )
        if len(healthy_nodes) < REPLICATION_FACTOR:
            raise InsufficientCapacityError(
                f"apenas {len(healthy_nodes)} nó(s) saudável(is); são necessários {REPLICATION_FACTOR}"
            )
        targets = healthy_nodes[:REPLICATION_FACTOR]

        previous = self._state["files"].get(path)
        version = previous["version"] + 1 if previous else 1
        object_key = uuid.uuid4().hex

        objects = {}
        for node_id in targets:
            node_dir = self._objects_dir / node_id
            node_dir.mkdir(parents=True, exist_ok=True)
            (node_dir / object_key).write_bytes(content)
            objects[node_id] = object_key
            self._state["nodes"][node_id]["used_bytes"] += len(content)

        self._state["files"][path] = {
            "version": version,
            "size": len(content),
            "sha256": digest,
            "state": FileState.HEALTHY.value,
            "objects": objects,
            "replicas": [{"node_id": n, "state": NodeState.HEALTHY.value} for n in targets],
        }
        self._save()
        return self._to_metadata(path)

    def get(self, remote_path: str, local_path: Path) -> FileMetadata:
        path = _normalize(remote_path)
        entry = self._state["files"].get(path)
        if entry is None:
            raise NotFoundError(f"arquivo não encontrado: {path}")

        # Tenta cada réplica até achar uma íntegra e de um nó saudável —
        # mesma lógica do fluxo de leitura do plano (consultar -> buscar ->
        # validar, tentando outra réplica em caso de corrupção).
        for node_id, object_key in entry["objects"].items():
            if self._state["nodes"].get(node_id, {}).get("state") != NodeState.HEALTHY.value:
                continue
            object_path = self._objects_dir / node_id / object_key
            if not object_path.exists():
                continue
            content = object_path.read_bytes()
            digest = hashlib.new(HASH_ALGORITHM, content).hexdigest()
            if digest != entry["sha256"]:
                continue
            local_path.write_bytes(content)
            return self._to_metadata(path)

        raise UnavailableError(f"nenhuma réplica íntegra disponível para: {path}")

    def mv(self, src: str, dst: str) -> None:
        src_path = _normalize(src)
        dst_path = _normalize(dst)
        if src_path not in self._state["files"]:
            if src_path in self._state["dirs"]:
                raise ConflictError("mover diretórios ainda não é suportado pelo mock")
            raise NotFoundError(f"caminho não encontrado: {src_path}")
        if dst_path in self._state["files"] or dst_path in self._state["dirs"]:
            raise ConflictError(f"destino já existe: {dst_path}")
        parent = _parent(dst_path)
        if parent not in self._state["dirs"]:
            raise NotFoundError(f"diretório de destino não existe: {parent}")
        self._state["files"][dst_path] = self._state["files"].pop(src_path)
        self._save()

    def rm(self, remote_path: str) -> None:
        path = _normalize(remote_path)
        entry = self._state["files"].pop(path, None)
        if entry is None:
            raise NotFoundError(f"arquivo não encontrado: {path}")
        for node_id, object_key in entry["objects"].items():
            (self._objects_dir / node_id / object_key).unlink(missing_ok=True)
            self._state["nodes"][node_id]["used_bytes"] -= entry["size"]
        self._save()

    def stat(self, remote_path: str) -> FileMetadata:
        path = _normalize(remote_path)
        if path not in self._state["files"]:
            raise NotFoundError(f"arquivo não encontrado: {path}")
        return self._to_metadata(path)

    # -- cluster --

    def status(self) -> ClusterStatus:
        nodes = [
            NodeInfo(
                node_id=node_id,
                state=NodeState(info["state"]),
                capacity_bytes=info["capacity_bytes"],
                used_bytes=info["used_bytes"],
            )
            for node_id, info in sorted(self._state["nodes"].items())
        ]
        files_by_state: dict[str, int] = {}
        for entry in self._state["files"].values():
            files_by_state[entry["state"]] = files_by_state.get(entry["state"], 0) + 1
        return ClusterStatus(nodes=nodes, files_by_state=files_by_state)

    def drain(self, node_id: str) -> None:
        if node_id not in self._state["nodes"]:
            raise NotFoundError(f"nó não encontrado: {node_id}")
        self._state["nodes"][node_id]["state"] = NodeState.DRAINING.value
        self._save()

    def _to_metadata(self, path: str) -> FileMetadata:
        entry = self._state["files"][path]
        return FileMetadata(
            path=path,
            version=entry["version"],
            size=entry["size"],
            sha256=entry["sha256"],
            state=FileState(entry["state"]),
            replicas=[ReplicaInfo(node_id=r["node_id"], state=NodeState(r["state"])) for r in entry["replicas"]],
        )
