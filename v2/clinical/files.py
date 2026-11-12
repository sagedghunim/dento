"""Attachment bytes (design E3): images and PDFs up to 10 MB, behind a storage class so S3 can replace it.

The database row (clinical_attachment) is the only way to a file: a key is never taken from the browser, and a
row is found under the clinic's row-level security before its bytes are read. Keys start with the clinic id so
a bucket policy or a move between shards can work per clinic.
"""

import hashlib
import os
import uuid
from pathlib import Path

from django.conf import settings
from django.utils import timezone
from django.utils.module_loading import import_string

MAX_BYTES = 10 * 1024 * 1024

# Sniffed from the bytes, never from the client's Content-Type or file name.
KINDS = (
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"%PDF-", "application/pdf"),
)


def sniff(head):
    """The content type of a file from its first bytes, or None when it is not an image or a PDF we accept."""
    for magic, content_type in KINDS:
        if head.startswith(magic):
            return content_type
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    return None


class LocalStorage:
    """Files on the server's disk under ATTACHMENTS_ROOT. Fine for one server; S3 replaces it for several."""

    def __init__(self, root=None):
        self.root = Path(root or settings.ATTACHMENTS_ROOT)

    def _path(self, key):
        path = (self.root / key).resolve()
        if self.root.resolve() not in path.parents:
            raise ValueError("storage key outside the root")
        return path

    def save(self, clinic_id, chunks):
        """Write the chunks under a new key. Returns (key, size, sha256 hex)."""
        now = timezone.now()
        key = f"{int(clinic_id)}/{now:%Y/%m}/{uuid.uuid4().hex}"
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        digest, size = hashlib.sha256(), 0
        with open(path, "xb") as out:
            for chunk in chunks:
                digest.update(chunk)
                size += len(chunk)
                out.write(chunk)
        return key, size, digest.hexdigest()

    def open(self, key):
        return open(self._path(key), "rb")

    def delete(self, key):
        try:
            os.remove(self._path(key))
        except FileNotFoundError:
            pass


def storage():
    return import_string(settings.ATTACHMENT_STORAGE)()
