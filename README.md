# SONiC VS image builder

Build the upstream [SONiC virtual-switch Docker image](https://github.com/sonic-net/sonic-buildimage/tree/202605/platform/vs) from a branch you choose, and publish it to this repository's GitHub Container Registry package:

```console
docker pull ghcr.io/itjamie/sonic-vs-builder:202605
```

Images are Linux amd64, built from `sonic-net/sonic-buildimage` without patches. The image tag is exactly the upstream branch name. Rebuilding a branch updates its tag; use the digest from the workflow summary when you need an immutable reference. There is no additional `latest` alias.

## Set up a runner

Follow [the Ubuntu runner guide](docs/runner.md). Builds require a dedicated self-hosted runner labeled `sonic-vs`; ordinary GitHub-hosted runners only validate this builder's code. Start with 16 GB RAM, 300 GiB free on both the workspace and Docker filesystems, and one build job. If those paths share a filesystem, the free-space checks refer to the same storage, not two separate 300 GiB allocations.

## Trigger a build

Open **Actions → Build SONiC VS → Run workflow** on `main`. Enter a branch, such as `202605`, or a comma-separated list, such as `202605,202511`. `build_jobs` controls upstream compilation parallelism and defaults to `1`.

Using the GitHub CLI:

```console
gh workflow run build.yml --repo ITJamie/sonic-vs-builder -f branches=202605 -f build_jobs=1
gh workflow run build.yml --repo ITJamie/sonic-vs-builder -f branches=202605,202511 -f build_jobs=1
gh run list --repo ITJamie/sonic-vs-builder --workflow build.yml
```

All names are validated and resolved before any self-hosted job starts. Whitespace is trimmed and duplicate branches are removed. Empty entries, nonexistent branches, and branch names that cannot be represented exactly as Docker tags (for example `feature/test`) fail validation. Tags and arbitrary commit inputs are not accepted. Every build uses the resolved branch commit and its pinned submodules, even if the branch advances while waiting for the runner.

Versions run sequentially and independently: failure of one version does not cancel the others. Build workflows share a concurrency group with cancellation disabled, preventing two builds from sharing the host. GitHub concurrency retains only one pending run, so submitting more runs while one is already pending can replace that pending run; submit several versions together in one branch list.

Each version runs `make init`, `make configure PLATFORM=vs PLATFORM_ARCH=amd64`, and `make SONIC_BUILD_JOBS=<jobs> target/docker-sonic-vs.gz` with `UNATTENDED=y`. Upstream tests and build defaults remain enabled. Other historical branches may need a different upstream environment; compatibility is established by successful builds, not guaranteed for every release.

The archive is loaded without importing upstream image tags. Before publishing, the builder checks Linux amd64, reads `/etc/sonic/sonic_version.yml`, and runs `sonic-cfggen --help` in a container with no network access. This checks image usability, not a complete virtual-switch topology.

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

The Actions job summary and `metadata.json` artifact contain the upstream branch, commit SHA, image ID, registry digest and pull command. Each version uploads build and publication logs, including on failure, with 14-day retention. The multi-gigabyte image archive is not uploaded as an Actions artifact; GHCR holds the image.

A failed build or smoke check skips publication, leaving the existing registry tag untouched. A successful push followed by a metadata or cleanup failure can still have updated the registry tag; inspect the publication log in that case. If a job waits indefinitely, check the runner is online with all four labels: `self-hosted`, `linux`, `x64`, `sonic-vs`.

Disk or prerequisite errors fail before cloning. For memory failures, keep `build_jobs=1` or increase runner memory. For old-branch build failures, inspect the upstream logs before changing builder defaults. A job times out after 24 hours. Failed, cancelled or timed-out jobs attempt to upload logs and remove their owned workspace; abrupt runner termination can leave files behind. See the runner guide for recovery.

## Local tools and checks

The scripts require Python 3.10+ and its standard library. From the repository root:

```console
python3 -m unittest discover -s tests -v
python3 -m compileall -q scripts tests
actionlint
python3 scripts/sonic_vs.py prepare --branches 202605,202511 --build-jobs 1
```

The first three commands are also run in CI. `actionlint` checks workflow expressions and uses ShellCheck for embedded shell when available; the Ubuntu CI runner provides ShellCheck. The final command queries upstream and prints the resolved build matrix. Build and publication are separate commands; `python3 scripts/sonic_vs.py --help` lists them.

## Future Docker Hub publication

The intended Docker Hub destination is `itjamie/docker-sonic-vs:<upstream-branch>`. Publication is separate from compilation so a future publisher can tag and push the same validated image to both registries without rebuilding. That addition will need a Docker Hub access token with write access and the username `itjamie`, stored as Actions secrets. This version does not request Docker Hub credentials or push there.
