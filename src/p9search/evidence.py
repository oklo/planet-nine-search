"""Bounded, lossless diagnostic evidence carried with a submission."""

import base64
import gzip
import hashlib
import io
import json
import zlib
from pathlib import Path

from .storage import canonical, sha256

MAX_PACKED = 1_000_000
MAX_EXPANDED = 16 * 2**20


def names(report):
    expected = dict(report["artifacts"])
    if any(Path(n).name != n or not n.endswith(".json") for n in expected):
        raise ValueError("Invalid evidence filename")
    for name, receipt in report["inputs"].items():
        parts = Path(name).parts
        if (
            len(parts) != 3
            or parts[0] != "data"
            or parts[2] != "download.json"
            or len(parts[1]) != 5
            or parts[1][0] != "s"
            or not parts[1][1:].isdigit()
        ):
            raise ValueError("Invalid input evidence filename")
        expected[name] = None
        expected[str(Path(name).with_name("archive-rows.json"))] = receipt["sha256"][
            "archive-rows.json"
        ]
    return expected


def validate(report, documents):
    expected = names(report)
    if not isinstance(documents, dict) or documents.keys() != expected.keys():
        raise ValueError("Incomplete submission evidence")
    for name, digest in expected.items():
        text = documents[name]
        if not isinstance(text, str):
            raise ValueError("Evidence must contain exact UTF-8 text")
        if digest is not None and hashlib.sha256(text.encode()).hexdigest() != digest:
            raise ValueError("Submission evidence checksum differs")
        if name in report["inputs"] and json.loads(text) != report["inputs"][name]:
            raise ValueError("Input evidence differs")
    return documents


def pack(directory, report):
    directory = Path(directory).absolute()
    documents = {}
    total = 0
    for name in names(report):
        path = directory / name
        if path.resolve() != path or path.stat().st_size > MAX_EXPANDED - total:
            raise ValueError("Unsafe or oversized evidence file")
        content = path.read_bytes()
        total += len(content)
        documents[name] = content.decode("utf-8")
    validate(report, documents)
    raw = canonical(documents).encode()
    if len(raw) > MAX_EXPANDED:
        raise ValueError("Expanded evidence exceeds limit")
    payload = gzip.compress(raw, compresslevel=6, mtime=0)
    if len(payload) > MAX_PACKED:
        raise ValueError("Compressed evidence exceeds limit")
    return {
        "format": "json-gzip-v1",
        "sha256": hashlib.sha256(payload).hexdigest(),
        "data": base64.b64encode(payload).decode("ascii"),
    }


def unpack(report, envelope):
    if (
        not isinstance(envelope, dict)
        or set(envelope) != {"format", "sha256", "data"}
        or envelope["format"] != "json-gzip-v1"
        or not isinstance(envelope["data"], str)
        or len(envelope["data"]) > 4 * ((MAX_PACKED + 2) // 3)
    ):
        raise ValueError("Invalid evidence envelope")
    try:
        payload = base64.b64decode(envelope["data"], validate=True)
        if len(payload) > MAX_PACKED or hashlib.sha256(payload).hexdigest() != envelope["sha256"]:
            raise ValueError("Evidence envelope checksum differs")
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as stream:
            raw = stream.read(MAX_EXPANDED + 1)
        if len(raw) > MAX_EXPANDED:
            raise ValueError("Expanded evidence exceeds limit")
        return validate(report, json.loads(raw))
    except (OSError, EOFError, UnicodeError, RecursionError, zlib.error) as error:
        raise ValueError("Invalid compressed evidence") from error


def acknowledged(directory):
    """The successful POST covered the complete evidence-bearing result."""
    directory = Path(directory)
    receipt = json.loads((directory / "submission.json").read_text())
    if receipt.get("accepted") is not True or receipt.get("verified") is not False:
        raise ValueError("No submission acknowledgement")
    if "evidence_sha256" not in receipt:
        return False
    report = json.loads((directory / "result.json").read_text())
    if (
        receipt.get("result_sha256") != sha256(directory / "result.json")
        or receipt["evidence_sha256"] != pack(directory, report)["sha256"]
    ):
        raise ValueError("Acknowledged evidence changed")
    return True
