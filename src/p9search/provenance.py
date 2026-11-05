"""Execution identity recorded before and after scientific work."""

import hashlib
import importlib.metadata
import platform
import sys
from pathlib import Path

from . import __version__


def source_hash():
    root = Path(__file__).parent
    return hashlib.sha256(
        b"".join(
            p.relative_to(root).as_posix().encode() + p.read_bytes()
            for p in sorted(root.rglob("*.py"))
        )
    ).hexdigest()


def execution_manifest():
    return {
        "source_sha256": source_hash(),
        "package_version": __version__,
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": {d.metadata["Name"]: d.version for d in importlib.metadata.distributions()},
    }
