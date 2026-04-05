#!/usr/bin/env python3
from __future__ import annotations

import subprocess
import sys
import os
from pathlib import Path

from setuptools import Command, setup


class RunTests(Command):
    """Compatibility test command for `python setup.py test`."""

    description = "run project tests with unittest discovery"
    user_options: list[tuple[str, str | None, str]] = []

    def initialize_options(self) -> None:  # pragma: no cover - setuptools API
        return None

    def finalize_options(self) -> None:  # pragma: no cover - setuptools API
        return None

    def run(self) -> None:
        root = Path(__file__).resolve().parent
        src_dir = root / "src"
        env = os.environ.copy()
        pythonpath_entries = [str(src_dir)]
        existing_pythonpath = env.get("PYTHONPATH", "")
        for raw_entry in existing_pythonpath.split(os.pathsep):
            if raw_entry:
                pythonpath_entries.append(raw_entry)
        env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        cmd = [
            sys.executable,
            "-m",
            "unittest",
            "discover",
            "-s",
            "tests",
            "-p",
            "test*.py",
        ]
        raise SystemExit(subprocess.call(cmd, cwd=root, env=env))


setup(cmdclass={"test": RunTests})
