"""Content hash of the API's build context, so anyone can check the published source.

Lists every file as "<sha256>  <path>" (sha256sum format, paths sorted, "/" separators) and
hashes that list. The Docker build stores the hash of /app; the source mirror's SHA256SUMS
file holds the list and its commit message the hash. Usage: python source_manifest.py DIR [--list]
"""
import hashlib
import sys
from pathlib import Path


def manifest(root: str) -> str:
    base = Path(root)
    files = [p for p in base.rglob("*") if p.is_file() and "__pycache__" not in p.parts]
    lines = sorted(
        (p.relative_to(base).as_posix(), hashlib.sha256(p.read_bytes()).hexdigest()) for p in files
    )
    return "".join(f"{digest}  {path}\n" for path, digest in lines)


if __name__ == "__main__":
    text = manifest(sys.argv[1])
    print(text, end="") if "--list" in sys.argv else print(hashlib.sha256(text.encode()).hexdigest())
