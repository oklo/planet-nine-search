"""Execution identity recorded before and after scientific work."""

import hashlib
import importlib.metadata
import platform
import re
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


def installed_dependencies():
    dependencies, seen = {}, {}
    for distribution in importlib.metadata.distributions():
        name, version = distribution.metadata["Name"], distribution.version
        key = re.sub(r"[-_.]+", "-", name).lower()
        if key in seen and seen[key] != version:
            raise ValueError(
                f"Conflicting installed versions of {name}: {seen[key]} and {version}. "
                "Rebuild the worker environment before starting a search."
            )
        seen[key] = version
        dependencies[name] = version
    return dependencies


def execution_manifest():
    return {
        "source_sha256": source_hash(),
        "package_version": __version__,
        "python": sys.version,
        "platform": platform.platform(),
        "dependencies": installed_dependencies(),
    }
