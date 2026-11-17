"""Teste ponta a ponta sem Docker: controlador + cinco Storage Nodes.

Requisitos: Python 3.12+, dependências em requirements.txt.
Executar na raiz do repositório: py scripts/smoke_gateway_local.py

Usa somente 127.0.0.1 e diretórios temporários. Não usa nem modifica o
banco de dados ou os volumes permanentes do projeto.
"""
import hashlib
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def port_free() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def ensure(predicate, message: str, deadline: float = 30.0) -> None:
    finish = time.monotonic() + deadline
    last_error = ""
    while time.monotonic() < finish:
        try:
            if predicate():
                return
        except Exception as exc:
            last_error = str(exc)
        time.sleep(0.25)
    raise RuntimeError(f"{message}. Último erro: {last_error}")


def run_cli(env: dict, *args: str) -> str:
    result = subprocess.run(
        [sys.executable, "-m", "client.cli", *args],
        env=env, cwd=ROOT, text=True, capture_output=True,
        timeout=75, encoding="utf-8", errors="replace",
    )
    output = (result.stdout + "\n" + result.stderr).strip()
    if result.returncode != 0:
        raise RuntimeError(f"CLI {' '.join(args)} falhou:\n{output}")
    return output


def main() -> int:
    from sqlalchemy import select

    with tempfile.TemporaryDirectory(prefix="dcomp-n3-smoke-") as tmpdir:
        root = Path(tmpdir)
        db_file = root / "test.sqlite"
        db_url = "sqlite+pysqlite:///" + db_file.as_posix()
        ctrl_port = port_free()
        node_ports = [port_free() for _ in range(5)]
        controller_url = f"http://127.0.0.1:{ctrl_port}"
        node_urls = [f"http://127.0.0.1:{p}" for p in node_ports]

        env = os.environ.copy()
        env.update({
            "DATABASE_URL": db_url,
            "NODE_TIMEOUT_SECONDS": "8",
            "MONITOR_INTERVAL_SECONDS": "1",
            "REPLICATION_FACTOR": "3",
            "REPAIR_INTERVAL_SECONDS": "1",
            "REPAIR_CLAIM_SECONDS": "180",
            "MAX_TRANSFER_BYTES": "1048576",
            "ALLOWED_STORAGE_ADDRESSES": ",".join(node_urls),
            "DCOMP_BACKEND": "http",
            "DCOMP_CONTROLLER_URL": controller_url,
            "PYTHONPATH": str(ROOT) + os.pathsep + env.get("PYTHONPATH", ""),
        })

        # Cria as tabelas de teste sem Alembic, somente para o smoke local.
        os.environ["DATABASE_URL"] = db_url
        from controller.database import engine, SessionLocal
        from controller.file_models import Directory, FileEntry
        from controller.models import Base
        Base.metadata.create_all(engine)
        with SessionLocal() as session:
            session.add(Directory(path="/"))
            session.commit()

        processes: dict[str, subprocess.Popen] = {}
        log_files = {}
        try:
            def spawn(name: str, app: str, port: int, config: dict) -> None:
                path = root / f"{name}.log"
                output = open(path, "w", encoding="utf-8")
                log_files[name] = output
                processes[name] = subprocess.Popen(
                    [sys.executable, "-m", "uvicorn", app,
                     "--host", "127.0.0.1", "--port", str(port), "--no-access-log"],
                    cwd=ROOT, env=config, stdout=output, stderr=subprocess.STDOUT,
                )

            spawn("controller", "controller.main:app", ctrl_port, env)
            with httpx.Client(timeout=2.0) as client:
                ensure(
                    lambda: client.get(f"{controller_url}/health").status_code == 200,
                    "Controlador não ficou saudável",
                )
            print("OK  Controlador iniciado com SQLite temporário.")

            for i, (port, url) in enumerate(zip(node_ports, node_urls), 1):
                node = f"storage-{i}"
                config = env.copy()
                config.update({
                    "NODE_ID": node,
                    "MACHINE_ID": node,
                    "STORAGE_ADDRESS": url,
                    "CONTROLLER_URL": controller_url,
                    "DATA_DIR": str(root / node),
                    "STORAGE_CAPACITY_BYTES": str(10 * 1024 * 1024),
                    "HEARTBEAT_INTERVAL_SECONDS": "1",
                })
                spawn(node, "storage.main:app", port, config)

            with httpx.Client(timeout=4.0) as client:
                def ready() -> bool:
                    data = client.get(f"{controller_url}/status").json()
                    return data.get("counts", {}).get("HEALTHY") == 5
                ensure(ready, "Os cinco nós não se registraram")
                print("OK  Cinco Storage Nodes HEALTHY.")

                original = root / "original.bin"
                data = (b"DComp Drive N=3 com dados reais\x00" * 2000)
                original.write_bytes(data)
                result = run_cli(env, "put", str(original), "/smoke.bin")
                print("OK  CLI put: " + result)

                meta = client.get(
                    f"{controller_url}/files/metadata",
                    params={"path": "/smoke.bin"},
                ).json()
                if (
                    meta["status"] != "HEALTHY"
                    or len(meta["replicas"]) != 3
                    or len({item["machine_id"] for item in meta["replicas"]}) != 3
                ):
                    raise RuntimeError(f"Metadados de N=3 inválidos: {meta}")

                with SessionLocal() as session:
                    entry = session.scalar(select(FileEntry).where(FileEntry.path == "/smoke.bin"))
                    object_id = entry.current_version_id

                for replica in meta["replicas"]:
                    file_path = root / replica["node_id"] / "objects" / object_id
                    if not file_path.exists() or file_path.read_bytes() != data:
                        raise RuntimeError(f"Réplica física inválida: {file_path}")
                print("OK  Três réplicas físicas íntegrias em diretórios diferentes.")

                recovered = root / "recovered.bin"
                run_cli(env, "get", "/smoke.bin", str(recovered))
                if recovered.read_bytes() != data:
                    raise RuntimeError("CLI get divergiu dos bytes originais")
                if hashlib.sha256(recovered.read_bytes()).digest() != hashlib.sha256(data).digest():
                    raise RuntimeError("SHA-256 divergiu após download")
                print("OK  Download pela CLI preservou os bytes e SHA-256.")

                failed_node = meta["replicas"][0]["node_id"]
                processes[failed_node].terminate()
                processes[failed_node].wait(timeout=10)
                ensure(
                    lambda: any(
                        node["node_id"] == failed_node and node["state"] == "DEAD"
                        for node in client.get(f"{controller_url}/status").json()["nodes"]
                    ),
                    f"{failed_node} não foi marcado DEAD",
                    deadline=15,
                )
                print(f"OK  {failed_node} marcado DEAD após queda do processo.")

                recovered_after_failure = root / "recovered-after-failure.bin"
                run_cli(env, "get", "/smoke.bin", str(recovered_after_failure))
                if recovered_after_failure.read_bytes() != data:
                    raise RuntimeError("Download após falha divergiu")
                print("OK  Download via outras réplicas após queda de um nó.")

                def healed() -> bool:
                    info = client.get(
                        f"{controller_url}/files/metadata",
                        params={"path": "/smoke.bin"},
                    ).json()
                    return (
                        info["status"] == "HEALTHY"
                        and len(info["replicas"]) == 3
                        and failed_node not in {r["node_id"] for r in info["replicas"]}
                    )

                ensure(healed, "O cluster não reconstruiu N=3 automaticamente", deadline=30)
                healed_meta = client.get(
                    f"{controller_url}/files/metadata",
                    params={"path": "/smoke.bin"},
                ).json()
                for replica in healed_meta["replicas"]:
                    file_path = root / replica["node_id"] / "objects" / object_id
                    if not file_path.exists() or file_path.read_bytes() != data:
                        raise RuntimeError(f"Réplica reconstruída inválida: {file_path}")
                print("OK  Terceira réplica reconstruída automaticamente em outro nó.")
                print("OK  Três cópias físicas íntegrias após a falha.")
                print("RESULTADO: fluxo N=3 e re-replicação automática aprovados, sem Docker.")
                print("NOTA: os nós ainda são processos na mesma máquina; a persistência PostgreSQL não foi testada.")
                return 0
        except Exception as exc:
            print(f"FALHA: {exc}", file=sys.stderr)
            for name, file_handle in log_files.items():
                file_handle.flush()
                try:
                    lines = (root / f"{name}.log").read_text(encoding="utf-8").splitlines()
                    print(f"\n[{name}] últimas linhas:", file=sys.stderr)
                    print("\n".join(lines[-12:]), file=sys.stderr)
                except OSError:
                    pass
            return 1
        finally:
            for process in processes.values():
                if process.poll() is None:
                    process.terminate()
            for process in processes.values():
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            for file_handle in log_files.values():
                file_handle.close()
            # Libera as conexões SQLite antes do TemporaryDirectory limpar
            # os arquivos no Windows.
            engine.dispose()


if __name__ == "__main__":
    sys.exit(main())
