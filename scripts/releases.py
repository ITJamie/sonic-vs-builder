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

from sonic_vs import (TARGETS, SHA, checksum, install_signal_handlers, maintenance_root,
                      owned_work, runner_lock, selected_images, write_json)

ASSET_LIMIT = 2 * 1024**3  # GitHub requires strictly less than 2 GiB per asset.
PART_SIZE = 1024**3


class GitHubAPIError(ValueError):
    def __init__(self, method, path, status, message):
        self.status = status
        super().__init__(f"GitHub {method} {path}: HTTP {status}: {message}")


def response_json(request, timeout):
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return None if getattr(response, "status", None) == 204 else json.load(response)
    except urllib.error.HTTPError as error:
        try:
            message = json.load(error).get("message", error.reason)
        except (ValueError, AttributeError):
            message = error.reason
        token = os.environ.get("GH_TOKEN", "")
        if token:
            message = str(message).replace(token, "***")
        raise GitHubAPIError(request.method, urllib.parse.urlsplit(request.full_url).path,
                             error.code, message) from None


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
            return response_json(request, 1800)
    data = json.dumps(body).encode() if body is not None else None
    if data:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    return response_json(request, 120)


def anchor(repository, tag, target):
    """Pin the builder while it is still a branch head, before a long build."""
    if not SHA.fullmatch(target):
        raise ValueError("Release tag requires an exact builder commit SHA")
    try:
        existing = api(repository, "/git/ref/tags/" + urllib.parse.quote(tag, safe=""))
    except GitHubAPIError as error:
        if error.status != 404:
            raise
        api(repository, "/git/refs", "POST", {"ref": f"refs/tags/{tag}", "sha": target})
    else:
        if existing["object"].get("type") != "commit" or existing["object"].get("sha") != target:
            raise ValueError("Existing release tag does not point at this builder commit")
    print(f"Release tag {tag} anchored at {target}", flush=True)


def release(repository, tag):
    # The by-tag endpoint is for published releases. List with write credentials
    # to find drafts too, so the second platform reuses the first platform's draft.
    page = 1
    drafts = []
    while True:
        batch = api(repository, f"/releases?per_page=100&page={page}")
        for item in batch:
            if item["tag_name"] == tag:
                return item
            # GitHub can expose a draft under an untagged-* placeholder even
            # though creation supplied our anchored tag. Its build-specific
            # title stays intact. Validate the commit before reusing it.
            if (item.get("draft") and item["tag_name"].startswith("untagged-") and
                    item.get("name", "").startswith("SONiC ") and
                    item.get("name", "").endswith(f" — {tag}")):
                drafts.append(item)
        if len(batch) < 100:
            break
        page += 1
    if len(drafts) > 1:
        raise ValueError("Multiple draft releases match this build; refusing to guess")
    return drafts[0] if drafts else None


def upload(repository, release_id, path):
    if path.stat().st_size >= ASSET_LIMIT:
        raise ValueError(f"Release asset exceeds GitHub's per-file limit: {path.name}")
    name = urllib.parse.quote(path.name, safe="")
    print(f"Uploading {path.name} ({path.stat().st_size:,} bytes)", flush=True)
    api(repository, f"/releases/{release_id}/assets?name={name}", "POST", file=path)


def draft_release(repository, tag, target, metadata):
    existing = release(repository, tag)
    if existing and existing["tag_name"] != tag and existing.get("target_commitish") != target:
        raise ValueError("Draft release does not match this builder commit")
    ztp = metadata.get("enable_ztp")
    ztp_note = ("ZTP: **Enabled** (`ENABLE_ZTP=y`)" if ztp is True else
                "ZTP: **Disabled** (`ENABLE_ZTP=n`)" if ztp is False else
                "ZTP: **Not recorded** (older build)")
    ztp_note += ". Applies to KVM/hardware installers; the reduced Docker VS image does not include ZTP."
    if existing is None:
        existing = api(repository, "/releases", "POST", {
            "tag_name": tag, "target_commitish": target, "name": f"SONiC {metadata['branch']} — {tag}",
            "draft": True, "make_latest": "false",
            "body": f"Source: {metadata['upstream']}\n\nBranch: `{metadata['branch']}`\n\n"
                    f"Upstream commit: `{metadata['commit']}`\n\n"
                    f"Builder commit: `{target}`\n\n"
                    f"{ztp_note}\n\n"
                    f"[Build logs](https://github.com/{repository}/actions/runs/{os.environ['GITHUB_RUN_ID']})\n\n"
                    "Each selected platform has a metadata JSON, SHA256SUMS and download instructions. "
                    "Files at or above 2 GiB are split; reassemble their parts before use. "
                    "The Broadcom installer is not hardware-boot-tested by this workflow.\n",
        })
    elif existing["draft"] and ztp is not None:
        # Preserve other notes when staging later platforms in the same draft.
        body = existing.get("body") or ""
        updated = re.sub(r"^ZTP:.*$", ztp_note, body, flags=re.MULTILINE)
        if updated == body and not re.search(r"^ZTP:", body, re.MULTILINE):
            updated = body.rstrip() + "\n\n" + ztp_note + "\n"
        if updated != body:
            existing = api(repository, f"/releases/{existing['id']}", "PATCH", {"body": updated})
    if not existing["draft"]:
        raise ValueError("Refusing to change an already published release; start a new workflow run")
    return existing


def stage(work, repository, tag, target):
    work = owned_work(work)
    metadata_path = work / "metadata.json"
    metadata = json.loads((metadata_path if metadata_path.exists() else work / "validated.json").read_text())
    platform = metadata["platform"]
    filename = TARGETS[platform]
    source = work / "source" / "target" / filename
    if not source.exists():
        source = work / "retained-output" / filename
    # Recheck the checksum before uploading an already validated build.
    if metadata["sha256"] != checksum(source):
        raise ValueError("Output changed after validation")
    existing = draft_release(repository, tag, target, metadata)
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
    if existing["tag_name"] != tag:
        reference = api(repository, "/git/ref/tags/" + urllib.parse.quote(tag, safe=""))
        if (reference["object"].get("type") != "commit" or
                reference["object"].get("sha") != existing.get("target_commitish")):
            raise ValueError("Draft release does not match the anchored builder commit")
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
    published = api(repository, f"/releases/{existing['id']}", "PATCH", {"tag_name": tag, "draft": False, "make_latest": "false"})
    print(published["html_url"])
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as stream:
            stream.write(f"\nDownload images: [{tag}]({published['html_url']})\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("anchor", "stage", "finish"))
    parser.add_argument("--repository", required=True)
    parser.add_argument("--tag", required=True)
    parser.add_argument("--work")
    parser.add_argument("--target")
    parser.add_argument("--images", choices=("all", "both", *TARGETS), default="all")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        raise ValueError("Expected GitHub owner/repository")
    if args.command == "anchor":
        if not args.target:
            parser.error("anchor requires --target")
        anchor(args.repository, args.tag, args.target)
    elif args.command == "stage":
        if not args.work or not args.target:
            parser.error("stage requires --work and --target")
        work = owned_work(args.work)
        with runner_lock(maintenance_root(work=work)):
            stage(work, args.repository, args.tag, args.target)
    else:
        finish(args.repository, args.tag, args.images)


if __name__ == "__main__":
    install_signal_handlers()
    try:
        main()
    except (ValueError, OSError, urllib.error.URLError) as error:
        sys.exit(f"ERROR: {error}")
