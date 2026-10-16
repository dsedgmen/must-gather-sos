"""Bundle the collected directory into a tarball, sos-report style."""

from __future__ import annotations

import os
import tarfile

from . import UserError

COMPRESSION = {
    "xz": ("w:xz", ".tar.xz"),
    "gz": ("w:gz", ".tar.gz"),
    "none": ("w", ".tar"),
}


def archive_name(case_id, when, compression="xz"):
    stamp = when.strftime("%Y%m%dT%H%M%SZ")
    parts = ["must-gather"]
    if case_id:
        parts.append(case_id)
    parts.append(stamp)
    return "-".join(parts) + COMPRESSION[compression][1]


def human_size(num_bytes):
    size = float(num_bytes)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024 or unit == "TiB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024
    return f"{size:.1f} TiB"  # pragma: no cover


def create(source_dir, case_id, when, compression="xz", dest_dir=None):
    """Tar up *source_dir*. Returns (path, size_in_bytes)."""
    if compression not in COMPRESSION:
        raise UserError(
            f"unknown archive format {compression!r}; expected one of "
            f"{', '.join(COMPRESSION)}"
        )
    if not os.path.isdir(source_dir):
        raise UserError(f"nothing to archive: {source_dir} is not a directory")

    mode, _ = COMPRESSION[compression]
    out_path = os.path.join(
        dest_dir if dest_dir is not None else os.path.dirname(os.path.abspath(source_dir)),
        archive_name(case_id, when, compression),
    )

    arcname = os.path.basename(os.path.normpath(source_dir))
    with tarfile.open(out_path, mode) as tar:
        tar.add(source_dir, arcname=arcname)
    return out_path, os.path.getsize(out_path)
