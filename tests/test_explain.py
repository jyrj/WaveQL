"""Expression evaluation and value-directed blame over generated Verilog."""
from pathlib import Path

import pytest

from waveql.ingest.explain import (Evaluator, Explainer, parse_expr, responsible,
                                   tokenize)
from waveql.ingest.netlist import HierNetlist


class _NoNetlist:
    def module_at(self, p): return "M"
    def define(self, m, n): return None
    def boundary(self, p, n): return None
    def step(self, p, s): return []


def ev_with(env, widths=None):
    e = Evaluator(_NoNetlist(), lambda p, n: env.get(n))
    w = widths or {}
    e.width = lambda p, n: w.get(n, (1, 1))[0] if isinstance(w.get(n), tuple) else w.get(n, 1)
    e.dims = lambda p, n: w.get(n, (1, 1)) if isinstance(w.get(n), tuple) else (w.get(n, 1), 1)
    return e


@pytest.mark.parametrize("src,want", [
    ("a & b", 0), ("a | b", 1), ("~b", 1), ("a ? c : d", 5), ("b ? c : d", 3),
    ("c == 3'h5", 1), ("c != 5", 0), ("{a, b}", 2), ("c + d", 8), ("c > d", 1),
    ("{2{a}}", 3), ("(a | b) & ~b", 1), ("a & ~b", 1),
])
def test_evaluates_firtool_expression_forms(src, want):
    e = ev_with({"a": 1, "b": 0, "c": 5, "d": 3}, {"c": 4, "d": 4})
    assert e.eval(parse_expr(src), "p") == want


def test_bit_select_versus_packed_array_select():
    """`wire [31:0][6:0] x` makes x[i] a 7-bit ELEMENT, not a bit. Reading every
    declaration as one flat vector made every such index silently wrong."""
    flat = ev_with({"w": 0xF0}, {"w": (8, 1)})
    assert flat.eval(parse_expr("w[4]"), "p") == 1
    packed = ev_with({"g": (3 << 7) | 5}, {"g": (14, 7)})
    assert packed.eval(parse_expr("g[0]"), "p") == 5
    assert packed.eval(parse_expr("g[1]"), "p") == 3


def test_unknown_propagates_rather_than_guessing():
    e = ev_with({"a": 1})
    assert e.eval(parse_expr("a & missing"), "p") is None


def test_responsible_blames_only_what_forces_the_value():
    """An AND is 0 because of its false operands; blaming the true ones as well
    is what turns a chain back into a cone."""
    e = ev_with({"a": 1, "b": 0})
    t = parse_expr("a & b")
    e.eval(t, "p")
    assert [n.text for n in responsible(t)] == ["b"]

    t2 = parse_expr("a | b")
    e.eval(t2, "p")
    assert [n.text for n in responsible(t2)] == ["a"]


def test_responsible_mux_takes_the_selector_and_the_live_arm():
    e = ev_with({"c": 1, "x": 7, "y": 9}, {"x": 4, "y": 4})
    t = parse_expr("c ? x : y")
    e.eval(t, "p")
    assert [n.text for n in responsible(t)] == ["c", "x"]


CHILD = """\
module Child(\t// @[a.scala:1:7]
  input  io_in,\t// @[a.scala:2:14]
  output io_out\t// @[a.scala:2:14]
);
  wire inner = io_in & 1'h1;\t// @[a.scala:10:20]
  assign io_out = inner;\t// @[a.scala:11:9]
endmodule
"""
PARENT = """\
module Parent(\t// @[b.scala:1:7]
  input src
);
  wire _child_io_out;\t// @[b.scala:5:20]
  wire gate = src & _child_io_out;\t// @[b.scala:7:18]
  Child child (\t// @[b.scala:20:22]
    .io_in  (src),\t// @[b.scala:21:9]
    .io_out (_child_io_out)\t// @[b.scala:22:9]
  );
endmodule
"""


@pytest.fixture
def gen(tmp_path: Path) -> Path:
    c = tmp_path / "gen-collateral"
    c.mkdir()
    (c / "Child.sv").write_text(CHILD)
    (c / "Parent.sv").write_text(PARENT)
    return tmp_path


def test_evaluator_crosses_into_a_child_module(gen: Path):
    """A parent's untraced wires are mostly child outputs; stopping at them left
    most of BoomCore unevaluable."""
    nl = HierNetlist(gen, {"top": "Parent", "top.child": "Child"})
    e = Evaluator(nl, lambda p, n: 1 if (p == "top" and n == "src") else None)
    assert e.signal("top", "_child_io_out") == 1
    assert e.signal("top", "gate") == 1


def test_why_reports_the_chisel_line_of_each_link(gen: Path):
    nl = HierNetlist(gen, {"top": "Parent", "top.child": "Child"})
    e = Evaluator(nl, lambda p, n: 0 if (p == "top" and n == "src") else None)
    chain = Explainer(nl, e).why("top", "gate", max_depth=4)
    assert chain[0].signal == "gate" and chain[0].value == 0
    blamed = {w.signal for w in chain[1:]}
    assert "src" in blamed
    assert any("b.scala" in str(r) for w in chain for r in w.refs)


def test_arithmetic_shift_right_sign_extends():
    """A logical shift where an arithmetic one is meant returns a plausible wrong
    number rather than an error: 0xF0 >>> 4 as int8 is 0xFF, not 0x0F. firtool
    emits no >>> in this design (0 occurrences in 44,722 lines of generated
    Verilog), so this is latent -- but a silently wrong value is precisely the
    failure this evaluator exists to avoid."""
    e = ev_with({"neg": 0xF0, "pos": 0x70}, {"neg": 8, "pos": 8})
    assert e.eval(parse_expr("neg >>> 4"), "p") == 0xFF
    assert e.eval(parse_expr("pos >>> 4"), "p") == 0x07
    assert e.eval(parse_expr("neg >> 4"), "p") == 0x0F      # logical, unchanged
