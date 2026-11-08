"""Verify actual Docker image identities and preservation of existing tags."""

import gzip
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import uuid

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import archive_image, capture, load_untagged, permissions_image

tag = "sonic-vs-builder-load-test:" + uuid.uuid4().hex[:12]
base = permissions_image()
subprocess.run(["docker", "tag", base, tag], check=True)
original = capture("docker", "image", "inspect", "--format", "{{.Id}}", tag)
before = set(capture("docker", "image", "ls", "--all", "--quiet", "--no-trunc").splitlines())
loaded = None
try:
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        tar = root / "image.tar"
        archive = root / "image.tar.gz"
        subprocess.run(["docker", "save", "--output", str(tar), tag], check=True)
        with tar.open("rb") as source, gzip.open(archive, "wb") as target:
            shutil.copyfileobj(source, target)
        config = archive_image(archive)
        loaded = load_untagged(archive)
        assert capture("docker", "image", "inspect", "--format", "{{.Os}}/{{.Architecture}}", loaded) == "linux/amd64"
        assert capture("docker", "image", "inspect", "--format", "{{.Id}}", tag) == original
        print(f"Archive config {config}; Docker returned {loaded}; original tag preserved")
finally:
    subprocess.run(["docker", "image", "rm", tag], check=False)
    if loaded and loaded != original and loaded not in before:
        subprocess.run(["docker", "image", "rm", loaded], check=False)
