"""RF3/RF7: teste não destrutivo de N=2, domínios lógicos e capacidade na EC2.

Executar dentro do controlador: python scripts/test_rf3_allocation_live.py
Cria apenas /rf3-n2-demo.bin; não modifica .env, volumes ou réplicas preexistentes.
"""
import hashlib
import json
import os

import httpx

BASE = os.getenv("DCOMP_CONTROLLER_URL", "http://127.0.0.1:8000").rstrip("/")
PATH = "/rf3-n2-demo.bin"
CONTENT = b"DComp Drive: replicacao configuravel N=2 na Parte 1.\n"
DIGEST = hashlib.sha256(CONTENT).hexdigest()


def ensure(response: httpx.Response, expected: int, context: str) -> dict:
    if response.status_code != expected:
        raise RuntimeError(f"{context}: HTTP {response.status_code}: {response.text[:300]}")
    return response.json()


def main() -> None:
    with httpx.Client(timeout=30.0) as client:
        prep = ensure(client.post(
            f"{BASE}/files/prepare",
            json={
                "path": PATH, "size_bytes": len(CONTENT),
                "sha256": DIGEST, "replication_factor": 2,
            },
        ), 201, "preparação N=2")
        targets = prep["targets"]
        assert prep["desired_replicas"] == 2 and len(targets) == 2, prep
        assert len({t["node_id"] for t in targets}) == 2, targets
        assert len({t["machine_id"] for t in targets}) == 2, targets
        assert len({t["disk_id"] for t in targets}) == 2, targets
        write_id = prep["write_id"]
        committed = False
        stored = []
        try:
            for target in targets:
                base = target["address"].rstrip("/")
                r = client.put(
                    f"{base}/objects/{write_id}",
                    content=CONTENT,
                    headers={"X-Write-Id": write_id, "X-Expected-Sha256": DIGEST},
                )
                response = ensure(r, 201, f"gravação {target['node_id']}")
                assert response.get("controller_acked") is True, response
                assert response.get("sha256") == DIGEST, response
                verify = ensure(
                    client.get(f"{base}/objects/{write_id}/verify"), 200,
                    f"verificação física {target['node_id']}",
                )
                assert verify["match"] is True, verify
                stored.append(base)
            ensure(client.post(f"{BASE}/files/uploads/{write_id}/commit"), 200, "commit N=2")
            committed = True
        finally:
            if not committed:
                try:
                    client.post(f"{BASE}/files/uploads/{write_id}/abort")
                except httpx.HTTPError:
                    pass
                for base in stored:
                    try:
                        client.delete(f"{base}/objects/{write_id}")
                    except httpx.HTTPError:
                        pass

        info = ensure(client.get(f"{BASE}/files/metadata", params={"path": PATH}), 200, "metadados N=2")
        assert info["replication_factor"] == 2 and len(info["replicas"]) == 2, info
        assert info["sha256"] == DIGEST and info["status"] == "HEALTHY", info
        downloaded = client.get(f"{targets[0]['address'].rstrip('/')}/objects/{write_id}")
        assert downloaded.status_code == 200 and downloaded.content == CONTENT

        insufficient_topology = client.post(f"{BASE}/files/prepare", json={
            "path": "/rf3-seis-destinos-recusado.bin",
            "size_bytes": 1, "sha256": DIGEST, "replication_factor": 6,
        })
        assert insufficient_topology.status_code == 507, insufficient_topology.text

        insufficient_capacity = client.post(f"{BASE}/files/prepare", json={
            "path": "/rf7-sem-espaco-recusado.bin",
            "size_bytes": 10**12, "sha256": DIGEST, "replication_factor": 3,
        })
        assert insufficient_capacity.status_code == 507, insufficient_capacity.text

        print(json.dumps({
            "result": "PASS",
            "path": PATH,
            "requested_N": 2,
            "replicas": [{"node_id": t["node_id"], "machine_id": t["machine_id"],
                          "disk_id": t["disk_id"]} for t in targets],
            "status": info["status"],
            "download_sha256_verified": True,
            "N_6_rejected_http": insufficient_topology.status_code,
            "over_capacity_rejected_http": insufficient_capacity.status_code,
            "caveat": "maquinas logicas isoladas em contêineres; todas no mesmo host EC2",
        }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
