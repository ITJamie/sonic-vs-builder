"""Fetch the exact original files consumed by a set of upstream patches."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import urllib.request


def populate_patch_sources(source, commit, patches, research_cache=None, extra_paths=()):
    paths = {line.removeprefix("--- a/") for patch in patches
             for line in patch.read_text().splitlines() if line.startswith("--- a/")}
    # A later patch can modify a file introduced earlier in the series. Such
    # files have no original at the pinned upstream commit (HTTP 404).
    added = set()
    for patch in patches:
        lines = patch.read_text().splitlines()
        added.update(right.removeprefix("+++ b/") for left, right in zip(lines, lines[1:])
                     if left == "--- /dev/null" and right.startswith("+++ b/"))
    paths -= added
    paths.update(extra_paths)

    def fetch(path):
        destination = source / path
        if destination.exists():
            return
        cached = Path(research_cache) / path if research_cache else None
        if cached and cached.is_file():
            data = cached.read_bytes()
        else:
            repo, revision, relative = "sonic-buildimage", commit, path
            if path.startswith("src/sonic-platform-common/"):
                repo, revision = "sonic-platform-common", "910b38bc5d94eb3475cd670a42a0f95250bdc184"
                relative = path.removeprefix("src/sonic-platform-common/")
            url = f"https://raw.githubusercontent.com/sonic-net/{repo}/{revision}/{relative}"
            with urllib.request.urlopen(url, timeout=60) as response:
                data = response.read()
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(data)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(fetch, sorted(paths)))
