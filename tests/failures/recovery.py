"""Cenário F06/F09: queda de um nó com réplica, re-replicação automática e retorno.

Roda contra o cluster real do docker compose (5 storages). Para cada execução:

1. grava um arquivo aleatório e descobre quais nós guardam as N réplicas;
2. para um desses nós (docker compose stop);
3. acompanha GET /status e GET /files/metadata e mede o tempo até:
   - o nó ficar DEAD,
   - o arquivo sair de HEALTHY (UNDER_REPLICATED / RECOVERING),
   - o arquivo voltar a HEALTHY com N réplicas, nenhuma no nó parado;
4. baixa o arquivo e compara o SHA-256 com o original;
5. religa o nó, espera ele voltar a HEALTHY e confere que o arquivo continua
   HEALTHY/N e íntegro depois da reconciliação (RF6);
6. remove o arquivo de teste e grava os tempos (JSON + CSV acumulado).

Sai com código 1 se N réplicas não forem restabelecidas a tempo ou se algum
download não bater com o SHA-256 original.

Uso (na raiz do repositório, com o cluster no ar):

    python tests/failures/recovery.py
    python tests/failures/recovery.py --runs 3 --size 4194304
    python tests/failures/recovery.py --out docs/evidencias/parte1

Upload/download usam o gateway do controlador (DCOMP_DIRECT_STORAGE=false),
porque a máquina que roda o teste não alcança os endereços internos dos nós.
"""

import argparse
import csv
import hashlib
import json
import os
import sys
import tempfile
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from client.errors import CLIError  # noqa: E402
from client.http_backend import HttpBackend  # noqa: E402
from node_failure import (  # noqa: E402
    REPO_ROOT, RESULTS_DIR, ScenarioError, compose, compose_setting, log, node_state, now_iso, wait_for_state,
)

RECOVERED = "RECOVERED"


@dataclass
class RecoveryResult:
    started_at: str
    file_path: str
    size_bytes: int
    sha256: str
    replication_factor: int
    replicas_before: list[str]
    victim: str
    replicas_after: list[str] = field(default_factory=list)
    dead_after_s: float | None = None
    degraded_after_s: float | None = None
    states_seen: list[str] = field(default_factory=list)
    recovered_after_s: float | None = None
    sha256_after_recovery_ok: bool = False
    node_back_after_s: float | None = None
    healthy_after_return: bool = False
    sha256_after_return_ok: bool = False
    passed: bool = False
    error: str | None = None


def recovery_label(meta: dict, victim: str, replicas: int) -> str:
    """RECOVERED quando o arquivo voltou a N réplicas saudáveis sem o nó parado;
    senão, o estado informado pelo controlador."""
    nodes = {r["node_id"] for r in meta["replicas"]}
    if meta["status"] == "HEALTHY" and len(nodes) >= replicas and victim not in nodes:
        return RECOVERED
    return meta["status"]


def file_digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def downloaded_ok(backend: HttpBackend, remote: str, expected: str, workdir: Path) -> bool:
    out = workdir / f"download-{time.monotonic_ns()}.bin"
    backend.get(remote, out)
    ok = file_digest(out) == expected
    out.unlink(missing_ok=True)
    return ok


def run_once(
    client: httpx.Client, backend: HttpBackend, size: int, node_timeout_s: float,
    recovery_deadline_s: float, restart_deadline_s: float, workdir: Path,
) -> RecoveryResult:
    remote = f"/recovery-{datetime.now().strftime('%Y%m%d-%H%M%S-%f')}.bin"
    local = workdir / "original.bin"
    local.write_bytes(os.urandom(size))
    digest = file_digest(local)

    backend.put(local, remote)
    meta = client.get("/files/metadata", params={"path": remote}).raise_for_status().json()
    replicas = meta["replication_factor"]
    holders = sorted(r["node_id"] for r in meta["replicas"])
    result = RecoveryResult(
        started_at=now_iso(), file_path=remote, size_bytes=size, sha256=digest,
        replication_factor=replicas, replicas_before=holders, victim=holders[0],
    )
    victim = result.victim
    log(f"{remote} gravado ({size} B) em {holders}; derrubando {victim}")

    stopped = False
    try:
        if meta["status"] != "HEALTHY" or len(holders) != replicas:
            raise ScenarioError(f"arquivo não ficou HEALTHY/{replicas} após o put: {meta['status']} {holders}")

        compose("stop", victim)
        stopped = True

        def read_label() -> str:
            if result.dead_after_s is None and node_state(client, victim) == "DEAD":
                result.dead_after_s = round(time.monotonic() - t0, 3)
                log(f"{victim} -> DEAD após {result.dead_after_s:.2f}s")
            current = client.get("/files/metadata", params={"path": remote}).raise_for_status().json()
            label = recovery_label(current, victim, replicas)
            if label == RECOVERED:
                result.replicas_after = sorted(r["node_id"] for r in current["replicas"])
            return label

        def track(label: str | None, elapsed: float) -> None:
            if label and label != RECOVERED and label not in result.states_seen:
                result.states_seen.append(label)
                if label != "HEALTHY" and result.degraded_after_s is None:
                    result.degraded_after_s = round(elapsed, 3)
                log(f"arquivo -> {label} após {elapsed:.2f}s")

        t0 = time.monotonic()
        _, elapsed = wait_for_state(
            read_label, {RECOVERED}, deadline_s=node_timeout_s + recovery_deadline_s, interval_s=0.5, on_state=track,
        )
        result.recovered_after_s = round(elapsed, 3)
        log(f"N={replicas} restabelecido após {elapsed:.2f}s em {result.replicas_after}")

        result.sha256_after_recovery_ok = downloaded_ok(backend, remote, digest, workdir)
        if not result.sha256_after_recovery_ok:
            raise ScenarioError("SHA-256 do download após a recuperação difere do original")

        log(f"religando {victim}")
        compose("start", victim)
        stopped = False
        _, elapsed = wait_for_state(lambda: node_state(client, victim), {"HEALTHY"}, deadline_s=restart_deadline_s)
        result.node_back_after_s = round(elapsed, 3)
        log(f"{victim} -> HEALTHY após {elapsed:.2f}s do start")

        # Dá tempo para a reconciliação do inventário do nó que voltou.
        time.sleep(node_timeout_s)
        after = client.get("/files/metadata", params={"path": remote}).raise_for_status().json()
        result.healthy_after_return = after["status"] == "HEALTHY" and len(after["replicas"]) >= replicas
        result.sha256_after_return_ok = downloaded_ok(backend, remote, digest, workdir)
        if not (result.healthy_after_return and result.sha256_after_return_ok):
            raise ScenarioError(
                f"após o retorno do nó: estado={after['status']} réplicas={len(after['replicas'])} "
                f"sha_ok={result.sha256_after_return_ok}"
            )
        result.passed = True
    except (ScenarioError, CLIError, httpx.HTTPError) as exc:
        result.error = str(getattr(exc, "message", exc))
        log(f"FALHOU: {result.error}")
    finally:
        if stopped:
            compose("start", victim)
        try:
            backend.rm(remote)
        except CLIError as exc:
            log(f"aviso: não removeu {remote}: {exc.message}")
    return result


def save(results: list[RecoveryResult], out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"recovery-{stamp}.json"
    json_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    csv_path = out_dir / "recovery.csv"
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(results[0])))
        if is_new:
            writer.writeheader()
        for r in results:
            row = asdict(r)
            for key in ("replicas_before", "replicas_after", "states_seen"):
                row[key] = " ".join(row[key])
            writer.writerow(row)
    return json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--runs", type=int, default=1, help="repetições (Parte 2 pede 3 a 5)")
    parser.add_argument("--size", type=int, default=1024 * 1024, help="tamanho do arquivo de teste em bytes")
    parser.add_argument("--controller", default=os.environ.get("DCOMP_CONTROLLER_URL", "http://127.0.0.1:8000"))
    parser.add_argument("--node-timeout", type=float, default=float(compose_setting("NODE_TIMEOUT_SECONDS", "6")))
    parser.add_argument("--recovery-deadline", type=float, default=60.0,
                        help="segundos além do timeout do nó para restabelecer N réplicas")
    parser.add_argument("--restart-deadline", type=float, default=30.0, help="limite para o nó voltar a HEALTHY")
    parser.add_argument("--out", type=Path, default=RESULTS_DIR, help="pasta dos resultados")
    args = parser.parse_args(argv)

    # Gateway do controlador: o teste roda fora da rede interna do Docker.
    os.environ["DCOMP_DIRECT_STORAGE"] = "false"
    backend = HttpBackend(args.controller)
    results: list[RecoveryResult] = []
    with httpx.Client(base_url=args.controller, timeout=10) as client, tempfile.TemporaryDirectory() as tmp:
        unhealthy = [n["node_id"] for n in client.get("/status").raise_for_status().json()["nodes"]
                     if n["state"] != "HEALTHY"]
        if unhealthy:
            log(f"todos os nós precisam estar HEALTHY antes do teste; fora do ar: {unhealthy}")
            return 1
        for i in range(args.runs):
            log(f"execução {i + 1}/{args.runs}")
            results.append(run_once(client, backend, args.size, args.node_timeout,
                                    args.recovery_deadline, args.restart_deadline, Path(tmp)))

    out_dir = args.out if args.out.is_absolute() else REPO_ROOT / args.out
    path = save(results, out_dir)
    passed = sum(r.passed for r in results)
    log(f"{passed}/{len(results)} execuções passaram; resultados em {path.relative_to(REPO_ROOT)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
