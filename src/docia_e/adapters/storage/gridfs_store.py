"""GridFS. El identificador público no es el ObjectId ni una ruta del disco."""

from __future__ import annotations

import hashlib
from pathlib import Path

from bson import ObjectId
from gridfs import GridFSBucket


class GridFSBlobStore:
    def __init__(self, db, bucket_name: str) -> None:
        self.bucket = GridFSBucket(db, bucket_name=bucket_name)

    def put_path(self, path: Path, file_id: str, filename: str, mime: str) -> str:
        oid = ObjectId()
        with path.open("rb") as source, self.bucket.open_upload_stream_with_id(
            oid,
            file_id,
            metadata={"file_id": file_id, "display_name": filename, "mime_type": mime},
        ) as dest:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                dest.write(chunk)
        return str(oid)

    def put_bytes(self, data: bytes, file_id: str, filename: str, mime: str) -> tuple[str, str, int]:
        oid = ObjectId()
        with self.bucket.open_upload_stream_with_id(
            oid,
            file_id,
            metadata={"file_id": file_id, "display_name": filename, "mime_type": mime},
        ) as dest:
            dest.write(data)
        return str(oid), hashlib.sha256(data).hexdigest(), len(data)

    def matches(self, gridfs_id: str, sha256: str, size: int) -> bool:
        digest = hashlib.sha256()
        total = 0
        try:
            stream = self.bucket.open_download_stream(ObjectId(gridfs_id))
        except Exception:
            return False
        with stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
                total += len(chunk)
        return digest.hexdigest() == sha256 and total == size

    def write_to(self, gridfs_id: str, path: Path) -> None:
        with self.bucket.open_download_stream(ObjectId(gridfs_id)) as source, path.open("wb") as target:
            for chunk in iter(lambda: source.read(1024 * 1024), b""):
                target.write(chunk)

    def open(self, gridfs_id: str):
        return self.bucket.open_download_stream(ObjectId(gridfs_id))

    def delete(self, gridfs_id: str) -> None:
        try:
            self.bucket.delete(ObjectId(gridfs_id))
        except Exception:
            return
