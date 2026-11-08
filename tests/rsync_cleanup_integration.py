"""Verify native cached cleanup leaves rsync available for final-stage copying."""

from pathlib import Path
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import fix_cached_rsync_cleanup

COMMIT = "bafb878dcd083e9650672f1a870089d03f1244d0"
URL = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMIT}/src/sonic-build-hooks/scripts/post_run_cleanup"
upstream = urllib.request.urlopen(URL, timeout=60).read().decode()

with tempfile.TemporaryDirectory() as directory:
    source = Path(directory)
    cleanup = source / "src/sonic-build-hooks/scripts/post_run_cleanup"
    cleanup.parent.mkdir(parents=True)
    cleanup.write_text(upstream)
    macros = source / "dockers/dockerfile-macros.j2"
    macros.parent.mkdir()
    macros.write_text("{% macro rsync_from_builder_stage() -%}\n{% endmacro %}\n")
    template = source / "dockers/docker-base-bookworm/Dockerfile.j2"
    template.parent.mkdir()
    template.write_text("FROM debian:bookworm-slim\n")
    assert fix_cached_rsync_cleanup(source, "read-write") == ["cached-rsync-cleanup"]
    assert "apt-get purge -y --auto-remove rsync" not in cleanup.read_text()
    assert fix_cached_rsync_cleanup(source, "read-write") == []
    assert "cached-rsync cleanup compatibility" in template.read_text()
    (source / "buildinfo_base.sh").write_text('''
BUILD_VERSION_PATH=/tmp/build-versions
VERSION_DEB_PREFERENCE=/tmp/version-preference
get_version_cache_option() { echo cache; }
symlink_build_hooks() { :; }
set_reproducible_mirrors() { :; }
''')
    (source / "Dockerfile").write_text('''
FROM debian:bookworm-slim AS parent
RUN apt-get update && apt-get install -y --no-install-recommends rsync
COPY buildinfo_base.sh /usr/local/share/buildinfo/scripts/buildinfo_base.sh
COPY src/sonic-build-hooks/scripts/post_run_cleanup /usr/local/bin/post_run_cleanup
RUN bash /usr/local/bin/post_run_cleanup docker-base-bookworm
FROM parent AS base
RUN mkdir -p /opt/test && echo preserved > /opt/test/payload
FROM parent
RUN --mount=type=bind,from=base,target=/changes-to-image rsync -axAX --omit-dir-times --no-D --exclude=/sys --exclude=/proc --exclude=/dev --exclude=resolv.conf /changes-to-image/ /
RUN command -v rsync && dpkg-query -W rsync && test "$(cat /opt/test/payload)" = preserved
''')
    subprocess.run(["docker", "build", "--progress=plain", str(source)], check=True)
print("Cached cleanup retains registered rsync package and final-stage copy succeeds")
