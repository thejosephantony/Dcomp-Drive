#!/usr/bin/env python3
"""Populate ONLY the isolated EC2 public lab with fictitious demo files.

Run from the EC2 host: python3 scripts/seed_public_demo.py
No third-party dependencies, no credentials and no Docker volume changes.
Existing files are preserved (idempotent). The API is loopback-only.
"""
import json
import sys
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, build_opener, ProxyHandler

BASE = "http://127.0.0.1:18001"
HTTP = build_opener(ProxyHandler({}))
DIRECTORIES = ("/apresentacao", "/apresentacao/documentos", "/apresentacao/relatorios")
SAMPLES = {
    "/demo-replicacao.txt": (
        3,
        "DComp-Drive | RF3 - Replicacao de dados\n"
        "A politica de replicacao exige tres copias confirmadas em storages logicos distintos.\n"
        "Este documento utiliza dados ficticios para uma demonstracao academica.\n\n"
        + "".join(
            f"Registro de teste {i:04d} | payload didatico | integridade SHA-256 | N=3\n"
            for i in range(1, 1001)
        ),
    ),
    "/demo-corrupcao.txt": (
        3,
        "DComp-Drive | RF4 e RF9 - Integridade e criptografia\n"
        "Este arquivo foi criado para testes controlados de corrupcao de UMA replica.\n"
        "O sistema deve detectar inconsistencias por SHA-256 e recompor as replicas.\n"
        "A evidencia AES-256-GCM deve ser demonstrada ANTES da corrupcao.\n",
    ),
    "/demo-falha.txt": (
        3,
        "DComp-Drive | RF5 - Falha logica e recuperacao\n"
        "O controlador monitora os storages por heartbeats.\n"
        "Apos falha de um no, o reparador procura replicas saudaveis em outros nos.\n",
    ),
    "/demo-leitura.txt": (
        3,
        "DComp-Drive | RF1 e RF4 - Download e hash SHA-256\n"
        "Ao baixar este arquivo, o navegador confere os bytes com o hash salvo.\n"
        "Texto de demonstracao, sem dados pessoais ou segredos.\n",
    ),
    "/demo-versoes.txt": (
        3,
        "DComp-Drive | Versao 1\n"
        "Este e o conteudo inicial do arquivo de demonstracao de versoes.\n"
        "Durante a gravacao, envie uma nova versao para o MESMO caminho.\n",
    ),
    "/demo-excluir.txt": (
        3,
        "DComp-Drive | RF1 - Exclusao de arquivos\n"
        "Arquivo descartavel. Exclua-o pelo explorador e confirme a remocao logica.\n",
    ),
    "/apresentacao/documentos/demo-mover.txt": (
        3,
        "DComp-Drive | RF1 - Renomear e mover\n"
        "Primeiro renomeie este arquivo no navegador.\n"
        "Depois mova-o para /apresentacao/relatorios/.\n",
    ),
    "/apresentacao/documentos/roteiro.txt": (
        3,
        "DComp-Drive - Roteiro da demonstracao\n"
        "1. Arquitetura e monitoramento.\n"
        "2. Upload, download, SHA-256 e replicacao.\n"
        "3. Namespace, falha, corrupcao e recuperacao.\n"
        "4. Descoberta, drenagem, concorrencia, criptografia, persistencia e logs.\n",
    ),
    "/apresentacao/relatorios/relatorio.csv": (
        3,
        "periodo,arquivos,replicas_planejadas,estado\n"
        "ensaio-01,3,9,HEALTHY\n"
        "ensaio-02,5,15,HEALTHY\n"
        "ensaio-03,9,27,DEMONSTRACAO\n",
    ),
}


def api(path, method="GET", data=None, content_type=None):
    headers = {"User-Agent": "DComp-Drive-Demo-Seed/1.0"}
    if content_type:
        headers["Content-Type"] = content_type
    request = Request(BASE + path, data=data, method=method, headers=headers)
    try:
        with HTTP.open(request, timeout=60) as response:
            raw = response.read(2_000_000)
            return response.status, json.loads(raw) if raw else {}
    except HTTPError as error:
        raw = error.read(4000)
        try:
            detail = json.loads(raw).get("detail", raw.decode("utf-8", errors="replace"))
        except ValueError:
            detail = raw.decode("utf-8", errors="replace")
        return error.code, {"detail": detail}


def main():
    try:
        code, config = api("/demo/config")
        if code != 200 or config.get("public") is not True:
            print("ERRO: controlador nao esta em modo publico isolado.")
            return 1
        code, cluster = api("/status")
        healthy = sum(node["state"] == "HEALTHY" for node in cluster.get("nodes", [])) if code == 200 else 0
        if healthy < 5:
            print(f"ERRO: apenas {healthy} storages saudaveis. Aguarde o registro e tente novamente.")
            return 1
        print(f"Laboratorio publico confirmado | storages saudaveis: {healthy}")
        for directory in DIRECTORIES:
            code, result = api(
                "/files/directories", "POST",
                json.dumps({"path": directory}).encode("utf-8"), "application/json",
            )
            if code in (201, 409):
                print(f"PASTA {directory} | HTTP {code}")
            else:
                raise RuntimeError(f"Falha ao criar pasta {directory}: HTTP {code} {result}")
        created = skipped = 0
        for path, (factor, text) in SAMPLES.items():
            params = urlencode({"path": path})
            status, detail = api("/files/metadata?" + params)
            if status == 200:
                skipped += 1
                print(f"EXISTENTE {path} (preservado)")
                continue
            if status != 404:
                raise RuntimeError(f"Falha ao verificar {path}: HTTP {status} {detail}")
            data = text.encode("utf-8")
            params = urlencode({"path": path, "replication_factor": factor})
            status, result = api(
                "/files/content?" + params, "PUT", data, "application/octet-stream"
            )
            if status != 201:
                raise RuntimeError(f"Upload {path}: HTTP {status} {result}")
            replicas = len(result.get("replicas", []))
            print(f"OK {path} | {len(data)} bytes | {replicas}/{factor} replicas")
            created += 1
        print(f"CONCLUIDO: {created} arquivos criados; {skipped} ja existentes.")
        print("Abra https://34.193.62.218/ e clique em Atualizar.")
        print("RF8: o botao de concorrencia cria /demo-concorrencia.txt automaticamente.")
        return 0
    except (URLError, OSError, RuntimeError, ValueError, KeyError) as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
