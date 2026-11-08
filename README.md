# SONiC image builder

Build the upstream [SONiC virtual-switch Docker image](https://github.com/sonic-net/sonic-buildimage/tree/202605/platform/vs) and [Broadcom ONIE installer](https://github.com/sonic-net/sonic-buildimage/tree/202605/platform/broadcom) from a branch you choose. Download both from [GitHub Releases](https://github.com/ITJamie/sonic-vs-builder/releases), and pull the VS container from GitHub Container Registry:

```console
docker pull ghcr.io/itjamie/sonic-vs-builder:202605
```

Images are Linux amd64, built from the selected `sonic-net/sonic-buildimage` commit. Cached builds apply targeted build-hook compatibility fixes when recognized upstream bugs are detected; metadata records applied fixes. The VS container tag is exactly the upstream branch name. Rebuilding a branch updates its container tag; use the digest from the workflow summary when you need an immutable reference. There is no additional `latest` alias. Downloadable releases retain every successful build separately.

With Docker Hub publishing enabled (the workflow default), the same VS image is also available as `itjamie/docker-sonic-vs:<branch>`.

## Set up a runner

Follow [the Ubuntu runner guide](docs/runner.md). Builds require a dedicated self-hosted runner labeled `sonic-vs`; ordinary GitHub-hosted runners only validate this builder's code. The default is tuned to our dedicated six-core, 48 GB RAM runner: three build jobs, with VS and Broadcom built sequentially. Hosts with 16 GB RAM should select one build job. Keep at least 300 GiB free on both the workspace and Docker filesystems. If those paths share a filesystem, the checks refer to the same storage, not two separate 300 GiB allocations.

## Trigger a build

Open **Actions → Build SONiC images → Run workflow** on `main`. Enter a branch, such as `202605`, or a comma-separated list, such as `202605,202511`. Choose `images=both` (default), `vs`, or `broadcom`. `build_jobs` controls upstream compilation parallelism per platform and defaults to `3`.

`cache_mode=read-write` is enabled by default. Select `off` to bypass the persistent caches for a diagnostic build; this does not erase them.

`publish_dockerhub=true` is enabled by default for VS builds. Set it to `false` for GHCR-only publication. Broadcom-only builds do not log in to or publish to either container registry.

Using the GitHub CLI:

```console
gh workflow run build.yml --repo ITJamie/sonic-vs-builder -f branches=202605 -f images=both -f build_jobs=3
gh workflow run build.yml --repo ITJamie/sonic-vs-builder -f branches=202605,202511 -f images=vs -f build_jobs=3
gh run list --repo ITJamie/sonic-vs-builder --workflow build.yml
```

All names are validated and resolved before any self-hosted job starts. Whitespace is trimmed and duplicate branches are removed. Empty entries, nonexistent branches, and branch names that cannot be represented exactly as Docker tags (for example `feature/test`) fail validation. Tags and arbitrary commit inputs are not accepted. Every build uses the resolved branch commit and its pinned submodules, even if the branch advances while waiting for the runner.

Versions run sequentially and independently: failure of one version does not cancel the others. Within a version, VS builds first, its output is uploaded, and its workspace is removed before Broadcom begins. Failure of an earlier step stops the remaining steps for that version. Build workflows share a concurrency group with cancellation disabled, preventing two builds from sharing the host. GitHub concurrency retains only one pending run, so submitting more runs while one is already pending can replace that pending run; submit several versions together in one branch list.

Each platform uses a fresh checkout of the same resolved upstream commit, then runs `make init`, `make configure PLATFORM=<vs|broadcom> PLATFORM_ARCH=amd64`, and `make SONIC_BUILD_JOBS=<jobs> <target>` with `UNATTENDED=y`. Targets are `target/docker-sonic-vs.gz` and `target/sonic-broadcom.bin`. Upstream tests and defaults remain enabled. The Broadcom target uses upstream's dependency selection, which can also build components for related Broadcom variants; this workflow uploads only the requested installer. Other historical branches may need a different upstream environment; compatibility is established by successful builds, not guaranteed for every release.

The VS archive is loaded without importing upstream image tags. Before publishing, the builder checks Linux amd64, reads `/etc/sonic/sonic_version.yml`, and runs `supervisord --version`, `rsync --version`, and `sonic-cfggen --help` in a container with no network access. This checks the configured entrypoint and key tools can execute, including their interpreter/library dependencies, but does not test a complete virtual-switch topology. The Broadcom output is checked for an ONIE shell-installer header and hashed; it is not boot-tested on switch hardware. Use it only with supported Broadcom platforms.

## Persistent runner cache

The runner keeps reusable data in `~/.cache/sonic-vs-builder`, outside the disposable build workspaces. This is runner-local storage: subsequent VS and Broadcom builds on this host can reuse it, including future workflow runs. No Actions cache upload/download is needed, and normal workspace cleanup leaves it intact.

The builder enables upstream's own cache interfaces on every Make invocation:

- **Packages:** `SONIC_DPKG_CACHE_METHOD=cache`, with storage under `v1/branches/<branch>/amd64/packages`. SONiC decides whether an entry matches its source, dependency, architecture, Debian environment and platform hashes. VS and Broadcom use the same branch cache directory, but platform-specific entries remain distinct; do not expect every compiled VS package to be reused for Broadcom.
- **Downloads:** `SONIC_VERSION_CACHE_METHOD=cache`, with shared storage under `v1/downloads/amd64`, mounted by upstream as `/vcache`. Supported build hooks can reuse versioned downloads across platforms and runs. This does not cover every network request: the fresh top-level Git fetch, submodule initialization and downloads bypassing upstream hooks may still require network access.

The cache settings do not modify upstream recipes, remove platform checks, or share raw `target/` directories. A cached Docker utility image repairs cache permissions and sets inherited ACLs before building, allowing root build hooks and non-root compilation processes to share lock files and downloads. Cache misses build normally and populate the cache. The first cached build starts cold; earlier runs with caching disabled do not automatically seed it. Older branches such as `202111` provide package caching but lack the newer download cache; the builder detects that and enables only the supported cache. A branch with no native package-cache support requires `cache_mode=off`.

Some upstream download hooks incorrectly skip a URL already using SONiC's mirror when caching is enabled, returning success without creating a file. For the recognized implementation, the builder fixes the early-return condition so only confirmed cache restores can skip the downloader. It adds a hook-content stamp to build-container Dockerfile templates so cached containers cannot retain the faulty hook. This refreshes affected build images once; source/dependency cache keys remain upstream-controlled. `compatibility_fixes` in build metadata identifies the applied fix. `cache_mode=off` bypasses this compatibility change as well as persistent caching.

The builder also detects cached cleanup scripts that purge `rsync` despite downstream images requiring it for final-stage copying. For affected multi-stage builds it retains the package, matching the non-cached cleanup behavior, and stamps runtime base-image templates to invalidate images missing the dependency. This fix is recorded as `cached-rsync-cleanup` in metadata. It does not require installing anything on the runner host.

To choose another disk, set the repository Actions variable **`SONIC_CACHE_DIR`** to an absolute host path owned by `github-runner`, for example `/mnt/nvme/sonic-vs-cache`. The default needs no manual directory setup. For local builds, use the same environment variable or `build --cache-dir /absolute/path`. Paths must be outside the temporary work directory and use only letters, numbers, `/`, `_`, `.` and `-`, as required for safe Make variable forwarding. The cache filesystem must have 300 GiB free before building.

Cache locations and mode are printed in the build log and retained in `cache.json` alongside each platform's Actions log artifact. See the [runner guide](docs/runner.md#persistent-cache-maintenance) for disk usage and reset instructions.

## Downloadable releases

Each branch/build attempt creates a separate release tagged `sonic-<branch>-<run-id>-<attempt>`, attached to the builder commit used by the workflow. Existing published releases are never overwritten and no release is automatically designated Latest.

For the default `both` selection, a complete release contains:

| Output | Purpose |
| --- | --- |
| `docker-sonic-vs.gz` | Docker image archive; load with `docker load -i docker-sonic-vs.gz`. |
| `sonic-broadcom.bin` | Broadcom ONIE installer. |
| `vs-metadata.json`, `broadcom-metadata.json` | Upstream commit, platform, output SHA-256 and size; VS metadata also records each selected registry's image reference and digest. |
| `vs-SHA256SUMS`, `broadcom-SHA256SUMS` | Checksums of the complete downloadable files. |
| `vs-README.txt`, `broadcom-README.txt` | Download, reassembly, verification and usage instructions. |

GitHub requires each release asset to be under 2 GiB. Larger files are uploaded as ordered 1 GiB parts named `*.part0001`, `*.part0002`, etc. Download all parts, reassemble, then check the checksum:

```sh
cat sonic-broadcom.bin.part* > sonic-broadcom.bin
sha256sum -c broadcom-SHA256SUMS
```

Releases stay draft until all selected images and their metadata are uploaded. If a build or upload fails, the partial draft is retained for inspection by repository writers; it is not published as a complete release. Rerunning failed jobs uses a new attempt number and a separate release. A VS image already pushed to a registry remains available even if a later registry push or Broadcom build fails. A `broadcom`-only run does not touch container registries.

## First publication and public pulls

The workflow authenticates with its own `GITHUB_TOKEN` and `packages: write`; no registry secret is required. Publishing with that token associates the package with this repository. GitHub initially creates container packages as private, even for a public repository.

After the first successful push, open [your container packages](https://github.com/ITJamie?tab=packages), select **sonic-vs-builder → Package settings → Change visibility → Public**. Verify the linked repository is `ITJamie/sonic-vs-builder`. This visibility change is a one-time manual GitHub setting; the workflow does not claim to make the package public automatically.

Verify anonymous access using a temporary empty Docker credential directory on a machine with Docker. Replace the digest below with the value from the workflow summary:

```sh
registry_config=$(mktemp -d)
docker --config "$registry_config" pull ghcr.io/itjamie/sonic-vs-builder:202605
docker --config "$registry_config" pull ghcr.io/itjamie/sonic-vs-builder@sha256:REPLACE_WITH_DIGEST
rm -rf "$registry_config"
```

Both commands must succeed without logging in. The digest pull verifies the exact published image even if the branch tag has since moved. [GitHub's container registry documentation](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) explains package access and repository linking.

## Build results and failures

The Actions job summary links to the published release. VS metadata contains the upstream branch, commit SHA, image ID and a `registries` map with each published image's reference, digest and pull command; the top-level image/digest fields continue to identify GHCR. Broadcom metadata records its output checksum and size. Each platform uploads build and publication logs, including on failure, as Actions artifacts with 14-day retention. The images are retained as release downloads; the VS image is also available through the selected registries. Large binaries are not duplicated in Actions artifacts.

A failed build or smoke check skips publication, leaving the existing registry tag untouched. A successful push followed by a metadata or cleanup failure can still have updated the registry tag; inspect the publication log in that case. If a job waits indefinitely, check the runner is online with all four labels: `self-hosted`, `linux`, `x64`, `sonic-vs`.

Disk or prerequisite errors fail before cloning. For memory failures, keep `build_jobs=1` or increase runner memory. For old-branch build failures, inspect the upstream logs before changing builder defaults. A job times out after 24 hours. Failed, cancelled or timed-out jobs attempt to upload logs and remove their owned workspace; abrupt runner termination can leave files behind. See the runner guide for recovery.

## Local tools and checks

The scripts require Python 3.10+ and its standard library. From the repository root:

```console
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
actionlint
python3 scripts/sonic_vs.py prepare --branches 202605,202511 --build-jobs 3 --images both
```

The first three commands are also run in CI. `actionlint` checks workflow expressions and uses ShellCheck for embedded shell when available; the Ubuntu CI runner provides ShellCheck. The final command queries upstream and prints the resolved build matrix. Build, container publication and release publication are separate commands; `python3 scripts/sonic_vs.py --help` and `python3 scripts/releases.py --help` list them. The workflow uses `GITHUB_TOKEN` with `contents: write` for releases and `packages: write` for GHCR; Docker Hub requires the two secrets below. The standalone `publish` command defaults to GHCR-only; pass `--dockerhub true` to enable both.

## Docker Hub publication

Configure repository Actions secrets `DOCKERHUB_USERNAME` (`itjamie`) and `DOCKERHUB_TOKEN` (a Docker Hub access token with write access to `itjamie/docker-sonic-vs`). Do not use your Docker Hub account password. Missing secrets fail preparation before a long source build when Docker Hub publication is selected. The publisher authenticates to both registries before pushing anything, and uses a temporary Docker credential directory that is removed on normal success or errors.

The same smoke-tested image ID is tagged and pushed to:

```text
ghcr.io/itjamie/sonic-vs-builder:<upstream-branch>
docker.io/itjamie/docker-sonic-vs:<upstream-branch>
```

There is no second image build and no `latest` alias. Each successful rebuild updates the selected branch tags. Metadata records each registry digest separately. Both local publication tags are cleaned up, restoring any tags that existed before the job.

Publishing to two registries is not atomic: if GHCR succeeds and Docker Hub subsequently fails, GHCR's tag is updated and the job fails. The successful GHCR publication is retained in the Actions metadata artifact; release publication and the subsequent Broadcom build are skipped. Fix the credentials or registry error and start/rerun a build, or select `publish_dockerhub=false` for GHCR-only publication.

After the first successful Docker Hub publication, verify a pull on a machine without Docker Hub credentials:

```console
docker pull itjamie/docker-sonic-vs:202605
```

The Broadcom installer is distributed through GitHub Releases, not Docker Hub.
