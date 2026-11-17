"""Contadores simples por processo para métricas de dados do controlador.

Reiniciam após reinício do controller; não são banco de auditoria.
"""
from threading import Lock

_lock = Lock()
_totals = {"upload_bytes": 0, "download_bytes": 0}


def record_transfer(*, upload_bytes: int = 0, download_bytes: int = 0) -> None:
    with _lock:
        _totals["upload_bytes"] += max(0, upload_bytes)
        _totals["download_bytes"] += max(0, download_bytes)


def counters_snapshot(elapsed_seconds: float) -> dict:
    with _lock:
        uploaded = _totals["upload_bytes"]
        downloaded = _totals["download_bytes"]
    elapsed = max(elapsed_seconds, 0.001)
    return {
        "upload_bytes_total": uploaded,
        "download_bytes_total": downloaded,
        "upload_throughput_avg_bytes_per_sec": round(uploaded / elapsed, 2),
        "download_throughput_avg_bytes_per_sec": round(downloaded / elapsed, 2),
    }
