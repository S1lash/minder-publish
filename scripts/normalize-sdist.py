#!/usr/bin/env python3
"""Remove build-machine metadata from a locally generated source distribution."""

import argparse
import gzip
import tarfile
import tempfile
from pathlib import Path


def normalize(path: Path) -> None:
    with tempfile.TemporaryDirectory(dir=path.parent) as scratch:
        output = Path(scratch) / path.name
        with (
            tarfile.open(path, "r:gz") as source,
            output.open("wb") as compressed,
            gzip.GzipFile(fileobj=compressed, filename="", mode="wb", mtime=0) as encoded,
            tarfile.open(fileobj=encoded, mode="w|", format=tarfile.PAX_FORMAT) as target,
        ):
            for member in source:
                if not (member.isdir() or member.isfile()) or member.issparse():
                    raise ValueError("Only regular files and directories belong in a source distribution.")
                member.uid = member.gid = member.mtime = 0
                member.uname = member.gname = "root"
                member.pax_headers = {}
                if member.isdir():
                    target.addfile(member)
                else:
                    with source.extractfile(member) as content:
                        target.addfile(member, content)
        output.chmod(path.stat().st_mode & 0o777)
        output.replace(path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    args = parser.parse_args()
    if (
        not args.archive.name.endswith(".tar.gz")
        or args.archive.is_symlink()
        or not args.archive.is_file()
    ):
        parser.error("Select one locally generated .tar.gz source distribution.")
    normalize(args.archive)
