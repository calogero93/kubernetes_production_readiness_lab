"""Download the pinned public GGUF and verify its SHA-256 before serving it."""

from __future__ import annotations

import hashlib
import json
import shutil
import urllib.request
from pathlib import Path


def checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> None:
    manifest_path = Path("/setup/model.json")
    manifest = json.loads(manifest_path.read_text())
    destination = Path("/models/model.gguf")
    expected = manifest["sha256"]
    if destination.exists() and checksum(destination) == expected:
        print("Pinned model is already downloaded and verified.", flush=True)
        return
    url = (
        f"https://huggingface.co/{manifest['repository']}/resolve/"
        f"{manifest['revision']}/{manifest['filename']}"
    )
    temporary = destination.with_suffix(".gguf.part")
    print(f"Downloading {manifest['filename']} ({manifest['size_bytes']} bytes).", flush=True)
    with urllib.request.urlopen(url, timeout=60) as response, temporary.open("wb") as output:
        shutil.copyfileobj(response, output, length=8 * 1024 * 1024)
    if temporary.stat().st_size != manifest["size_bytes"] or checksum(temporary) != expected:
        raise ValueError("Downloaded GGUF size or SHA-256 does not match the pinned manifest")
    temporary.replace(destination)
    shutil.copyfile(manifest_path, destination.parent / "model.json")
    print(f"Model verified: sha256:{expected}", flush=True)


if __name__ == "__main__":
    main()
