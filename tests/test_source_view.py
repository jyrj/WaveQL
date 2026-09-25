"""Source reading and patch proposal."""
from pathlib import Path

import pytest

from waveql.agent.source import SourceView

SRC = """\
class Decode {
  val is_sys_pc2epc = Wire(Bool())
  is_sys_pc2epc := uop.is_eret && csr.io.status
}
"""


@pytest.fixture
def view(tmp_path: Path) -> SourceView:
    d = tmp_path / "generators/boom/src/main/scala/v3/exu"
    d.mkdir(parents=True)
    (d / "decode.scala").write_text(SRC)
    return SourceView(root=tmp_path)


def test_a_rejected_anchor_is_recorded_not_discarded(view: SourceView):
    """An episode where every anchor missed is a different outcome from one where
    the agent never proposed, and reporting both as "never called propose_fix"
    blames the agent for a harness-shaped failure."""
    with pytest.raises(ValueError):
        view.propose_fix("exu/decode.scala", "no_such_text", "x")
    assert len(view.rejected) == 1
    assert view.rejected[0]["why"] == "no match"
    assert not view.proposals


def test_a_missed_anchor_is_told_what_is_actually_there(view: SourceView):
    """"It does not match" leaves the agent to guess which character was wrong,
    and on the evidence it guesses once and gives up."""
    with pytest.raises(ValueError) as e:
        view.propose_fix("exu/decode.scala",
                         "is_sys_pc2epc := uop.is_eret && csr.status", "x")
    msg = str(e.value)
    assert "Closest lines actually in the file" in msg
    assert "is_sys_pc2epc := uop.is_eret && csr.io.status" in msg


def test_an_accepted_proposal_is_registered_with_its_line(view: SourceView):
    r = view.propose_fix("exu/decode.scala",
                         "uop.is_eret && csr.io.status", "uop.is_eret")
    assert view.proposals and not view.rejected
    assert r.rows[0][0] == "registered"
    assert r.rows[0][2] == 3
