# SONiC image builder

Build the upstream [SONiC virtual-switch Docker image](https://github.com/sonic-net/sonic-buildimage/tree/202605/platform/vs) and [Broadcom ONIE installer](https://github.com/sonic-net/sonic-buildimage/tree/202605/platform/broadcom) from a branch you choose. Download both from [GitHub Releases](https://github.com/ITJamie/sonic-vs-builder/releases), and pull the VS container from GitHub Container Registry:

```console
docker pull ghcr.io/itjamie/sonic-vs-builder:202605
```

Images are Linux amd64, built from `sonic-net/sonic-buildimage` without patches. The VS container tag is exactly the upstream branch name. Rebuilding a branch updates its container tag; use the digest from the workflow summary when you need an immutable reference. There is no additional `latest` alias. Downloadable releases retain every successful build separately.

## Set up a runner

Follow [the Ubuntu runner guide](docs/runner.md). Builds require a dedicated self-hosted runner labeled `sonic-vs`; ordinary GitHub-hosted runners only validate this builder's code. The default is tuned to our dedicated six-core, 48 GB RAM runner: three build jobs, with VS and Broadcom built sequentially. Hosts with 16 GB RAM should select one build job. Keep at least 300 GiB free on both the workspace and Docker filesystems. If those paths share a filesystem, the checks refer to the same storage, not two separate 300 GiB allocations.

## Trigger a build

Open **Actions → Build SONiC images → Run workflow** on `main`. Enter a branch, such as `202605`, or a comma-separated list, such as `202605,202511`. Choose `images=both` (default), `vs`, or `broadcom`. `build_jobs` controls upstream compilation parallelism per platform and defaults to `3`.

Using the GitHub CLI:

```console
gh workflow run build.yml --repo ITJamie/sonic-vs-builder -f branches=202605 -f images=both -f build_jobs=3
gh workflow run build.yml --repo ITJamie/sonic-vs-builder -f branches=202605,202511 -f images=vs -f build_jobs=3
gh run list --repo ITJamie/sonic-vs-builder --workflow build.yml
```

All names are validated and resolved before any self-hosted job starts. Whitespace is trimmed and duplicate branches are removed. Empty entries, nonexistent branches, and branch names that cannot be represented exactly as Docker tags (for example `feature/test`) fail validation. Tags and arbitrary commit inputs are not accepted. Every build uses the resolved branch commit and its pinned submodules, even if the branch advances while waiting for the runner.

Versions run sequentially and independently: failure of one version does not cancel the others. Within a version, VS builds first, its output is uploaded, and its workspace is removed before Broadcom begins. Failure of an earlier step stops the remaining steps for that version. Build workflows share a concurrency group with cancellation disabled, preventing two builds from sharing the host. GitHub concurrency retains only one pending run, so submitting more runs while one is already pending can replace that pending run; submit several versions together in one branch list.

Each platform uses a fresh checkout of the same resolved upstream commit, then runs `make init`, `make configure PLATFORM=<vs|broadcom> PLATFORM_ARCH=amd64`, and `make SONIC_BUILD_JOBS=<jobs> <target>` with `UNATTENDED=y`. Targets are `target/docker-sonic-vs.gz` and `target/sonic-broadcom.bin`. Upstream tests and defaults remain enabled. The Broadcom target uses upstream's dependency selection, which can also build components for related Broadcom variants; this workflow uploads only the requested installer. Other historical branches may need a different upstream environment; compatibility is established by successful builds, not guaranteed for every release.

The VS archive is loaded without importing upstream image tags. Before publishing, the builder checks Linux amd64, reads `/etc/sonic/sonic_version.yml`, and runs `sonic-cfggen --help` in a container with no network access. This checks image usability, not a complete virtual-switch topology. The Broadcom output is checked for an ONIE shell-installer header and hashed; it is not boot-tested on switch hardware. Use it only with supported Broadcom platforms.

## Downloadable releases

Each branch/build attempt creates a separate release tagged `sonic-<branch>-<run-id>-<attempt>`, attached to the builder commit used by the workflow. Existing published releases are never overwritten and no release is automatically designated Latest.

For the default `both` selection, a complete release contains:

| Output | Purpose |
| --- | --- |
| `docker-sonic-vs.gz` | Docker image archive; load with `docker load -i docker-sonic-vs.gz`. |
| `sonic-broadcom.bin` | Broadcom ONIE installer. |
| `vs-metadata.json`, `broadcom-metadata.json` | Upstream commit, platform, output SHA-256 and size; VS metadata also records its GHCR digest. |
| `vs-SHA256SUMS`, `broadcom-SHA256SUMS` | Checksums of the complete downloadable files. |
| `vs-README.txt`, `broadcom-README.txt` | Download, reassembly, verification and usage instructions. |

GitHub requires each release asset to be under 2 GiB. Larger files are uploaded as ordered 1 GiB parts named `*.part0001`, `*.part0002`, etc. Download all parts, reassemble, then check the checksum:

```sh
cat sonic-broadcom.bin.part* > sonic-broadcom.bin
sha256sum -c broadcom-SHA256SUMS
```

Releases stay draft until all selected images and their metadata are uploaded. If a build or upload fails, the partial draft is retained for inspection by repository writers; it is not published as a complete release. Rerunning failed jobs uses a new attempt number and a separate release. A VS image already pushed to GHCR remains available even if the subsequent Broadcom build fails. A `broadcom`-only run does not touch GHCR.

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

The Actions job summary links to the published release. VS metadata contains the upstream branch, commit SHA, image ID, registry digest and pull command; Broadcom metadata records its output checksum and size. Each platform uploads build and publication logs, including on failure, as Actions artifacts with 14-day retention. The images are retained as release downloads; the VS image is also available through GHCR. Large binaries are not duplicated in Actions artifacts.

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

The first three commands are also run in CI. `actionlint` checks workflow expressions and uses ShellCheck for embedded shell when available; the Ubuntu CI runner provides ShellCheck. The final command queries upstream and prints the resolved build matrix. Build, GHCR publication and release publication are separate commands; `python3 scripts/sonic_vs.py --help` and `python3 scripts/releases.py --help` list them. The workflow uses `GITHUB_TOKEN` with `contents: write` for releases and `packages: write` for GHCR; no additional secrets are needed.

## Future Docker Hub publication

The intended Docker Hub destination is `itjamie/docker-sonic-vs:<upstream-branch>`. Publication is separate from compilation so a future publisher can tag and push the same validated image to both registries without rebuilding. That addition will need a Docker Hub access token with write access and the username `itjamie`, stored as Actions secrets. This version does not request Docker Hub credentials or push there.
