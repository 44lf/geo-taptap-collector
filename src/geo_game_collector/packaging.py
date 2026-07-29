"""Deterministic, integrity-checked Bundle v2 tar.zst packaging."""

from __future__ import annotations

import hashlib
import json
import os
import tarfile
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

import zstandard

from geo_game_collector.bundle import BundleManifestV2


class BundlePackagingError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class BundlePackageResult:
    path: Path
    object_key: str
    archive_bytes: int
    archive_sha256: str
    file_count: int


def _sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _verify_mime(path: Path, media_type: str) -> None:
    data = path.read_bytes()
    try:
        if media_type == "application/json":
            json.loads(data.decode("utf-8"))
            return
        if media_type == "text/html":
            data.decode("utf-8")
            return
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BundlePackagingError(f"MIME signature mismatch for {path.name}") from exc
    signatures = {
        "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": data.startswith(b"\xff\xd8\xff"),
        "image/webp": len(data) >= 12
        and data.startswith(b"RIFF")
        and data[8:12] == b"WEBP",
        "image/gif": data.startswith((b"GIF87a", b"GIF89a")),
    }
    if not signatures.get(media_type, False):
        raise BundlePackagingError(f"MIME signature mismatch for {path.name}")


def _tar_info(name: str, size: int) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size = size
    info.mode = 0o644
    info.mtime = 0
    info.uid = 0
    info.gid = 0
    info.uname = ""
    info.gname = ""
    return info


def package_bundle(
    manifest: BundleManifestV2,
    *,
    files: dict[str, Path],
    destination: Path,
) -> BundlePackageResult:
    expected_paths = {entry.path for entry in manifest.files}
    if set(files) != expected_paths:
        raise BundlePackagingError("files must exactly match the Bundle inventory")
    for entry in manifest.files:
        source = files[entry.path]
        if not source.is_file():
            raise BundlePackagingError(f"inventory source is missing: {entry.path}")
        if source.stat().st_size != entry.size_bytes:
            raise BundlePackagingError(f"file size mismatch for {entry.path}")
        if _sha(source) != entry.sha256:
            raise BundlePackagingError(f"file SHA-256 mismatch for {entry.path}")
        _verify_mime(source, entry.media_type)

    manifest_bytes = (
        json.dumps(
            manifest.to_dict(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )
    if len(manifest_bytes) > manifest.limits.max_file_bytes:
        raise BundlePackagingError("manifest exceeds max_file_bytes")

    destination.parent.mkdir(parents=True, exist_ok=True)
    raw_handle = tempfile.NamedTemporaryFile(
        prefix=".bundle.",
        suffix=".tar",
        dir=destination.parent,
        delete=False,
    )
    raw_tar = Path(raw_handle.name)
    raw_handle.close()
    compressed_handle = tempfile.NamedTemporaryFile(
        prefix=f".{destination.name}.",
        suffix=".tmp",
        dir=destination.parent,
        delete=False,
    )
    compressed = Path(compressed_handle.name)
    compressed_handle.close()
    try:
        with tarfile.open(raw_tar, mode="w", format=tarfile.PAX_FORMAT) as bundle:
            bundle.addfile(
                _tar_info("manifest.json", len(manifest_bytes)),
                BytesIO(manifest_bytes),
            )
            for entry in sorted(manifest.files, key=lambda item: item.path):
                source = files[entry.path]
                with source.open("rb") as stream:
                    bundle.addfile(
                        _tar_info(entry.path, entry.size_bytes),
                        stream,
                    )
        compressor = zstandard.ZstdCompressor(
            level=10,
            write_checksum=True,
            write_content_size=True,
            threads=0,
        )
        with raw_tar.open("rb") as raw_stream, compressed.open("wb") as target:
            compressor.copy_stream(raw_stream, target)
            target.flush()
            os.fsync(target.fileno())
        os.replace(compressed, destination)
    except BaseException:
        compressed.unlink(missing_ok=True)
        destination.unlink(missing_ok=True)
        raise
    finally:
        raw_tar.unlink(missing_ok=True)

    archive_bytes = destination.stat().st_size
    archive_sha256 = _sha(destination)
    return BundlePackageResult(
        path=destination,
        object_key=(
            f"incoming/{manifest.correlations.collector_id}/"
            f"{manifest.correlations.transport_id}.tar.zst"
        ),
        archive_bytes=archive_bytes,
        archive_sha256=archive_sha256,
        file_count=len(manifest.files) + 1,
    )
