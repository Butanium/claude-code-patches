#!/usr/bin/env python3
"""Put back a missing pristine `<binary>.orig` backup, verified against the release manifest.

    python3 restore_orig.py                  # the live claude binary
    python3 restore_orig.py --binary PATH    # a specific one (e.g. versions/2.1.283)

`zz-bytecode-off.py` diffs the patched binary against `.orig` to find the
modules it must switch to source; without the backup it can do nothing. This
downloads the same version from Claude Code's release bucket (the one the
official installer uses), checks the file's sha256 against that version's
manifest.json, and installs it as `<binary>.orig` without exec bits (see
`_binpatch.protect_backup`). It never touches the binary itself.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _binpatch import candidate_binaries, protect_backup

BUCKET = ("https://storage.googleapis.com/claude-code-dist-86c565f3-f756-42ad-8dfa-d59b1c096819"
          "/claude-code-releases")


def platform_key() -> str:
    machine = platform.machine().lower()
    arch = "arm64" if machine in ("arm64", "aarch64") else "x64"
    if sys.platform == "darwin":
        return f"darwin-{arch}"
    if sys.platform == "win32":
        return f"win32-{arch}"
    musl = "musl" in (platform.libc_ver()[0] or "") or Path("/lib/ld-musl-x86_64.so.1").exists()
    return f"linux-{arch}" + ("-musl" if musl else "")


def binary_version(binp: Path) -> str:
    m = re.fullmatch(r"(\d+\.\d+\.\d+)(\.exe)?", binp.name)
    if m:
        return m.group(1)
    with open(binp, "rb") as f:  # every bundled chunk carries a "// Version: X.Y.Z" header
        for block in iter(lambda: f.read(1 << 22), b""):
            m = re.search(rb"// Version: (\d+\.\d+\.\d+)", block)
            if m:
                return m.group(1).decode()
    raise RuntimeError(f"can't tell which version {binp} is; pass --version")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("--binary", type=Path, help="default: the live claude binary")
    ap.add_argument("--version", help="default: read from the binary")
    ap.add_argument("--force", action="store_true", help="replace an existing .orig")
    a = ap.parse_args()

    binp = a.binary.expanduser().absolute() if a.binary else (candidate_binaries() or [None])[0]
    if binp is None or not binp.is_file():
        print("no claude binary found; pass --binary", file=sys.stderr)
        return 1
    orig = binp.with_name(binp.name + ".orig")
    if orig.exists() and not a.force:
        print(f"{orig} already exists (use --force to replace it)", file=sys.stderr)
        return 1

    version = a.version or binary_version(binp)
    key = platform_key()
    with urllib.request.urlopen(f"{BUCKET}/{version}/manifest.json", timeout=30) as r:
        entry = json.load(r)["platforms"][key]
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(prefix=orig.name + ".", dir=str(orig.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out, \
                urllib.request.urlopen(f"{BUCKET}/{version}/{key}/{entry['binary']}", timeout=60) as r:
            for block in iter(lambda: r.read(1 << 20), b""):
                digest.update(block)
                out.write(block)
        if digest.hexdigest() != entry["checksum"]:
            raise RuntimeError(f"sha256 {digest.hexdigest()} != manifest {entry['checksum']}")
        if tmp.stat().st_size != binp.stat().st_size:
            raise RuntimeError(f"downloaded {tmp.stat().st_size} B but {binp} is {binp.stat().st_size} B "
                               f"— wrong version?")
        protect_backup(tmp)
        os.replace(tmp, orig)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    print(f"restored {orig} ({version} {key}, sha256 {entry['checksum'][:12]}… matches the manifest)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
