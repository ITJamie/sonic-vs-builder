# Ubuntu 24.04 build runner

Use a dedicated Linux x86_64 host for this repository, with at least 16 GB RAM and 300 GiB free on the filesystem containing the runner workspace and Docker data directory. An SSD is recommended. The builder checks free space before each version; accumulated Docker build caches can require additional storage. KVM is recommended by upstream for its general build environment; this workflow builds only the Docker VS target, not VM/ONIE images.

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

Start with `build_jobs=1`. Increase it only with enough memory for upstream's package compilation: roughly 4–6 GB per job plus overhead, with package-level compilation sometimes using more. A 32 GB or larger host is a better starting point for four jobs. Upstream defaults also control parallelism inside individual packages, so this input is not a strict memory cap.

Each job creates a uniquely named `sonic-vs-*` directory in the runner's temporary directory, marked with `.sonic-vs-builder`. Credentials use a separate temporary `sonic-vs-auth-*` directory and are removed after publication, including normal publication errors. On ordinary completion, cleanup restores any preexisting local GHCR branch tag, removes newly loaded image IDs when unused, and deletes the work directory. No host-wide prune is run.

Upstream may retain reusable build images and caches. These are not removed automatically because they are not all uniquely attributable to one job. Check `docker system df` and the free space before subsequent builds. Remove specific obsolete images or caches only when you have confirmed no build is active; configure host-specific maintenance outside this workflow.

After a host crash or forced process termination, stop the runner and verify no build containers are active. Inspect leftover work directories and preserve any logs you need, then invoke cleanup on each verified project-owned directory:

```console
python3 scripts/sonic_vs.py cleanup --work /absolute/path/to/runner/_work/_temp/sonic-vs-EXAMPLE
```

Cleanup refuses directories without the ownership marker. If upstream left root-owned files, inspect the exact directory and remove those files as an administrator. Also remove abandoned `sonic-vs-auth-*` credential directories after confirming they are unused. Do not print their contents. Restart the runner after recovery.
