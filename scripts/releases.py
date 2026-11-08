#!/usr/bin/env python3
"""Stage streamed SONiC assets in draft releases, then publish complete builds."""

import argparse
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

from sonic_vs import TARGETS, checksum, owned_work, selected_images, write_json

ASSET_LIMIT = 2 * 1024**3  # GitHub requires strictly less than 2 GiB per asset.
PART_SIZE = 1024**3


def api(repository, path, method="GET", body=None, file=None):
    url = f"https://api.github.com/repos/{repository}{path}"
    headers = {"Authorization": f"Bearer {os.environ['GH_TOKEN']}",
               "Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
    if file is not None:
        # Explicit Content-Length streams files without buffering multi-GB assets.
        url = f"https://uploads.github.com/repos/{repository}{path}"
        headers.update({"Content-Type": "application/octet-stream", "Content-Length": str(file.stat().st_size)})
        with file.open("rb") as stream:
            request = urllib.request.Request(url, data=stream, headers=headers, method=method)
            with urllib.request.urlopen(request, timeout=1800) as response:
                return json.load(response)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=120) as response:
        return json.load(response)


def release(repository, tag):
    # The by-tag endpoint is for published releases. List with write credentials
    # to find drafts too, so the second platform reuses the first platform's draft.
    page = 1
    while True:
        batch = api(repository, f"/releases?per_page=100&page={page}")
        for item in batch:
            if item["tag_name"] == tag:
                return item
        if len(batch) < 100:
            return None
        page += 1


def upload(repository, release_id, path):
    if path.stat().st_size >= ASSET_LIMIT:
        raise ValueError(f"Release asset exceeds GitHub's per-file limit: {path.name}")
    name = urllib.parse.quote(path.name, safe="")
    print(f"Uploading {path.name} ({path.stat().st_size:,} bytes)", flush=True)
    api(repository, f"/releases/{release_id}/assets?name={name}", "POST", file=path)


def stage(work, repository, tag, target):
    work = owned_work(work)
    metadata_path = work / "metadata.json"
    metadata = json.loads((metadata_path if metadata_path.exists() else work / "validated.json").read_text())
    platform = metadata["platform"]
    filename = TARGETS[platform]
    source = work / "source" / "target" / filename
    # Recheck the checksum before uploading an already validated build.
    if metadata["sha256"] != checksum(source):
        raise ValueError("Output changed after validation")
    existing = release(repository, tag)
    if existing is None:
        existing = api(repository, "/releases", "POST", {
            "tag_name": tag, "target_commitish": target, "name": f"SONiC {metadata['branch']} — {tag}",
            "draft": True, "make_latest": "false",
            "body": f"Source: {metadata['upstream']}\n\nBranch: `{metadata['branch']}`\n\n"
                    f"Upstream commit: `{metadata['commit']}`\n\n"
                    f"Builder commit: `{target}`\n\n"
                    f"[Build logs](https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']})\n\n"
                    "Each selected platform has a metadata JSON, SHA256SUMS and download instructions. "
                    "Files at or above 2 GiB are split; reassemble their parts before use. "
                    "The Broadcom installer is not hardware-boot-tested by this workflow.\n",
        })
    if not existing["draft"]:
        raise ValueError("Refusing to change an already published release; start a new workflow run")
    release_id = existing["id"]
    # Never overwrite another successful build's assets.
    assets = []
    with tempfile.TemporaryDirectory(prefix="release-assets-", dir=work) as directory:
        staging = Path(directory)
        if source.stat().st_size < ASSET_LIMIT:
            upload(repository, release_id, source)
            assets.append(filename)
            instructions = f"Download {filename}.\n"
        else:
            with source.open("rb") as stream:
                part = 1
                while True:
                    path = staging / f"{filename}.part{part:04d}"
                    written = 0
                    with path.open("wb") as output:
                        while written < PART_SIZE:
                            block = stream.read(min(1024 * 1024, PART_SIZE - written))
                            if not block:
                                break
                            output.write(block)
                            written += len(block)
                    if not written:
                        path.unlink()
                        break
                    upload(repository, release_id, path)
                    assets.append(path.name)
                    path.unlink()
                    part += 1
            instructions = (f"Download all {filename}.part* files into one directory.\n"
                            f"cat {filename}.part* > {filename}\n")
        instructions += f"sha256sum -c {platform}-SHA256SUMS\n"
        if platform == "vs":
            instructions += f"docker load -i {filename}\n"
        elif platform == "kvm":
            instructions += (f"gzip -dk {filename}\n"
                             "The decompressed sonic-vs.img is a QCOW2 disk for QEMU/KVM.\n"
                             "Import it into your VM/lab system; configure resources and NICs for your topology.\n")
        else:
            instructions += "Use the verified sonic-broadcom.bin with ONIE on a supported switch.\n"
        metadata["release_assets"] = assets
        files = {f"{platform}-SHA256SUMS": f"{metadata['sha256']}  {filename}\n",
                 f"{platform}-README.txt": instructions,
                 f"{platform}-metadata.json": json.dumps(metadata, indent=2) + "\n"}
        for name, content in files.items():
            path = staging / name
            path.write_text(content)
            upload(repository, release_id, path)
    write_json(work / "release.json", {"tag": tag, "url": existing["html_url"], "assets": assets})


def finish(repository, tag, images):
    existing = release(repository, tag)
    if not existing or not existing["draft"]:
        raise ValueError("Expected a draft release for this build")
    expected = selected_images(images)
    # Paginate: split images can produce many assets.
    names = set()
    for page in range(1, 12):
        batch = api(repository, f"/releases/{existing['id']}/assets?per_page=100&page={page}")
        names.update(item["name"] for item in batch)
        if len(batch) < 100:
            break
    for platform in expected:
        filename = TARGETS[platform]
        for required in (f"{platform}-metadata.json", f"{platform}-SHA256SUMS", f"{platform}-README.txt"):
            if required not in names:
                raise ValueError(f"Release is incomplete: missing {required}")
        if filename not in names and f"{filename}.part0001" not in names:
            raise ValueError(f"Release is incomplete: missing {filename}")
    published = api(repository, f"/releases/{existing['id']}", "PATCH", {"draft": False, "make_latest": "false"})
    print(published["html_url"])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(f"\nDownload images: [{tag}]({published['html_url']})\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("stage", "finish"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--work")
    parser.add_argument("--target")
    parser.add_argument("--images", choices=("all", "both", *TARGETS), default="all")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise ValueError("Expected GitHub owner/repository")
    if args.command == "stage":
        if not args.work or not args.target:
            parser.error("stage requires --work and --target")
        stage(args.work, args.repository, args.tag, args.target)
    else:
        finish(args.repository, args.tag, args.images)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, urllib.error.URLError) as error:
        sys.exit(f"ERROR: {error}")
