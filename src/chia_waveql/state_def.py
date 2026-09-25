"""Typed results passed between WaveQL's CHIA nodes.

Mirrors the shape of ``chia.chipyard.state_def``: plain dataclasses that travel
by value between nodes, so a loop can be replayed from a recorded result without
re-running the step that produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class MutantArtifact:
    """A defect injected into Chisel source, and the ground truth it defines."""

    mutant_id: str
    path: str                  # repo-relative Chisel file
    line: int
    col: int
    operator: str              # eq_to_ne, mux_arm_swap, fire_to_valid, ...
    mutation_class: str        # comparison-inversion, handshake-protocol, ...
    module: str | None         # enclosing Chisel module, resolved transitively
    before: str
    after: str
    context: str               # the original source line
    diff: str                  # unified diff: the reviewable artifact
    sha256_before: str
    sha256_after: str


@dataclass
class ScreenArtifact:
    """Whether an oracle could see the defect, and the evidence if it could."""

    mutant_id: str
    verdict: str               # killed | survived | build-failed | invalid
    kill_kind: str | None      # divergence | assertion | timeout | crash
    killing_stimulus: str | None
    divergence: dict | None
    assertion: str | None
    assertion_src: str | None
    divergence_cycle: int | None
    vcd_path: str | None
    vcd_bytes: int = 0
    window: dict | None = None
    window_covers_divergence: bool = False
    build_seconds: float = 0.0
    run_seconds: float = 0.0
    stimuli_run: list[str] = field(default_factory=list)


@dataclass
class StoreArtifact:
    """A queryable join of waveform, commit log, golden trace and divergence."""

    mutant_id: str
    db_path: str               # ":memory:" when the store was not persisted
    signals: int
    changes: int
    commits: int
    divergences: int
    cycle_source: str          # debug_tsc_reg | clock_edges
    window_lo: int | None
    window_hi: int | None
    scopes: list[str] = field(default_factory=list)
    ingest_seconds: float = 0.0


@dataclass
class BlameArtifact:
    """What an agent concluded, and what it cost. Scored by the harness."""

    mutant_id: str
    arm: str
    seed: int
    model: str
    module: str | None
    signal: str | None
    cycle: int | None
    module_hit: bool
    signal_hit: bool
    cycle_hit: bool
    queries: int
    turns: int
    tokens: int
    cost_usd: float | None
    seconds: float
    stop_reason: str
    error: str | None = None
    tool_calls: list[str] = field(default_factory=list)


@dataclass
class RepairArtifact:
    """A proposed patch and the harness's verdict on it."""

    mutant_id: str
    arm: str
    seed: int
    verdict: str               # fixed | not-fixed | build-failed | no-proposal | rejected
    exact_revert: bool
    same_file_as_bug: bool
    same_line_as_bug: bool
    stimuli_passed: int
    stimuli_total: int
    first_failure: str | None
    reason: str | None
    proposal: dict | None
    build_seconds: float = 0.0
    run_seconds: float = 0.0
