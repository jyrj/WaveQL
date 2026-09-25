

def test_mutation_reverts_when_the_process_is_terminated(tmp_path):
    """A `finally` does not run on SIGTERM, and stopping a campaign is ordinary.

    Without a handler the checkout is left MUTATED with the backup beside it, and
    the next run either cannot re-derive its sites or, worse, builds a design
    carrying two defects.
    """
    import signal
    import subprocess
    import sys

    src = "object A { val x = a > b }\n"
    (tmp_path / ".git").mkdir()
    (tmp_path / "A.scala").write_text(src)
    child = (
        "import sys, time; sys.path.insert(0, 'src')\n"
        "from pathlib import Path\n"
        "from waveql.mutator.engine import mutated\n"
        "from waveql.mutator.operators import enumerate_sites\n"
        "root = Path(sys.argv[1])\n"
        "site = next(iter(enumerate_sites('A.scala', (root / 'A.scala').read_text())))\n"
        "ctx = mutated(root, site)\n"
        "ctx.__enter__()\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", child, str(tmp_path)],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "READY"
        assert (tmp_path / "A.scala").read_text() != src, "mutation was not applied"
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert (tmp_path / "A.scala").read_text() == src
    assert not list(tmp_path.glob("*.waveql-orig"))
    assert not list(tmp_path.glob("*.waveql-tmp"))


def test_stopping_the_harness_stops_its_builds_too(tmp_path):
    """A stopped run once left `make` and seven `cc1plus` compiling a processor
    from source that had already been reverted: the runner died, the build it
    had launched was never signalled. Children run in their own process group
    now, and the signal path takes that group down before reverting."""
    import os
    import signal
    import subprocess
    import sys
    import time

    src = "object A { val x = a > b }\n"
    (tmp_path / ".git").mkdir()
    (tmp_path / "A.scala").write_text(src)
    marker = f"waveql-orphan-probe-{os.getpid()}"
    child = (
        "import sys; sys.path.insert(0, 'src')\n"
        "from pathlib import Path\n"
        "from waveql.harness import children\n"
        "from waveql.mutator.engine import mutated\n"
        "from waveql.mutator.operators import enumerate_sites\n"
        "root = Path(sys.argv[1])\n"
        "site = next(iter(enumerate_sites('A.scala', (root / 'A.scala').read_text())))\n"
        "with mutated(root, site):\n"
        "    print('READY', flush=True)\n"
        # a tree, the way make forks compilers: a shell with two long children
        # (bash, not sh: `exec -a` is a bash builtin and sh is dash on Ubuntu)
        f"    children.run(['bash', '-c', 'exec -a {marker} sleep 300 & exec -a {marker} sleep 300; wait'])\n"
    )
    p = subprocess.Popen([sys.executable, "-c", child, str(tmp_path)],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "READY"
        deadline = time.time() + 10
        while time.time() < deadline:
            if subprocess.run(["pgrep", "-f", marker], capture_output=True).stdout:
                break
            time.sleep(0.1)
        assert subprocess.run(["pgrep", "-f", marker], capture_output=True).stdout, \
            "the child tree never started"
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
        time.sleep(0.5)
    finally:
        if p.poll() is None:
            p.kill()
        subprocess.run(["pkill", "-KILL", "-f", marker], capture_output=True)
    left = subprocess.run(["pgrep", "-f", marker], capture_output=True).stdout
    assert not left, f"orphaned children survived: {left!r}"
    assert (tmp_path / "A.scala").read_text() == src


def test_a_patch_under_verification_reverts_on_sigterm_too(tmp_path, _register=True):
    """The agent's patch, not just the mutation, must survive a stop.

    verify_applied restores a patched file in a `finally`, which SIGTERM skips.
    When the patch touches a file OTHER than the mutation's, stopping a verifier
    mid-build must not leave it in the worker for every later build to compile.
    """
    import signal
    import subprocess
    import sys

    src_a = "object A { val x = a > b }\n"
    src_b = "object B { val y = 1 }\n"
    (tmp_path / ".git").mkdir()
    (tmp_path / "A.scala").write_text(src_a)
    (tmp_path / "B.scala").write_text(src_b)
    register = "h = held_edit(b, before); h.__enter__()\n" if _register else ""
    child = (
        "import sys, time; sys.path.insert(0, 'src')\n"
        "from pathlib import Path\n"
        "from waveql.mutator.engine import held_edit, mutated\n"
        "from waveql.mutator.operators import enumerate_sites\n"
        "root = Path(sys.argv[1])\n"
        "site = next(iter(enumerate_sites('A.scala', (root / 'A.scala').read_text())))\n"
        "ctx = mutated(root, site); ctx.__enter__()\n"
        "b = root / 'B.scala'; before = b.read_text()\n"
        + register +
        "b.write_text('object B { val y = br_deallocseallocs }\\n')\n"
        "print('READY', flush=True)\n"
        "time.sleep(60)\n"
    )
    p = subprocess.Popen([sys.executable, "-c", child, str(tmp_path)],
                         stdout=subprocess.PIPE, text=True)
    try:
        assert p.stdout.readline().strip() == "READY"
        assert (tmp_path / "A.scala").read_text() != src_a, "mutation was not applied"
        assert "br_deallocs" in (tmp_path / "B.scala").read_text(), "patch was not applied"
        p.send_signal(signal.SIGTERM)
        p.wait(timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    assert (tmp_path / "A.scala").read_text() == src_a
    assert (tmp_path / "B.scala").read_text() == src_b, "the patch outlived the stop"


def test_the_class_balanced_sample_does_not_depend_on_input_order():
    """A seeded draw must name the same mutants on every machine.

    Sampling in filesystem iteration order made a draw recomputed elsewhere a
    different set of mutants, so a later draw's exclusion list could miss it.
    """
    import random
    from pathlib import Path
    from waveql.corpus.targets import ARCH_TARGETS, filter_sites
    from waveql.mutator.engine import collect_sites, sample_class_balanced

    root = Path(__file__).resolve().parents[1] / "thirdparty" / "chipyard"
    if not (root / "generators" / "boom").is_dir():
        import pytest
        pytest.skip("needs the Chipyard checkout in thirdparty/chipyard")
    sites = filter_sites(collect_sites(root, [t for t, _ in ARCH_TARGETS]), record=[])
    a = [s.site_id for s in sample_class_balanced(sites, 5, 20260924)]
    shuffled = list(sites)
    random.Random(1).shuffle(shuffled)
    b = [s.site_id for s in sample_class_balanced(shuffled, 5, 20260924)]
    assert a == b
