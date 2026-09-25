"""WaveQLQueryTool — the cost-bounded query surface an agent is given.

A waveform viewer with no screen. Eight operations over the joined store, each
capped and each logged, so "queries per fix" is a measured quantity rather than
an estimate.

Three properties are load-bearing and were each learned by running it:

* **Discovery is an operation.** A MediumBoom waveform has ~72k generated signal
  names and the parser answers an unknown path with silence. Without
  ``find_signals`` an agent confidently queries a name that does not exist, gets
  an empty table, and concludes the signal was quiet.
* **Answers are asof, not range-filtered.** A waveform stores value *changes*, so
  "what was X at cycle N" is the last change at or before N. A range filter
  returns nothing for a signal holding its value, which reads as "no data".
* **Every answer is capped and says so.** An agent that cannot tell a prefix from
  a complete answer will reason confidently about the prefix.
"""

from __future__ import annotations

import logging

from chia.base.ChiaFunction import ChiaFunction

from waveql.store.store import WaveQLStore
from waveql.tool.api import WaveQLTool, declarations, dispatch


class WaveQLQueryTool:
    """CHIA-facing wrapper around the agent tool surface."""

    logging_name = "WaveQLQueryTool"

    def __init__(self, store: WaveQLStore, logging_level: int = logging.INFO,
                 gen_src: str | None = None):
        netlist = None
        if gen_src:
            from pathlib import Path as _P

            from waveql.ingest.netlist import HierNetlist
            from waveql.ingest.srcmap import SourceMap
            netlist = HierNetlist(_P(gen_src), SourceMap(_P(gen_src)).inst2mod)
        self.tool = WaveQLTool(store=store, netlist=netlist)
        self.logger = logging.getLogger(self.logging_name)
        self.logger.setLevel(logging_level)

    @staticmethod
    def declarations() -> list[dict]:
        """Function-calling schemas, shared by every model seat.

        Declared once so all arms describe the tool identically: the task
        statement and success contract stay byte-identical across arms, and only
        the tools differ -- because the tools are the independent variable.
        """
        return declarations()

    @ChiaFunction()
    def call(self, name: str, args: dict) -> str:
        """Run one tool call and render it, capped, for the model's context."""
        return dispatch(self.tool, name, args)

    @property
    def queries(self) -> int:
        return self.tool.store.query_count
