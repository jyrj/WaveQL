"""WaveQL as CHIA blocks.

Five nodes and one loop. Each node is usable on its own -- that is the point of
shipping them as blocks rather than as a script -- and `waveql_loop` composes
them into one pipeline:

    ChiselMutateNode -> (chipyard build + cosim) -> WaveQLIngestNode
                     -> WaveQLQueryTool -> agent -> FixVerifyNode

The nodes deliberately reuse CHIA's existing Chipyard capability rather than
re-implementing it: ``ChiselBuildNode`` compiles, ``VerilatorRunNode`` runs and
captures PC-triggered waveform windows, ``CosimNode`` provides the architectural
oracle. What WaveQL adds is the mutation source, the JOIN of waveform to commit
log to golden trace, the cost-bounded query surface over it, and mechanical
verification of a proposed repair.
"""

from chia_waveql.state_def import (
    BlameArtifact,
    MutantArtifact,
    RepairArtifact,
    ScreenArtifact,
    StoreArtifact,
)
from chia_waveql.mutate_node import ChiselMutateNode
from chia_waveql.ingest_node import WaveQLIngestNode
from chia_waveql.query_tool import WaveQLQueryTool
from chia_waveql.verify_node import FixVerifyNode
from chia_waveql.screen_node import DetectabilityScreenNode
from chia_waveql.loop import run_loop

__all__ = [
    "MutantArtifact", "ScreenArtifact", "StoreArtifact", "BlameArtifact",
    "RepairArtifact", "ChiselMutateNode", "DetectabilityScreenNode",
    "WaveQLIngestNode", "WaveQLQueryTool", "FixVerifyNode", "run_loop",
]
