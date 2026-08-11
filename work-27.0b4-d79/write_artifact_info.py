#!/usr/bin/env python3
"""Write identity and SHA-256 metadata for a d79 boot artifact."""

import argparse
import hashlib
import json
from pathlib import Path


def digest(path):
    hasher = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", required=True, choices=("sshrd", "normal-experimental"))
    parser.add_argument("--directory", type=Path, required=True)
    args = parser.parse_args()

    directory = args.directory.resolve()
    output = directory / "artifact-info.json"
    files = {}
    for path in sorted(directory.rglob("*")):
        if path.is_file() and path != output:
            files[path.relative_to(directory).as_posix()] = digest(path)
    if not files:
        raise SystemExit("artifact directory is empty")

    info = {
        "schema": 1,
        "product": "iPhone12,8",
        "board": "d79ap",
        "build": "24A5390f",
        "mode": args.mode,
        "files": files,
    }
    output.write_text(json.dumps(info, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {output} with {len(files)} SHA-256 entries")


if __name__ == "__main__":
    main()
