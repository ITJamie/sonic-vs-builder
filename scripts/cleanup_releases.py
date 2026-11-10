#!/usr/bin/env python3
"""Remove old GitHub releases with no attached assets; dry-run by default."""
import argparse
from datetime import datetime, timedelta, timezone
import re
import sys
import urllib.error

from releases import api


def timestamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def old_enough(release, cutoff):
    # Also protect recently edited drafts, even when their creation is old.
    return (timestamp(release["created_at"]) < cutoff and
            timestamp(release.get("updated_at") or release["created_at"]) < cutoff)


def cleanup(repository, days=2, apply=False, now=None):
    if days < 2:
        raise ValueError("Retention must be at least 2 days")
    cutoff = (now or datetime.now(timezone.utc)) - timedelta(days=days)
    candidates = []
    page = 1
    # Finish pagination before deleting, so shifting pages cannot skip releases.
    while True:
        batch = api(repository, f"/releases?per_page=100&page={page}")
        candidates.extend(item for item in batch if not item["assets"] and old_enough(item, cutoff))
        if len(batch) < 100:
            break
        page += 1
    removed = []
    for item in candidates:
        path = f"/releases/{item['id']}"
        current = api(repository, path)
        # Recheck both the release and authoritative asset endpoint just before deletion.
        if not old_enough(current, cutoff) or current["assets"]:
            continue
        if api(repository, path + "/assets?per_page=1"):
            continue
        print(f"{'DELETE' if apply else 'DRY RUN'}: {current['tag_name']} (release {current['id']})", flush=True)
        if apply:
            api(repository, path, "DELETE")
        removed.append(current["id"])
    print(f"{len(removed)} empty releases {'deleted' if apply else 'eligible'}; Git tags and registry packages retained.")
    return removed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--days", type=int, default=2)
    parser.add_argument("--apply", action="store_true", help="Delete eligible releases instead of listing them")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", args.repository):
        parser.error("Expected GitHub owner/repository")
    cleanup(args.repository, args.days, args.apply)


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, urllib.error.URLError) as error:
        sys.exit(f"ERROR: {error}")
