"""Adapter wrapping the unmodified Layer 1 drone-rl-agent repository."""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from typing import Any

from common.errors import ErrorCode
from common.models import ChannelDecision
from configuration.settings import Layer1Config

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAYER1_DIR = _REPO_ROOT / "vendor" / "layer1_drone_rl_agent"
if str(_LAYER1_DIR) not in sys.path:
    sys.path.insert(0, str(_LAYER1_DIR))

from drone_agent import ChannelSelector  # type: ignore  # noqa: E402
from q_learning import QLearningAgent  # type: ignore  # noqa: E402
from simulator import CommunicationSimulator  # type: ignore  # noqa: E402
from train import train_agent  # type: ignore  # noqa: E402


class Layer1AntiJammingAdapter:
    """Monitors simulated RF channel state and selects non-jammed channels using RL."""

    def __init__(self, config: Layer1Config | None = None) -> None:
        self.config = config or Layer1Config()
        if self.config.jammer_type not in CommunicationSimulator.JAMMER_TYPES:
            raise ValueError(
                f"Unsupported jammer_type '{self.config.jammer_type}'. "
                f"Expected one of {CommunicationSimulator.JAMMER_TYPES}."
            )

        self.simulator = CommunicationSimulator(
            num_channels=self.config.num_channels,
            jammer_type=self.config.jammer_type,
            reactive_hit_prob=self.config.reactive_hit_prob,
            seed=self.config.seed,
        )
        self.simulator.reset()

        self.selector = self._load_selector(self.config.jammer_type)
        self.link_connected: bool = True
        self.active_channel: int = 0
        self.total_attempts: int = 0
        self.successful_attempts: int = 0
        self._forced_jam_remaining: int = 0

    def _load_selector(self, jammer_type: str) -> ChannelSelector:
        """Load pre-trained Q-table from vendor/layer1_drone_rl_agent or vendor/results."""
        local_q = _LAYER1_DIR / f"q_table_{jammer_type}.json"
        results_q = _REPO_ROOT / "vendor" / "results" / f"q_table_{jammer_type}.json"

        selector = ChannelSelector.__new__(ChannelSelector)
        if local_q.exists():
            selector.agent = QLearningAgent.load(str(local_q))
        elif results_q.exists():
            selector.agent = QLearningAgent.load(str(results_q))
        else:
            selector.agent = train_agent(jammer_type, seed=self.config.seed or 0)
        selector.agent.exploration_rate = 0.0
        return selector

    def set_link_status(self, connected: bool) -> None:
        """Simulate RF link interruption or restoration."""
        self.link_connected = bool(connected)

    def force_jam_next(self, count: int = 1) -> None:
        """Force the next `count` channel attempts to suffer simulated jamming."""
        if count < 0:
            raise ValueError("count must be non-negative")
        self._forced_jam_remaining = int(count)

    def set_jammer_type(self, jammer_type: str) -> None:
        """Switch the active jammer model and reload its Q-learning policy."""
        if jammer_type not in CommunicationSimulator.JAMMER_TYPES:
            raise ValueError(f"Unsupported jammer_type: {jammer_type}")
        self.config.jammer_type = jammer_type
        self.simulator = CommunicationSimulator(
            num_channels=self.config.num_channels,
            jammer_type=jammer_type,
            reactive_hit_prob=self.config.reactive_hit_prob,
            seed=self.config.seed,
        )
        self.simulator.reset()
        self.selector = self._load_selector(jammer_type)

    @property
    def cumulative_pdr(self) -> float:
        if self.total_attempts == 0:
            return 100.0
        return round(100.0 * self.successful_attempts / self.total_attempts, 2)

    def allocate_channel(self, max_attempts: int | None = None) -> ChannelDecision:
        """Select a non-jammed channel via Q-learning, hopping up to `max_attempts`."""
        start = time.perf_counter()

        if not self.link_connected:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
            return ChannelDecision(
                allowed=False,
                selected_channel=-1,
                observed_state=self.simulator.get_state(),
                jammer_type=self.config.jammer_type,
                reward=-1,
                attempts=0,
                hop_history=[],
                cumulative_pdr=self.cumulative_pdr,
                error_code=ErrorCode.L1_LINK_DOWN.value,
                reason="RF communication link is disconnected",
                latency_ms=latency_ms,
            )

        attempts_limit = max_attempts if max_attempts is not None else self.config.max_hop_attempts
        attempts_limit = max(1, int(attempts_limit))

        hop_history: list[dict[str, Any]] = []
        last_channel = -1
        last_state = self.simulator.get_state()
        last_reward = -1

        try:
            for attempt_idx in range(1, attempts_limit + 1):
                state = self.simulator.get_state()
                channel = int(self.selector.select_channel(state))
                if not (0 <= channel < self.config.num_channels):
                    latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
                    return ChannelDecision(
                        allowed=False,
                        selected_channel=channel,
                        observed_state=state,
                        jammer_type=self.config.jammer_type,
                        reward=-1,
                        attempts=attempt_idx,
                        hop_history=hop_history,
                        cumulative_pdr=self.cumulative_pdr,
                        error_code=ErrorCode.L1_INVALID_CHANNEL.value,
                        reason=f"Selected channel {channel} outside valid range",
                        latency_ms=latency_ms,
                    )

                reward, success = self.simulator.step(channel)
                if self._forced_jam_remaining > 0:
                    self._forced_jam_remaining -= 1
                    reward = -1
                    success = False

                next_state = self.simulator.get_state()
                if self.config.online_learning:
                    self.selector.update(state, channel, reward, next_state, explore=0.0)

                self.total_attempts += 1
                if success:
                    self.successful_attempts += 1

                hop_history.append(
                    {
                        "attempt": attempt_idx,
                        "observed_state": state,
                        "selected_channel": channel,
                        "reward": reward,
                        "success": success,
                    }
                )
                last_channel = channel
                last_state = state
                last_reward = reward

                if success:
                    self.active_channel = channel
                    latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
                    return ChannelDecision(
                        allowed=True,
                        selected_channel=channel,
                        observed_state=state,
                        jammer_type=self.config.jammer_type,
                        reward=reward,
                        attempts=attempt_idx,
                        hop_history=hop_history,
                        cumulative_pdr=self.cumulative_pdr,
                        error_code=ErrorCode.OK.value,
                        reason=(
                            f"Channel {channel} acquired on attempt {attempt_idx} "
                            f"(jammer={self.config.jammer_type})"
                        ),
                        latency_ms=latency_ms,
                    )

            latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
            return ChannelDecision(
                allowed=False,
                selected_channel=last_channel,
                observed_state=last_state,
                jammer_type=self.config.jammer_type,
                reward=last_reward,
                attempts=attempts_limit,
                hop_history=hop_history,
                cumulative_pdr=self.cumulative_pdr,
                error_code=ErrorCode.L1_CHANNEL_JAMMED.value,
                reason=(
                    f"All {attempts_limit} channel selection attempts jammed "
                    f"(jammer={self.config.jammer_type})"
                ),
                latency_ms=latency_ms,
            )
        except Exception as exc:
            latency_ms = round((time.perf_counter() - start) * 1000.0, 4)
            return ChannelDecision(
                allowed=False,
                selected_channel=last_channel,
                observed_state=last_state,
                jammer_type=self.config.jammer_type,
                reward=-1,
                attempts=len(hop_history),
                hop_history=hop_history,
                cumulative_pdr=self.cumulative_pdr,
                error_code=ErrorCode.L1_INTERNAL_ERROR.value,
                reason=f"Layer 1 error: {exc}",
                latency_ms=latency_ms,
            )

    def get_metrics(self) -> dict[str, Any]:
        return {
            "jammer_type": self.config.jammer_type,
            "num_channels": self.config.num_channels,
            "active_channel": self.active_channel,
            "link_connected": self.link_connected,
            "total_attempts": self.total_attempts,
            "successful_attempts": self.successful_attempts,
            "cumulative_pdr_percent": self.cumulative_pdr,
        }
