"""Regression tests for one bug class: undecodable files must not crash Hermes.

``UnicodeDecodeError`` subclasses ``ValueError``, not ``OSError``. Every site
in this file previously wrapped a ``read_text(encoding="utf-8")`` in an
``except`` that listed ``OSError`` / ``JSONDecodeError`` / ``SyntaxError`` and
therefore did **not** catch it — so a single undecodable byte escaped as a raw
codec error.

That is not hypothetical. On 2026-08-24 macOS AppleDouble ``._*`` sidecars
landed in ``/opt/hermes/tools/``; ``tools/registry.py`` raised
``UnicodeDecodeError`` during tool discovery, which runs at agent startup, and
**every scheduled cron job died** — morning briefs, the job digest and mail
triage all stopped while the gateway still looked healthy from outside.

An audit then found the same shape in three more places. These tests pin each
one so the class cannot come back silently.

Two different correct answers appear below, and the difference is deliberate:

* Content that is **displayed or used as context** (subprocess logs, a prior
  job's output) is read with ``errors="replace"`` — garbled text is better
  than no feature.
* Content that is **parsed as data** (the cron job database, an RPC snapshot)
  must not be silently mangled: it fails, but with a legible error instead of
  a bare codec traceback.
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

# A byte that is never valid at the start of a UTF-8 sequence. 0xA3 is the
# byte that actually took the scheduler down, from an AppleDouble header.
UNDECODABLE = b"\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        \x00\x02\x00\x00\x00\xa3"


class TestToolDiscovery:
    """tools/registry.py — the site that caused the original outage."""

    def test_undecodable_module_is_skipped_not_raised(self, tmp_path):
        from tools.registry import _module_registers_tools

        bad = tmp_path / "._ansi_strip.py"
        bad.write_bytes(UNDECODABLE)

        # A file we cannot read is simply not a module that registers tools.
        assert _module_registers_tools(bad) is False

    def test_discovery_survives_undecodable_sibling(self, tmp_path, monkeypatch):
        """One bad file must not stop the good ones from being discovered."""
        import tools.registry as registry

        (tmp_path / "._poison.py").write_bytes(UNDECODABLE)
        (tmp_path / "good_tool.py").write_text(
            "import registry\n"
            "registry.register(name='ok')\n",
            encoding="utf-8",
        )

        found = [
            p for p in sorted(tmp_path.glob("*.py"))
            if registry._module_registers_tools(p)
        ]
        assert [p.name for p in found] == ["good_tool.py"]


class TestCronJobDatabase:
    """cron/jobs.py — read on every scheduler tick, so blast radius is total."""

    def test_undecodable_jobs_file_raises_legible_error(self, tmp_path, monkeypatch):
        import cron.jobs as jobs_mod

        jobs_file = tmp_path / "jobs.json"
        jobs_file.write_bytes(UNDECODABLE)
        monkeypatch.setattr(jobs_mod, "JOBS_FILE", jobs_file)
        monkeypatch.setattr(jobs_mod, "ensure_dirs", lambda: None)

        # Not a bare UnicodeDecodeError, and emphatically not a silent [] —
        # returning no jobs would disable every schedule without saying so.
        with pytest.raises(RuntimeError, match="not valid UTF-8"):
            jobs_mod.load_jobs()


class TestSpawnTreeLoad:
    """tui_gateway/server.py — an RPC handler must answer, not crash."""

    def test_undecodable_snapshot_returns_error_response(self, tmp_path, monkeypatch):
        import tui_gateway.server as server

        root = tmp_path / "spawn-trees"
        root.mkdir()
        snapshot = root / "snap.json"
        snapshot.write_bytes(UNDECODABLE)
        monkeypatch.setattr(server, "_spawn_trees_root", lambda: root)

        handler = server._methods["spawn_tree.load"]

        resp = handler(1, {"path": str(snapshot)})
        assert "error" in resp
        assert "spawn_tree.load failed" in json.dumps(resp)


class TestReadForDisplay:
    """Sites that show text rather than parse it read with errors='replace'.

    These drive the real call site, not ``read_text`` in the abstract: a test
    that only asserts stdlib behaviour would pass against the unfixed code and
    prove nothing.
    """

    def test_context_from_survives_undecodable_prior_output(
        self, tmp_path, monkeypatch,
    ):
        """cron/scheduler.py: garbled context beats a dead job."""
        import cron.jobs as jobs_mod
        import cron.scheduler as sched

        source_id = "a1b2c3d4e5f6"
        output_dir = tmp_path / "output" / source_id
        output_dir.mkdir(parents=True)
        (output_dir / "2026-08-24_09-00-00.md").write_bytes(
            b"# Digest\n20 jobs found\n" + UNDECODABLE,
        )
        monkeypatch.setattr(jobs_mod, "OUTPUT_DIR", tmp_path / "output")

        prompt = sched._build_job_prompt(
            {"id": "ffffffffffff", "name": "consumer",
             "prompt": "Analyse it.", "context_from": source_id},
        )

        # The readable part of the prior job's output reaches the prompt...
        assert "20 jobs found" in prompt
        # ...and the caller's own prompt is still intact.
        assert "Analyse it." in prompt

    def test_update_log_read_uses_replacement(self):
        """gateway/run.py: apt/pip/docker stdout is arbitrary bytes.

        The streaming update watcher is an async loop over a live subprocess
        with no seam to call directly, so this pins the decision at the call
        site instead of leaving it untested.
        """
        import inspect

        import gateway.run as run_mod

        src = inspect.getsource(run_mod)
        reads = [
            line.strip() for line in src.splitlines()
            if "output_path.read_text(" in line
        ]
        assert reads, "update-watcher read sites disappeared — retarget this test"
        assert all('errors="replace"' in line for line in reads), reads
