"""Durable encryption for explicitly requested owner publication-password copies."""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import json
import os
import secrets
import stat
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from minder_publish.errors import PublishError

KEY_FILENAME = "publication-password.key"
KEY_BYTES = 32
MAX_PASSWORD_BYTES = 1024
_PURPOSE = "minder-publish:publication-password:v1"


class PasswordVault:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / KEY_FILENAME
        self._key_fingerprint = None

    @staticmethod
    def sealed_publications(connection):
        """Include staged policies so losing a key cannot silently replace it."""
        for table in ("publication", "upload"):
            for row in connection.execute(f"SELECT data FROM {table}"):
                try:
                    data = json.loads(row["data"])
                    if not isinstance(data, dict):
                        raise ValueError("Invalid publication policy")
                    publication = (
                        data if table == "publication" else data["publication"]
                    )
                    if not isinstance(publication, dict):
                        raise ValueError("Invalid staged publication policy")
                    if publication.get("password_sealed") is not None:
                        yield publication
                except (ValueError, TypeError, KeyError) as exc:
                    raise PublishError("publication_password_unavailable", 503) from exc

    def initialize(self, connection):
        try:
            key = self.key()
            for publication in self.sealed_publications(connection):
                self._open(publication, key)
            self._key_fingerprint = hashlib.sha256(key).digest()
            return
        except PublishError:
            if self.path.exists() or self.path.is_symlink():
                raise
            if next(self.sealed_publications(connection), None) is not None:
                raise
        try:
            descriptor = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self.initialize(connection)
            return
        with os.fdopen(descriptor, "wb") as target:
            target.write(secrets.token_bytes(KEY_BYTES))
            target.flush()
            os.fsync(target.fileno())
        descriptor = os.open(self.path.parent, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._key_fingerprint = hashlib.sha256(self.key()).digest()

    def key(self):
        try:
            descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            with os.fdopen(descriptor, "rb") as source:
                metadata = os.fstat(source.fileno())
                if (
                    not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_size != KEY_BYTES
                    or stat.S_IMODE(metadata.st_mode) & 0o077
                ):
                    raise ValueError("Invalid key file")
                key = source.read(KEY_BYTES + 1)
                if len(key) != KEY_BYTES:
                    raise ValueError("Invalid key length")
                if self._key_fingerprint is not None and not hmac.compare_digest(
                    self._key_fingerprint, hashlib.sha256(key).digest()
                ):
                    raise ValueError("Publication-password key changed")
                return key
        except (OSError, ValueError) as exc:
            raise PublishError(
                "publication_password_key_unavailable",
                503,
                "Restore the publication-password key from a matching backup.",
            ) from exc

    @staticmethod
    def associated_data(publication):
        return json.dumps(
            [_PURPOSE, publication["id"], publication["password_hash"]],
            ensure_ascii=True,
            separators=(",", ":"),
        ).encode()

    def seal(self, publication, password):
        nonce = secrets.token_bytes(12)
        ciphertext = AESGCM(self.key()).encrypt(
            nonce, password.encode("utf-8"), self.associated_data(publication)
        )
        return {
            "version": 1,
            "sealed": base64.urlsafe_b64encode(nonce + ciphertext).decode("ascii"),
        }

    def open(self, publication):
        return self._open(publication, self.key())

    def _open(self, publication, key):
        envelope = publication.get("password_sealed")
        if envelope is None:
            raise PublishError(
                "password_copy_unavailable",
                409,
                "Set a new publication password to enable copying it.",
                action="reset_publication_password",
            )
        try:
            if (
                not isinstance(envelope, dict)
                or set(envelope) != {"version", "sealed"}
                or type(envelope["version"]) is not int
                or envelope["version"] != 1
                or not isinstance(envelope["sealed"], str)
                or not 40 <= len(envelope["sealed"]) <= 1404
            ):
                raise ValueError("Invalid password envelope")
            encrypted = base64.b64decode(
                envelope["sealed"], altchars=b"-_", validate=True
            )
            if not 29 <= len(encrypted) <= MAX_PASSWORD_BYTES + 28:
                raise ValueError("Invalid password envelope length")
            password = AESGCM(key).decrypt(
                encrypted[:12], encrypted[12:], self.associated_data(publication)
            )
            if not 1 <= len(password) <= MAX_PASSWORD_BYTES:
                raise ValueError("Invalid password length")
            return password.decode("utf-8")
        except (InvalidTag, ValueError, TypeError, KeyError, binascii.Error) as exc:
            raise PublishError(
                "publication_password_unavailable",
                503,
                "The saved password copy cannot be read. Restore its matching key and data or set a new publication password.",
            ) from exc
