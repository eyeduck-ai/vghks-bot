"""Discover the live local UI without opening its databases a second time."""
from __future__ import annotations

import hmac
import http.client
import json
import re
import secrets
from contextlib import contextmanager
from pathlib import Path

from .storage import atomic_json

INSTANCE_FILE = ".instance.json"
REOPEN_PATH = "/api/launcher"
REOPEN_HEADER = "X-VGHKS-Launcher"
CHALLENGE_HEADER = "X-VGHKS-Challenge"


def valid_token(value):
    return isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{43}", value) is not None


def signature(secret, message):
    return hmac.new(secret.encode("ascii"), message.encode("ascii"), "sha256").hexdigest()


@contextmanager
def publish_instance(home, server):
    # The caller holds DatabaseManager's catalog lock throughout this lifetime.
    home = Path(home).resolve()
    path = home / INSTANCE_FILE
    atomic_json(path, {"home": str(home), "port": server.server_port,
                       "secret": server.launcher_token})
    try:
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            # A stale record cannot reopen an unrelated service and is replaced
            # by the next process that acquires the catalog lock.
            pass


def running_url(home):
    """Return a verified loopback launch URL, or None while the owner is unavailable."""
    home = Path(home).resolve()
    connection = None
    try:
        with (home / INSTANCE_FILE).open(encoding="utf-8") as stream:
            record = json.loads(stream.read(8193))
        if not isinstance(record, dict):
            return None
        port, secret = record.get("port"), record.get("secret")
        if (type(port) is not int or not 1 <= port <= 65535 or not valid_token(secret)
                or not isinstance(record.get("home"), str) or Path(record["home"]) != home):
            return None
        # Do not use proxies, redirects, or a hostname/URL supplied by the file.
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=1)
        challenge = secrets.token_urlsafe(32)
        connection.request("GET", REOPEN_PATH, headers={CHALLENGE_HEADER: challenge,
                           REOPEN_HEADER: signature(secret, challenge)})
        response = connection.getresponse()
        if response.status != 200:
            return None
        payload = json.loads(response.read(8193))
        if (not isinstance(payload, dict) or payload.get("application") != "VGHKS-bot"
                or not valid_token(payload.get("launch_token"))):
            return None
        proof = payload.get("proof")
        expected = signature(secret, challenge + ":" + payload["launch_token"])
        if not isinstance(proof, str) or not hmac.compare_digest(proof, expected):
            return None
        return f"http://127.0.0.1:{port}/#{payload['launch_token']}"
    except (OSError, ValueError, TypeError, http.client.HTTPException):
        return None
    finally:
        if connection:
            connection.close()
