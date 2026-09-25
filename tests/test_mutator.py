"""Tests for the Chisel source mutator.

The mutator is the part of this project where a silent bug is most expensive:
a wrong mutation site becomes wrong ground truth, and every localization number
computed against it is wrong in a way no downstream check can detect. So these
tests are aimed squarely at the cases where a naive implementation is wrong but
still *looks* right -- comment and string handling, operator boundaries, and
inheritance chains.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from waveql.mutator.scala_lex import (
    RegionKind,
    classify,
    code_mask,
    find_call,
    match_paren,
    operator_tokens,
    split_args,
)
from waveql.mutator.operators import enumerate_sites
from waveql.mutator.engine import (
    ModuleResolver,
    enclosing_module,
    mutated,
    sample_class_balanced,
    scopes,
)


def kinds(src: str) -> list[tuple[str, str]]:
    return [(r.kind.value, src[r.start : r.end]) for r in classify(src)]


# --- lexer -------------------------------------------------------------------

def test_regions_tile_the_input_exactly():
    src = 'val a = 1 // c\n/* b */ val s = "x"\n'
    pos = 0
    for r in classify(src):
        assert r.start == pos
        pos = r.end
    assert pos == len(src)


def test_block_comments_nest():
    """Scala nests block comments; a C-style scanner stops at the first `*/`."""
    src = "val a = 1\n/* outer /* inner */ still comment */\nval b = 2"
    regions = classify(src)
    comment = [r for r in regions if r.kind is RegionKind.BLOCK_COMMENT]
    assert len(comment) == 1
    assert src[comment[0].start : comment[0].end].endswith("still comment */")
    assert "val b = 2" in src[comment[0].end :]


def test_operators_inside_strings_and_comments_are_not_code():
    src = 'printf("a === b")  // c === d\nval x = p === q'
    toks = [t.text for t in operator_tokens(src)]
    assert toks.count("===") == 1, toks


def test_triple_quoted_string_without_escape_processing():
    src = 'val s = """a \\""" + b === c'
    mask = code_mask(src)
    # The `===` here follows the literal; what matters is that the scan does not
    # run off the end of the file and that live code resumes.
    assert len(mask) == len(src)


def test_char_literal_versus_symbol_literal():
    """`'a'` is a char; `'sym` has no closing quote and must not swallow the line."""
    src = "val c = 'x'\nval y = a === b"
    assert any(k == "char" for k, _ in kinds(src))
    assert [t.text for t in operator_tokens(src)].count("===") == 1


def test_unterminated_symbol_quote_does_not_eat_the_file():
    src = "val s = 'sym\nval y = a === b"
    assert [t.text for t in operator_tokens(src)].count("===") == 1


# --- maximal munch -----------------------------------------------------------

@pytest.mark.parametrize(
    "src,expected",
    [
        ("a === b", ["==="]),
        ("a =/= b", ["=/="]),
        ("a <> b", ["<>"]),          # Chisel bulk connect: NOT `<` then `>`
        ("a << b", ["<<"]),          # shift: NOT `<`
        ("a <= b", ["<="]),
        ("m => n", ["=>"]),
        ("k -> v", ["->"]),
        ("x := y", [":="]),
        ("for (i <- s)", ["<-"]),
    ],
)
def test_maximal_munch_keeps_operators_whole(src, expected):
    assert [t.text for t in operator_tokens(src)] == expected


def test_no_fragment_mutations_of_compound_operators():
    """`<>`, `<<`, `<-` must never yield a `<` mutation site."""
    src = "class M { val a = x <> y; val b = z << 2; for (i <- s) {} }"
    ops = {s.operator for s in enumerate_sites("M.scala", src)}
    assert "lt_to_le" not in ops


# --- delimiter handling ------------------------------------------------------

def test_split_args_is_top_level_only():
    src = "Mux(sel, Cat(a, b), d)"
    calls = find_call(src, "Mux")
    assert len(calls) == 1
    _, lp, rp = calls[0]
    args = split_args(src, lp, rp)
    assert [src[a:b].strip() for a, b in args] == ["sel", "Cat(a, b)", "d"]


def test_find_call_requires_a_whole_word():
    """`Mux` must not match inside `MuxCase`, or the rewrite will not compile."""
    src = "val a = MuxCase(d, s); val b = Mux(c, x, y)"
    assert len(find_call(src, "Mux")) == 1


def test_match_paren_ignores_parens_in_strings():
    src = 'f(printf("("), b)'
    lp = src.index("(")
    assert match_paren(src, lp) == len(src) - 1


# --- operators ---------------------------------------------------------------

def test_regnext_drop_only_single_argument_form():
    src = "class M { val a = RegNext(x); val b = RegNext(y, init) }"
    ops = [s for s in enumerate_sites("M.scala", src) if s.operator == "regnext_drop"]
    assert len(ops) == 1
    assert ops[0].before == "RegNext(x)"
    assert ops[0].after == "(x)"


def test_mux_arm_swap_produces_valid_swap():
    src = "class M { val c = Mux(sel, a, b) }"
    site = next(s for s in enumerate_sites("M.scala", src) if s.operator == "mux_arm_swap")
    assert site.apply(src) == "class M { val c = Mux(sel, b, a) }"


def test_mux_with_identical_arms_is_not_a_site():
    """Swapping identical arms is a no-op mutant: it would never be killed."""
    src = "class M { val c = Mux(sel, a, a) }"
    assert not [s for s in enumerate_sites("M.scala", src) if s.operator == "mux_arm_swap"]


def test_when_negation_wraps_the_whole_condition():
    src = "class M { when (a && b) { x := y } }"
    site = next(s for s in enumerate_sites("M.scala", src) if s.operator == "when_negate")
    assert site.apply(src) == "class M { when (!(a && b)) { x := y } }"


def test_fire_to_valid():
    src = "class M { when (io.deq.fire) { x := y } }"
    site = next(s for s in enumerate_sites("M.scala", src) if s.operator == "fire_to_valid")
    assert site.apply(src) == "class M { when (io.deq.valid) { x := y } }"


def test_fire_does_not_match_a_longer_identifier():
    src = "class M { val a = io.fired; val b = io.fire_count }"
    assert not [s for s in enumerate_sites("M.scala", src) if s.operator == "fire_to_valid"]


def test_handshake_drop_ready_handles_indexed_paths():
    """The real BOOM form: `metaReadArb.io.in(2).ready && dataReadArb.io.in(1).ready`."""
    src = "class M { io.a.ready := metaArb.io.in(2).ready && dataArb.io.in(1).ready }"
    sites = [s for s in enumerate_sites("M.scala", src) if "handshake_drop_ready" in s.operator]
    assert sites, "indexed selector path was not recognised"
    out = sites[0].apply(src)
    assert "&&" not in out
    assert out.count(".ready") == 2   # the assignment target plus one survivor


def test_every_site_applies_and_changes_the_text():
    src = textwrap.dedent(
        """
        class M extends Module {
          val q = RegNext(io.in)
          when (io.a.valid && io.b.ready) { c := Mux(s, x, y) }
          val n = idx + 1.U
          val e = true.B
        }
        """
    )
    sites = enumerate_sites("M.scala", src)
    assert sites
    for s in sites:
        assert s.apply(src) != src


# --- scopes and module attribution ------------------------------------------

MULTILINE = textwrap.dedent(
    """
    class IqWakeup(val pregSz: Int) extends Bundle {
      val a = UInt()
    }

    abstract class IssueUnit(
      val numIssueSlots: Int,
      val iqType: Int
      )(implicit p: Parameters) extends BoomModule
    {
      val ready = io.a.valid && io.b.ready
    }
    """
)


def test_multiline_class_header_is_attributed_correctly():
    """A line-anchored regex attributes this to the Bundle above it."""
    offset = MULTILINE.index("io.a.valid")
    assert enclosing_module(MULTILINE, offset) == "IssueUnit"


def test_bundle_is_not_a_module():
    offset = MULTILINE.index("val a = UInt()")
    assert enclosing_module(MULTILINE, offset) is None


def test_resolver_walks_inheritance_transitively():
    src = textwrap.dedent(
        """
        abstract class FunctionalUnit(implicit p: Parameters) extends BoomModule { val z = 0 }
        abstract class PipelinedFunctionalUnit(n: Int)(implicit p: Parameters)
          extends FunctionalUnit { val y = 0 }
        class ALUUnit(dataWidth: Int)(implicit p: Parameters)
          extends PipelinedFunctionalUnit(1) { val q = a === b }
        """
    )
    r = ModuleResolver()
    r.add_source(src)
    assert r.is_module("ALUUnit")
    assert enclosing_module(src, src.index("a === b"), r) == "ALUUnit"


def test_resolver_stops_at_a_bundle_base():
    src = "class Req(implicit p: Parameters) extends BoomBundle { val a = UInt() }"
    r = ModuleResolver()
    r.add_source(src)
    assert not r.is_module("Req")


def test_resolver_survives_an_inheritance_cycle():
    """Malformed or generated source must not hang the corpus generator."""
    src = "class A extends B { }\nclass B extends A { }\n"
    r = ModuleResolver()
    r.add_source(src)
    assert r.is_module("A") is False


# --- engine ------------------------------------------------------------------

def test_mutated_applies_and_always_reverts(tmp_path: Path):
    src = "class M extends Module {\n  val a = x === y\n}\n"
    d = tmp_path / "repo"
    (d / "sub").mkdir(parents=True)
    f = d / "sub" / "M.scala"
    f.write_text(src)

    site = next(s for s in enumerate_sites("sub/M.scala", src) if s.operator == "eq_to_ne")
    with mutated(d, site, seed=1) as rec:
        assert f.read_text() != src
        assert "=/=" in f.read_text()
        assert rec.module == "M"
        assert rec.diff.startswith("--- a/sub/M.scala")
        assert rec.sha256_before != rec.sha256_after
    assert f.read_text() == src, "file was not restored"
    assert not list(d.rglob("*.waveql-orig"))
    assert not list(d.rglob("*.waveql-tmp"))


def test_mutated_reverts_even_when_the_body_raises(tmp_path: Path):
    """A build crash inside the context must not leave the checkout mutated."""
    src = "class M extends Module {\n  val a = x === y\n}\n"
    d = tmp_path / "repo"
    d.mkdir()
    f = d / "M.scala"
    f.write_text(src)
    site = next(s for s in enumerate_sites("M.scala", src) if s.operator == "eq_to_ne")

    with pytest.raises(RuntimeError, match="simulated build failure"):
        with mutated(d, site):
            assert f.read_text() != src
            raise RuntimeError("simulated build failure")
    assert f.read_text() == src


def test_sample_class_balanced_is_deterministic_and_balanced():
    src = textwrap.dedent(
        """
        class M extends Module {
          val a = p === q; val b = r === s; val c = t === u
          val d = e && f;  val g = h && i;  val j = k && l
          val n = RegNext(z)
        }
        """
    )
    sites = enumerate_sites("M.scala", src)
    first = sample_class_balanced(sites, per_class=2, seed=42)
    second = sample_class_balanced(sites, per_class=2, seed=42)
    assert [s.site_id for s in first] == [s.site_id for s in second]
    counts = {}
    for s in first:
        counts[s.mutation_class] = counts.get(s.mutation_class, 0) + 1
    assert all(v <= 2 for v in counts.values())


def test_sample_takes_everything_from_a_short_class():
    src = "class M extends Module { val n = RegNext(z) }"
    sites = enumerate_sites("M.scala", src)
    picked = sample_class_balanced(sites, per_class=10, seed=0, classes=["pipeline-depth"])
    assert len(picked) == 1


# --- non-hardware exclusion --------------------------------------------------

def test_mutations_inside_require_are_excluded():
    """`require` is a Scala elaboration-time contract, not a circuit.

    Regression for the first mutant a real campaign ever selected: a `>` -> `>=`
    inside `require(x.dispatchWidth > 0)` in parameters.scala. It survived all
    eight stimuli, correctly, after eight minutes of compute -- because it does
    not describe hardware.
    """
    src = "class M extends Module {\n  require(w <= n && w > 0)\n  val a = x > y\n}\n"
    ops = enumerate_sites("M.scala", src)
    assert [s.line for s in ops] == [3], "only the real hardware comparison should remain"


def test_mutations_inside_chisel_assert_are_excluded():
    """A mutated Chisel assertion fires spuriously and aborts the run, so it
    SCREENS AS KILLED while the defect is unfindable in the design -- a corpus
    task nobody can solve."""
    src = 'class M extends Module {\n  assert(!(io.enq.valid && !io.enq.ready))\n}\n'
    assert enumerate_sites("M.scala", src) == []


def test_mutations_inside_printf_are_excluded():
    """The DUT commit log this project joins on IS a printf; mutating one
    corrupts the measuring instrument rather than the design."""
    src = 'class M extends Module {\n  when (c) { printf("%d ", Mux(a, b, d)) }\n}\n'
    ops = {s.operator for s in enumerate_sites("M.scala", src)}
    assert "mux_arm_swap" not in ops


def test_non_hardware_sites_remain_enumerable_and_labelled():
    """They are reportable as a property of the operator set, not silently gone."""
    src = "class M extends Module {\n  require(w > 0)\n}\n"
    every = enumerate_sites("M.scala", src, include_non_hardware=True)
    assert every and all(s.in_construct == "require" for s in every)
    assert all(not s.is_hardware for s in every)
