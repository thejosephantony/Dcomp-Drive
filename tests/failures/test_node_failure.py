"""Testes da lógica de espera do cenário de falha.

Não precisam do cluster: o relógio e as respostas do /status são simulados.
O cenário completo roda com: python tests/failures/node_failure.py
"""

import httpx
import pytest

from node_failure import ScenarioError, compose_setting, wait_for_state


class FakeClock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


def states(*sequence):
    it = iter(sequence)

    def read():
        value = next(it)
        if isinstance(value, Exception):
            raise value
        return value

    return read


def test_reaches_target_and_reports_elapsed():
    clock = FakeClock()
    state, elapsed = wait_for_state(
        states("HEALTHY", "HEALTHY", "DEAD"), {"DEAD"}, deadline_s=10, interval_s=1, clock=clock, sleep=clock.sleep
    )
    assert state == "DEAD"
    assert elapsed == 2


def test_on_state_sees_intermediate_suspect():
    clock = FakeClock()
    seen = []
    wait_for_state(
        states("HEALTHY", "SUSPECT", "DEAD"),
        {"DEAD"},
        deadline_s=10,
        interval_s=1,
        clock=clock,
        sleep=clock.sleep,
        on_state=lambda s, e: seen.append((s, e)),
    )
    assert ("SUSPECT", 1) in seen


def test_network_error_is_retried():
    clock = FakeClock()
    state, _ = wait_for_state(
        states(httpx.ConnectError("caiu"), "HEALTHY"),
        {"HEALTHY"},
        deadline_s=10,
        interval_s=1,
        clock=clock,
        sleep=clock.sleep,
    )
    assert state == "HEALTHY"


def test_fails_when_deadline_passes():
    clock = FakeClock()
    with pytest.raises(ScenarioError, match="não alcançado"):
        wait_for_state(
            states(*["SUSPECT"] * 20), {"DEAD"}, deadline_s=3, interval_s=1, clock=clock, sleep=clock.sleep
        )


def slow_states(clock, delay, *sequence):
    """Cada leitura do /status demora `delay` segundos."""
    read = states(*sequence)

    def slow():
        clock.t += delay
        return read()

    return slow


def test_elapsed_is_measured_after_slow_read():
    clock = FakeClock()
    state, elapsed = wait_for_state(
        slow_states(clock, 2.5, "DEAD"), {"DEAD"}, deadline_s=10, interval_s=1, clock=clock, sleep=clock.sleep
    )
    assert state == "DEAD"
    assert elapsed == 2.5


def test_target_arriving_after_deadline_is_rejected():
    clock = FakeClock()
    with pytest.raises(ScenarioError):
        wait_for_state(
            slow_states(clock, 3, "SUSPECT", "DEAD"), {"DEAD"}, deadline_s=5, interval_s=0, clock=clock, sleep=clock.sleep
        )


def test_compose_setting_precedence(tmp_path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("# comentário\nNODE_TIMEOUT_SECONDS=20  # lento\n", encoding="utf-8")

    monkeypatch.delenv("NODE_TIMEOUT_SECONDS", raising=False)
    assert compose_setting("NODE_TIMEOUT_SECONDS", "6", env_file) == "20"

    monkeypatch.setenv("NODE_TIMEOUT_SECONDS", "9")
    assert compose_setting("NODE_TIMEOUT_SECONDS", "6", env_file) == "9"

    monkeypatch.delenv("NODE_TIMEOUT_SECONDS")
    assert compose_setting("NODE_TIMEOUT_SECONDS", "6", tmp_path / "nao_existe") == "6"
