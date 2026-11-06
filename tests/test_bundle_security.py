import io
import socket
import ssl
import zipfile

import numpy as np
import pytest

from p9search import network
from p9search.bundles import ARRAYS, array_headers, validate_bundle


def npz(arrays):
    out = io.BytesIO()
    np.savez_compressed(out, **arrays)
    return out.getvalue()


def bundle_bytes():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w") as z:
        z.writestr("motion_masks.npz", npz({"slice_00": np.ones(3, bool)}))
        for sid in (1, 28, 68):
            z.writestr(
                f"observer_s{sid}.npz", npz({"time": np.arange(3.0), "position": np.zeros((3, 3))})
            )
            for name, rules in ARRAYS.items():
                arrays = {}
                for key, rule in rules.items():
                    shape = () if len(rule) == 1 else (3, *rule[2:])
                    arrays[key] = np.zeros(shape, dtype=bool if rule[0] == "b" else float)
                z.writestr(f"s{sid:04d}/{name}", npz(arrays))
            z.writestr(f"s{sid:04d}/metadata.json", "{}")
            z.writestr(f"s{sid:04d}/wcs.txt", "")
    output.seek(0)
    return output


def test_legitimate_nested_geometry_passes():
    with zipfile.ZipFile(bundle_bytes()) as z:
        assert len(validate_bundle(z, [1, 28, 68])) == 22


@pytest.mark.parametrize(
    "arrays",
    [
        {"xy": np.zeros((2, 2)), "flux": np.ones(2), "tmag": np.ones(2), "unused": np.ones(2)},
        {"xy": np.zeros((2, 2)), "flux": np.ones(2, dtype=object), "tmag": np.ones(2)},
        {"xy": np.zeros((2, 3)), "flux": np.ones(2), "tmag": np.ones(2)},
    ],
)
def test_unused_keys_objects_and_bad_dimensions_are_rejected(arrays):
    with pytest.raises(ValueError):
        array_headers(npz(arrays), "stars.npz")


def test_huge_declared_shape_is_rejected_before_loading(monkeypatch):
    payload = io.BytesIO()
    np.lib.format.write_array_header_1_0(
        payload, {"shape": (2**40,), "fortran_order": False, "descr": "|b1"}
    )
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr("slice_00.npy", payload.getvalue())
    monkeypatch.setattr(
        np, "load", lambda *a, **k: pytest.fail("Array allocated before validation")
    )
    with pytest.raises(ValueError, match="shape"):
        array_headers(out.getvalue(), "motion_masks.npz")


def test_nested_expansion_limit_uses_uncompressed_headers(monkeypatch):
    # A small fixture exercises the same branch as a 128-MiB nested expansion.
    from p9search import bundles

    monkeypatch.setattr(bundles, "MAX_EXPANDED", 1024)
    with pytest.raises(ValueError, match="expanded"):
        array_headers(npz({"slice_00": np.zeros(2048, bool)}), "motion_masks.npz")


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1/x",
        "https://169.254.169.254/x",
        "http://oklo.org/x",
        "https://other.org/x",
        "https://u:p@oklo.org/x",
    ],
)
def test_bundle_destination_policy(url):
    with pytest.raises(ValueError):
        network.public_url(url, ["https://oklo.org"])


def test_dns_rebinding_cannot_escape_validated_address(monkeypatch):
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 443))])
    monkeypatch.setattr(
        socket, "create_connection", lambda *a, **k: pytest.fail("Private destination contacted")
    )
    with pytest.raises(ValueError, match="nonpublic"):
        network.PublicConnection("oklo.org").connect()


def test_redirect_is_checked_before_contact(tmp_path, monkeypatch):
    contacted = []

    class Redirect:
        status = 302

        def getheader(self, key):
            return "https://127.0.0.1/private"

    class Connection:
        def __init__(self, host, **kwargs):
            contacted.append(host)

        def request(self, *a, **k):
            pass

        def getresponse(self):
            return Redirect()

        def close(self):
            pass

    monkeypatch.setattr(network, "PublicConnection", Connection)
    with pytest.raises(ValueError):
        network.download("https://oklo.org/file", tmp_path / "file", 10)
    assert contacted == ["oklo.org"]


def test_download_retries_read_timeout_then_succeeds(tmp_path, monkeypatch):
    attempts = []
    sleeps = []

    class Response:
        status = 200

        def __init__(self, blocks):
            self.blocks = iter(blocks)

        def getheader(self, key, default=None):
            return {"Content-Length": "4"}.get(key, default)

        def read(self, _size):
            block = next(self.blocks)
            if isinstance(block, BaseException):
                raise block
            return block

    class Connection:
        def __init__(self, *_args, **_kwargs):
            self.number = len(attempts)
            attempts.append(self.number)

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            blocks = [b"ab", TimeoutError("read timed out")]
            if self.number != 0:
                blocks = [b"abcd", b""]
            return Response(blocks)

        def close(self):
            pass

    monkeypatch.setattr(network, "PublicConnection", Connection)
    monkeypatch.setattr(network.time, "sleep", sleeps.append)
    destination = tmp_path / "bundle.zip"
    network.download("https://oklo.org/file", destination, 4)
    assert destination.read_bytes() == b"abcd"
    assert len(attempts) == 2
    assert sleeps == [30]


def test_download_timeout_exhaustion_removes_partial_file(tmp_path, monkeypatch):
    attempts = []
    sleeps = []

    class Response:
        status = 200

        def getheader(self, key, default=None):
            return {"Content-Length": "4"}.get(key, default)

        def read(self, _size):
            if not hasattr(self, "sent"):
                self.sent = True
                return b"ab"
            raise TimeoutError("read timed out")

    class Connection:
        def __init__(self, *_args, **_kwargs):
            attempts.append(None)

        def request(self, *_args, **_kwargs):
            pass

        def getresponse(self):
            return Response()

        def close(self):
            pass

    monkeypatch.setattr(network, "PublicConnection", Connection)
    monkeypatch.setattr(network.time, "sleep", sleeps.append)
    destination = tmp_path / "bundle.zip"
    with pytest.raises(TimeoutError, match="read timed out"):
        network.download("https://oklo.org/file", destination, 4)
    assert len(attempts) == 3
    assert sleeps == [30, 30]
    assert not destination.exists()


@pytest.mark.parametrize(
    "failure",
    [ssl.SSLCertVerificationError("bad certificate"), ValueError("invalid bundle URL")],
)
def test_download_does_not_retry_tls_or_validation_errors(tmp_path, monkeypatch, failure):
    attempts = []

    class Connection:
        def __init__(self, *_args, **_kwargs):
            attempts.append(None)

        def request(self, *_args, **_kwargs):
            if isinstance(failure, ssl.SSLCertVerificationError):
                raise failure

        def close(self):
            pass

    monkeypatch.setattr(network, "PublicConnection", Connection)
    if not isinstance(failure, ssl.SSLCertVerificationError):

        def reject_url(*_args):
            raise failure

        monkeypatch.setattr(network, "public_url", reject_url)
    with pytest.raises(type(failure)):
        network.download("https://oklo.org/file", tmp_path / "bundle.zip", 4)
    assert len(attempts) == (1 if isinstance(failure, ssl.SSLCertVerificationError) else 0)
