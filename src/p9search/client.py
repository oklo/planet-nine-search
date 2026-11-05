"""Small HTTPS client; credentials never enter task or result files."""

import secrets
from pathlib import Path

import requests

from . import USER_AGENT
from .storage import MAX_REPORT, bounded_json, read_json, write_json
from .tasks import validate_url

DEFAULT_COORDINATOR = "https://oklo.org/planet-nine/api.php"


class CoordinatorError(RuntimeError):
    def __init__(self, status, message):
        super().__init__(f"Coordinator {status}: {message}")
        self.status = status


class Client:
    def __init__(self, endpoint, credentials=None):
        self.endpoint = validate_url(endpoint)
        self.credentials_path = Path(credentials) if credentials else None
        self.credentials = (
            read_json(credentials) if credentials and Path(credentials).exists() else {}
        )
        if self.credentials and self.credentials.get("endpoint") != endpoint:
            raise ValueError("Credentials belong to a different coordinator")
        self.session = requests.Session()
        self.session.headers["User-Agent"] = USER_AGENT

    def save(self):
        write_json(self.credentials_path, self.credentials, private=True)

    def request(self, action, body=None):
        headers = {}
        if self.credentials.get("token"):
            headers["Authorization"] = "Bearer " + self.credentials["token"]
        if action == "status":
            response = self.session.get(
                self.endpoint,
                params={"action": action},
                timeout=(10, 30),
                allow_redirects=False,
                stream=True,
            )
        else:
            response = self.session.post(
                self.endpoint,
                params={"action": action},
                json=body,
                headers=headers,
                timeout=(10, 30),
                allow_redirects=False,
                stream=True,
            )
        try:
            content = bytearray()
            for block in response.iter_content(65536):
                content.extend(block)
                if len(content) > MAX_REPORT:
                    raise ValueError("Coordinator response exceeds limit")
            payload = bounded_json(content)
            if not isinstance(payload, dict):
                raise ValueError("Coordinator response must be an object")
        except ValueError as error:
            raise CoordinatorError(response.status_code, "Non-JSON response") from error
        finally:
            response.close()
        if not response.ok or "error" in payload:
            raise CoordinatorError(response.status_code, payload.get("error", "request failed"))
        return payload

    def register(self, name):
        if "token" not in self.credentials:
            self.credentials = {"endpoint": self.endpoint, "token": secrets.token_hex(32)}
            self.save()
        if "worker_id" not in self.credentials:
            reply = self.request("register", {"name": name, "token": self.credentials["token"]})
            self.credentials["worker_id"] = reply["worker_id"]
            self.save()

    def lease_body(self, **extra):
        return {
            "task_id": self.credentials["task_id"],
            "lease_token": self.credentials["lease_token"],
            **extra,
        }

    def clear_claim(self):
        for key in ("task_id", "lease_token"):
            self.credentials.pop(key, None)
        self.save()
