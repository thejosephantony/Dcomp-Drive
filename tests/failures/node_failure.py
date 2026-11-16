"""Cenário de falha de nó: queda, detecção (SUSPECT/DEAD) e retorno (HEALTHY).

Roda contra o cluster real do docker compose. Para cada execução:

1. confere que o nó alvo está HEALTHY;
2. para o contêiner (docker compose stop <nó>);
3. consulta GET /status até o nó aparecer como SUSPECT e depois DEAD;
4. religa o contêiner e consulta até o nó voltar a HEALTHY;
5. grava os tempos medidos em tests/results/ (JSON por execução + CSV acumulado).

O script falha (código de saída 1) se o nó não chegar a DEAD dentro de
NODE_TIMEOUT_SECONDS + tolerância, ou se não voltar a HEALTHY a tempo.

Uso (na raiz do repositório, com o cluster no ar):

    python tests/failures/node_failure.py --node storage-3
    python tests/failures/node_failure.py --node storage-3 --runs 5

Limitação: a re-replicação ainda não existe no controlador, então o script
mede só a detecção da falha e o retorno do nó, não a recuperação dos arquivos.
"""

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_DIR = REPO_ROOT / "tests" / "results"


class ScenarioError(Exception):
    """O cluster não se comportou como esperado durante o cenário."""


@dataclass
class RunResult:
    node: str
    started_at: str
    node_timeout_s: float
    suspect_after_s: float | None
    dead_after_s: float | None
    healthy_after_restart_s: float | None
    passed: bool
    error: str | None = None


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def log(message: str) -> None:
    print(f"[{now_iso()}] {message}", flush=True)


def node_state(client: httpx.Client, node_id: str) -> str | None:
    """Estado do nó segundo GET /status, ou None se o nó não estiver listado."""
    response = client.get("/status")
    response.raise_for_status()
    for node in response.json()["nodes"]:
        if node["node_id"] == node_id:
            return node["state"]
    return None


def wait_for_state(
    read_state: Callable[[], str | None],
    targets: set[str],
    deadline_s: float,
    interval_s: float = 0.2,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    on_state: Callable[[str | None, float], None] | None = None,
) -> tuple[str, float]:
    """Consulta o estado até ele estar em `targets`.

    Retorna (estado alcançado, segundos decorridos). Chama `on_state` a cada
    estado observado (útil para registrar SUSPECT no caminho até DEAD).
    Erros de rede contam como "sem resposta" e a consulta continua.
    """
    start = clock()
    while True:
        elapsed = clock() - start
        try:
            state = read_state()
        except httpx.HTTPError:
            state = None
        if on_state is not None:
            on_state(state, elapsed)
        if state in targets:
            return state, elapsed
        if elapsed >= deadline_s:
            raise ScenarioError(
                f"estado {sorted(targets)} não alcançado em {deadline_s:.1f}s (último: {state})"
            )
        sleep(interval_s)


def compose(action: str, node: str) -> None:
    subprocess.run(["docker", "compose", action, node], cwd=REPO_ROOT, check=True, capture_output=True)


def run_once(client: httpx.Client, node: str, node_timeout_s: float, grace_s: float, restart_deadline_s: float) -> RunResult:
    result = RunResult(
        node=node,
        started_at=now_iso(),
        node_timeout_s=node_timeout_s,
        suspect_after_s=None,
        dead_after_s=None,
        healthy_after_restart_s=None,
        passed=False,
    )
    stopped = False
    try:
        state = node_state(client, node)
        if state != "HEALTHY":
            raise ScenarioError(f"{node} precisa estar HEALTHY antes do teste (atual: {state})")

        log(f"parando {node}")
        compose("stop", node)
        stopped = True

        def track_suspect(state: str | None, elapsed: float) -> None:
            if state == "SUSPECT" and result.suspect_after_s is None:
                result.suspect_after_s = round(elapsed, 3)
                log(f"{node} -> SUSPECT após {elapsed:.2f}s")

        # Mede a partir do fim do "stop": a partir daí o nó com certeza não
        # envia mais heartbeat.
        _, elapsed = wait_for_state(
            lambda: node_state(client, node),
            {"DEAD"},
            deadline_s=node_timeout_s + grace_s,
            on_state=track_suspect,
        )
        result.dead_after_s = round(elapsed, 3)
        log(f"{node} -> DEAD após {elapsed:.2f}s")

        log(f"religando {node}")
        compose("start", node)
        stopped = False
        _, elapsed = wait_for_state(lambda: node_state(client, node), {"HEALTHY"}, deadline_s=restart_deadline_s)
        result.healthy_after_restart_s = round(elapsed, 3)
        log(f"{node} -> HEALTHY após {elapsed:.2f}s do start")

        result.passed = True
    except (ScenarioError, httpx.HTTPError, subprocess.CalledProcessError) as exc:
        result.error = str(exc)
        log(f"FALHOU: {exc}")
    finally:
        # Nunca deixar o cluster com um nó parado se o cenário abortar no meio.
        if stopped:
            compose("start", node)
    return result


def save(results: list[RunResult]) -> Path:
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = RESULTS_DIR / f"node_failure-{stamp}.json"
    json_path.write_text(json.dumps([asdict(r) for r in results], indent=2), encoding="utf-8")

    csv_path = RESULTS_DIR / "node_failure.csv"
    is_new = not csv_path.exists()
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(asdict(results[0])))
        if is_new:
            writer.writeheader()
        writer.writerows(asdict(r) for r in results)
    return json_path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--node", default="storage-3", help="serviço do compose a derrubar")
    parser.add_argument("--runs", type=int, default=1, help="repetições (Parte 2 pede 3 a 5)")
    parser.add_argument("--controller", default=os.environ.get("CONTROLLER_URL", "http://localhost:8000"))
    parser.add_argument(
        "--node-timeout",
        type=float,
        default=float(os.environ.get("NODE_TIMEOUT_SECONDS", "6")),
        help="deve bater com NODE_TIMEOUT_SECONDS do controlador",
    )
    parser.add_argument("--grace", type=float, default=4.0, help="tolerância além do timeout para chegar a DEAD")
    parser.add_argument("--restart-deadline", type=float, default=30.0, help="limite para voltar a HEALTHY")
    args = parser.parse_args(argv)

    results: list[RunResult] = []
    with httpx.Client(base_url=args.controller, timeout=3) as client:
        for i in range(args.runs):
            log(f"execução {i + 1}/{args.runs}")
            results.append(run_once(client, args.node, args.node_timeout, args.grace, args.restart_deadline))

    path = save(results)
    passed = sum(r.passed for r in results)
    log(f"{passed}/{len(results)} execuções passaram; resultados em {path.relative_to(REPO_ROOT)}")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
