"""RF8: ensaio reproduzível de escritas e leituras concorrentes no controlador real.

Usa um caminho DESCARTÁVEL: /rf8-concorrencia-demo.bin.
Execução no controller: python scripts/test_concurrency_live.py
"""
import concurrent.futures
from datetime import datetime, timezone
import hashlib
import json
import os
import threading
import time

import httpx

URL = os.getenv("DCOMP_CONTROLLER_URL", "http://127.0.0.1:8000").rstrip("/")
PATH = "/rf8-concorrencia-demo.bin"
SEED = b"seed RF8 " * 100
A = b"CLIENTE A: " * 100_000
B = b"CLIENTE B: " * 100_000
KNOWN = {hashlib.sha256(x).hexdigest(): len(x) for x in (SEED, A, B)}


def put(data: bytes, barrier=None):
    if barrier is not None:
        barrier.wait()
    with httpx.Client(timeout=90) as client:
        r = client.put(f"{URL}/files/content", params={"path": PATH}, content=data)
        return r.status_code, r.text[:300]


def get():
    with httpx.Client(timeout=90) as client:
        r = client.get(f"{URL}/files/content", params={"path": PATH})
        if r.status_code != 200:
            return r.status_code, False, None
        sha = hashlib.sha256(r.content).hexdigest()
        return 200, KNOWN.get(sha) == len(r.content), sha


def main():
    seed_status, seed_msg = put(SEED)
    if seed_status != 201:
        raise SystemExit(f"Falha ao criar arquivo RF8: {seed_status} {seed_msg}")
    barrier = threading.Barrier(2)
    reads = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        fa = pool.submit(put, A, barrier)
        fb = pool.submit(put, B, barrier)
        for _ in range(12):
            reads.append(get())
            time.sleep(0.1)
        writers = [fa.result(), fb.result()]
    final = get()
    result = {
        "at_utc": datetime.now(timezone.utc).isoformat(),
        "path": PATH,
        "writers": [item[0] for item in writers],
        "reads": [{"http": status, "hash_valid": ok, "sha256": sha}
                  for status, ok, sha in reads],
        "final": {"http": final[0], "hash_valid": final[1], "sha256": final[2]},
        "result": "PASS" if (
            all(code in (201, 409) for code, _ in writers)
            and any(code == 201 for code, _ in writers)
            and all(ok for status, ok, _ in reads if status == 200)
            and final[0] == 200 and final[1]
        ) else "FAIL",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if result["result"] != "PASS":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
