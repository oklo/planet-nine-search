"""HTTPS bundle downloads restricted to operator-approved public origins."""

import http.client
import ipaddress
import socket
import ssl
from urllib.parse import urljoin, urlsplit

from . import USER_AGENT

DEFAULT_ORIGINS = ("https://oklo.org",)


def public_url(url, origins):
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.port not in (None, 443)
        or parsed.fragment
        or "\\" in url
        or any(ord(c) < 33 for c in url)
    ):
        raise ValueError("Bundle URL must use public HTTPS on port 443")
    origin = f"https://{parsed.hostname}"
    if origin not in origins:
        raise ValueError(
            "Bundle origin is not approved; use --bundle-origin for a trusted publisher"
        )
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise ValueError("Private bundle destination is forbidden")
    return parsed


class PublicConnection(http.client.HTTPSConnection):
    def connect(self):
        addresses = socket.getaddrinfo(self.host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise ValueError("Bundle hostname resolves to a nonpublic address")
        # Connect to the checked address, retaining the original TLS hostname.
        sock = socket.create_connection((addresses[0][4][0], 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


def download(url, destination, expected_bytes, origins=DEFAULT_ORIGINS):
    for _ in range(4):
        parsed = public_url(url, origins)
        connection = PublicConnection(
            parsed.hostname, timeout=120, context=ssl.create_default_context()
        )
        try:
            path = parsed.path or "/"
            if parsed.query:
                path += "?" + parsed.query
            connection.request(
                "GET", path, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
            )
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location")
                if not location:
                    raise ValueError("Redirect lacks a destination")
                url = urljoin(url, location)
                public_url(url, origins)
                continue
            if (
                response.status != 200
                or response.getheader("Content-Encoding", "identity") != "identity"
            ):
                raise ValueError(f"Unexpected bundle response: {response.status}")
            length = response.getheader("Content-Length")
            if length is not None and int(length) != expected_bytes:
                raise ValueError("Bundle size differs")
            count = 0
            with destination.open("wb") as output:
                while block := response.read(min(1024 * 1024, expected_bytes + 1 - count)):
                    count += len(block)
                    if count > expected_bytes:
                        raise ValueError("Bundle exceeds declared size")
                    output.write(block)
            if count != expected_bytes:
                raise ValueError("Bundle size differs")
            return
        finally:
            connection.close()
    raise ValueError("Too many bundle redirects")
