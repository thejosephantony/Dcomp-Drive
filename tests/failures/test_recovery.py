"""Testes da regra que decide quando o arquivo foi recuperado (sem cluster)."""

from recovery import RECOVERED, recovery_label


def meta(status, *nodes):
    return {"status": status, "replicas": [{"node_id": n} for n in nodes]}


def test_recovered_when_n_replicas_without_victim():
    assert recovery_label(meta("HEALTHY", "storage-2", "storage-3", "storage-5"), "storage-1", 3) == RECOVERED


def test_not_recovered_while_degraded():
    assert recovery_label(meta("UNDER_REPLICATED", "storage-2", "storage-3"), "storage-1", 3) == "UNDER_REPLICATED"
    assert recovery_label(meta("RECOVERING", "storage-2", "storage-3"), "storage-1", 3) == "RECOVERING"


def test_healthy_before_failure_is_detected_is_not_recovery():
    # Logo após o stop o controlador ainda lista o nó parado como réplica viva.
    assert recovery_label(meta("HEALTHY", "storage-1", "storage-2", "storage-3"), "storage-1", 3) == "HEALTHY"


def test_not_recovered_with_fewer_than_n_replicas():
    assert recovery_label(meta("HEALTHY", "storage-2", "storage-3"), "storage-1", 3) == "HEALTHY"
