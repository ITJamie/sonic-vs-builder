# Ubuntu 24.04 build runner

Use a dedicated Linux x86_64 host for this repository, with at least 16 GB RAM and 300 GiB free on the filesystem containing the runner workspace and Docker data directory. An SSD is recommended. The builder checks free space before each platform; accumulated Docker build caches can require additional storage. The default configuration targets our six-core, 48 GB RAM host. KVM support is recommended by upstream for the complete build environment; ensure it is available for build steps that need virtualization. This workflow can build the VS Docker target and the Broadcom ONIE installer.

## Prerequisites

Create a normal non-root account for the runner. Install [Docker Engine using Docker's Ubuntu instructions](https://docs.docker.com/engine/install/ubuntu/), then run these commands as the runner account:

```sh
sudo apt-get update
sudo apt-get install -y git make python3 python3-venv pipx kmod
pipx install jinjanator
sudo usermod -aG docker "$USER"
sudo modprobe overlay
```

Log out and log back in to pick up Docker group membership. Add `$HOME/.local/bin` to the runner service's `PATH` so the `j2` executable installed by pipx is visible. For a dedicated systemd service, a typical explicit `PATH` is `/home/sonic-runner/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`; substitute your actual runner account's home directory.

Allow the service account to load overlay without an interactive password. Find the absolute `modprobe` path with `command -v modprobe`, then use `sudo visudo -f /etc/sudoers.d/sonic-vs-overlay` to add this rule, adjusting both account and path:

```sudoers
sonic-runner ALL=(root) NOPASSWD: /usr/sbin/modprobe overlay
```

Check as the runner account:

```console
git --version
make --version
python3 --version
j2 --version
docker info
sudo -n modprobe overlay
df -h . /var/lib/docker
free -h
```

Use a local Docker Engine, not a remote context or Docker Desktop. Both the runner and Docker must use Linux amd64. The builder does not install or upgrade system dependencies during a build. See [upstream build requirements](https://github.com/sonic-net/sonic-buildimage/blob/202605/README.md) for details.

## Register the runner

In `ITJamie/sonic-vs-builder`, open **Settings → Actions → Runners → New self-hosted runner**, select Linux x64, and follow GitHub's generated download and registration commands. Add the custom label `sonic-vs`; keep the default `self-hosted`, `linux` and `x64` labels. Install and start its service using the generated runner's `svc.sh` instructions. Verify that the service account can execute every prerequisite check above, including `j2`.

Run only one runner service on this dedicated build host. The repository's workflow serializes its jobs, but does not coordinate other repositories, separate runner installations or builds started outside Actions. Keep disk maintenance and manual builds outside active workflow runs.

The source build runs with Docker access and upstream code. Only manual dispatch runs this workflow; pull requests and pushes run validation on GitHub-hosted machines. Keep these triggers separate and do not add untrusted pull-request code to the self-hosted workflow.

## Tuning and maintenance

The default `build_jobs=3` is intended for the dedicated six-core, 48 GB RAM runner. Builds run one platform at a time and remove its source workspace after upload, before starting the next platform. On a smaller 16 GB host, use `build_jobs=1`. Upstream package compilation can need roughly 4–6 GB per job plus overhead, with individual packages sometimes using more. Upstream defaults also control parallelism inside individual packages, so this input is not a strict memory cap. Lower it if memory pressure or swapping appears; increase it only after measuring the actual builds.

Each job creates a uniquely named `sonic-vs-*` directory in the runner's temporary directory, marked with `.sonic-vs-builder`. Credentials use a separate temporary `sonic-vs-auth-*` directory and are removed after publication, including normal publication errors. On ordinary completion, cleanup restores any preexisting local GHCR branch tag, removes newly loaded image IDs when unused, and deletes the work directory. No host-wide prune is run.

Upstream may retain reusable build images and caches. These are not removed automatically because they are not all uniquely attributable to one job. Check `docker system df` and the free space before subsequent builds. Remove specific obsolete images or caches only when you have confirmed no build is active; configure host-specific maintenance outside this workflow.

## Persistent cache maintenance

The builder automatically creates `~/.cache/sonic-vs-builder` as the runner service account. It survives temporary workspace cleanup and is shared by later jobs on this host. A small cached Docker utility image supplies ACL tools; no additional host packages or sudo rules are needed. Before each cached build, it repairs existing cache permissions and sets inherited default ACLs so root hooks and the non-root build user can both write future cache files. Helpers run without networking and mount only the selected cache directory. GitHub-hosted validation jobs test this behavior in a temporary cache without populating the runner cache.

The layout is:

```text
~/.cache/sonic-vs-builder/
  v1/
    branches/<branch>/amd64/packages/  # Native dependency-checked package cache
    downloads/amd64/                  # Native version/download cache
```

Package caches are partitioned by upstream branch and architecture. Upstream's keys additionally distinguish platform, Debian build environment, source and dependencies; leave those checks enabled. Download storage is shared, with upstream's hooks providing their own image/version keys. Raw build directories remain separate and disposable.

To use a different filesystem, prepare a directory for the service account and set repository Actions variable `SONIC_CACHE_DIR` to its absolute host path. The path is passed directly into the build container as a bind mount, so it must exist on the local Docker host. Check the chosen filesystem has at least 300 GiB free, in addition to the workspace and Docker storage checks. Avoid spaces or Make-special characters in its path.

Inspect usage as the runner account:

```sh
du -sh ~/.cache/sonic-vs-builder/v1/branches/* ~/.cache/sonic-vs-builder/v1/downloads
df -h ~/.cache/sonic-vs-builder
docker system df
```

Substitute your configured cache root when using `SONIC_CACHE_DIR`. The cache has no automatic eviction and can grow with new branches and source revisions. Remove obsolete branch package directories only after stopping the runner and confirming no builds are active. To reset a problematic cache, rename the affected branch directory (or the complete `v1` directory), then restart the runner; the next build creates a fresh cache. Remove the renamed directory once you no longer need it. Normal job cleanup does not remove persistent caches.

For troubleshooting without clearing data, dispatch with `cache_mode=off`. Both native cache methods are disabled for that run and the persistent cache is preserved. A cache from an older build cannot make an unsupported source branch buildable; upstream build dependencies still need to be available.

After a host crash or forced process termination, stop the runner and verify no build containers are active. Inspect leftover work directories and preserve any logs you need, then invoke cleanup on each verified project-owned directory:

```console
python3 scripts/sonic_vs.py cleanup --work /absolute/path/to/runner/_work/_temp/sonic-vs-EXAMPLE
```

Cleanup refuses directories without the ownership marker. If upstream leaves root-owned Docker data in `source/fsroot.docker.*`, cleanup uses the Docker utility image with only that marked workspace mounted to remove its `source` directory. Persistent caches are not mounted into this cleanup helper. If that fails, inspect the exact directory and remove those files as an administrator. Also remove abandoned `sonic-vs-auth-*` credential directories after confirming they are unused. Do not print their contents. Restart the runner after recovery.
