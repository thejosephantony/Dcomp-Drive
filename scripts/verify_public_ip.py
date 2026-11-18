#!/usr/bin/env python3
"""Teste não destrutivo do IP HTTPS público. Não executa falhas ou uploads."""
import json
import ssl
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

BASE = "https://34.193.62.218"
SSL = ssl.create_default_context()


def check(path: str) -> tuple[int, bytes]:
    req = Request(BASE + path, method="GET", headers={"User-Agent": "DComp-Public-Smoke/1.0"})
    try:
        with urlopen(req, timeout=15, context=SSL) as reply:
            return reply.status, reply.read(2_000_000)
    except HTTPError as error:
        return error.code, error.read(1024)


def main() -> int:
    try:
        index_status, index = check("/")
        health_status, _ = check("/health")
        cfg_status, cfg = check("/demo/config")
        status_code, status = check("/status")
        denied = {path: check(path)[0] for path in (
            "/openapi.json", "/docs", "/files/prepare", "/nodes/register", "/_demo/offline"
        )}
        config = json.loads(cfg) if cfg_status == 200 else {}
        nodes = json.loads(status).get("nodes", []) if status_code == 200 else []
        results = {
            "HTTPS válido / UI": index_status == 200 and b"DComp Drive" in index,
            "Health": health_status == 200,
            "Demonstração pública sem token": cfg_status == 200 and config.get("public") is True,
            "Storages registrados": status_code == 200 and len(nodes) >= 5,
            "Endpoints internos bloqueados": all(value in (403, 404) for value in denied.values()),
        }
        for name, okay in results.items():
            print(("PASS" if okay else "FAIL") + "  " + name)
        print("Nós:", len(nodes), "| Códigos privados:", denied)
        return 0 if all(results.values()) else 1
    except (URLError, ValueError, OSError) as exc:
        print("FAIL HTTPS/IP: " + str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
