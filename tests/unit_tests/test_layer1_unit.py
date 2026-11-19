"""Unit tests for Layer 1 Anti-Jamming Reinforcement Learning adapter."""

from __future__ import annotations

import pytest

from common.errors import ErrorCode
from configuration.settings import Layer1Config
from layer1_adapter.adapter import Layer1AntiJammingAdapter


@pytest.mark.parametrize("jammer_type", ["sweep", "reactive", "random"])
def test_layer1_loads_all_jammer_policies(jammer_type: str) -> None:
    cfg = Layer1Config(jammer_type=jammer_type, max_hop_attempts=5, seed=7)
    adapter = Layer1AntiJammingAdapter(cfg)

    decision = adapter.allocate_channel()
    assert decision.allowed is True
    assert decision.error_code == ErrorCode.OK.value
    assert 0 <= decision.selected_channel < 8
    assert decision.jammer_type == jammer_type
    assert decision.attempts >= 1


def test_layer1_sweep_jammer_achieves_high_pdr() -> None:
    cfg = Layer1Config(jammer_type="sweep", max_hop_attempts=1, online_learning=False, seed=42)
    adapter = Layer1AntiJammingAdapter(cfg)

    successes = 0
    trials = 50
    for _ in range(trials):
        dec = adapter.allocate_channel(max_attempts=1)
        if dec.allowed:
            successes += 1
    assert successes >= 45
    assert adapter.cumulative_pdr >= 90.0


def test_layer1_link_interruption_and_recovery() -> None:
    adapter = Layer1AntiJammingAdapter(Layer1Config(jammer_type="sweep", seed=12))

    adapter.set_link_status(False)
    down_dec = adapter.allocate_channel()
    assert down_dec.allowed is False
    assert down_dec.error_code == ErrorCode.L1_LINK_DOWN.value
    assert down_dec.selected_channel == -1

    adapter.set_link_status(True)
    up_dec = adapter.allocate_channel()
    assert up_dec.allowed is True
    assert up_dec.error_code == ErrorCode.OK.value


def test_layer1_forced_jamming_exhausts_hops_and_recovers() -> None:
    adapter = Layer1AntiJammingAdapter(Layer1Config(jammer_type="sweep", max_hop_attempts=3, seed=9))
    adapter.force_jam_next(3)

    jammed = adapter.allocate_channel()
    assert jammed.allowed is False
    assert jammed.error_code == ErrorCode.L1_CHANNEL_JAMMED.value
    assert jammed.attempts == 3
    assert len(jammed.hop_history) == 3

    recovered = adapter.allocate_channel()
    assert recovered.allowed is True
    assert recovered.error_code == ErrorCode.OK.value


def test_layer1_rejects_unknown_jammer_type() -> None:
    with pytest.raises(ValueError):
        Layer1AntiJammingAdapter(Layer1Config(jammer_type="adaptive_invalid"))
