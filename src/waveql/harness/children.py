"""Child process groups that must die with the harness.

A build or a simulation is launched in its own process group and registered
here, so a SIGTERM to the harness can take the whole tree down. Without it the
Python runner exits -- reverting its mutation on the way out -- while the `make`
it launched is never signalled and keeps compiling, as an orphan, a processor
from source that has already been reverted.
"""
from __future__ import annotations

import contextlib
import os
import signal
import subprocess

_GROUPS: set[int] = set()


def register(pgid: int) -> None:
    _GROUPS.add(pgid)


def unregister(pgid: int) -> None:
    _GROUPS.discard(pgid)


def kill_all(sig: int = signal.SIGTERM) -> None:
    """Signal every registered group; called from the harness's own signal path."""
    for g in list(_GROUPS):
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(g, sig)
    _GROUPS.clear()


def run(cmd, *, timeout=None, **kw) -> subprocess.CompletedProcess:
    """subprocess.run, but in a registered process group of its own.

    On timeout the whole group is killed -- not just the direct child, which for
    `make` would leave every compiler it forked still running -- and
    TimeoutExpired is re-raised so callers see the same contract as before.
    """
    proc = subprocess.Popen(cmd, start_new_session=True, **kw)
    register(proc.pid)
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()
        raise
    finally:
        unregister(proc.pid)
    return subprocess.CompletedProcess(proc.args, proc.returncode)
