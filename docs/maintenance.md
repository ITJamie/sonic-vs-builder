# Failed-build cleanup and storage retention

The cleanup plan has two layers. The workflow uploads logs/metadata and attempts
to remove its marked workspace after every output, including failures. Scheduled
maintenance handles obsolete build images, bounded Docker build cache, and work
directories left by a crashed runner or interrupted cleanup. Failed compilations
may still produce useful native package archives; keep those for the next retry.

If validation succeeds but publication does not finish, normal cleanup moves the
verified archive into `retained-output/` in the marked workspace and removes the
large source tree. It keeps metadata/logs so the archive can be uploaded again
without compiling it again. The job summary reports this location. These retained
workspaces remain subject to the two-day scheduled workspace policy below.

Builder CLI commands handle both SIGINT and SIGTERM, including when the Actions
shell inherited SIGINT as ignored. Each upstream command uses a separate process
group; cancellation terminates that command's descendants before releasing the
maintenance lock. This avoids an orphaned build blocking the workflow's cleanup
step. It does not signal the runner service's shared process group. Unit tests
exercise a TERM-resistant grandchild and lock release; Docker CI verifies that a
cancelled command leaves no isolated test container running.

## Retention policy

| Resource | Policy |
| --- | --- |
| Active builds/publication | Skip when `Runner.Worker` is present. A shared filesystem lock also serializes maintenance with builder commands. |
| Native package/download caches | Preserve all entries; branch retirement is a separate manual decision. |
| Local SONiC build-image tags | Opt-in `--sonic-images`; retain the newest two per repository and do not remove tags younger than one hour. Images referenced by any container, including stopped containers, are protected. |
| `tmp-sonic-slave-*` tags | Eligible after one hour when unused; they are upstream intermediate tags, not the final reusable base tags. |
| Docker build cache | Opt-in `--builder NAME`; default limits are 80 GiB maximum cache, 20 GiB reserved cache and 350 GiB desired free space. Docker may be unable to meet these targets when images pin shared layers. |
| Abandoned work directories | Remove only `sonic-vs-*` directories with the project ownership marker, older than two days. Symlinked workspaces are skipped. The threshold exceeds the workflow's 24-hour job timeout. |
| Published images, releases, volumes, other image repositories | Preserve. No `docker system prune`, image-wide prune, volume prune or forced image removal. |

`--sonic-images` explicitly opts into managing the local upstream `sonic-slave-*`
and `tmp-sonic-slave-*` namespaces on this dedicated runner, including older
images without project labels. Do not use that option on a host where another
project manages these same namespaces. Only named tags are removed; other tags
on the same image remain. Docker's shared layer accounting means removing a
25 GiB image tag does not necessarily free 25 GiB.

Builder pruning affects the cache of the selected builder, including other
projects if that builder is shared. The supplied service selects `default`
because this runner is dedicated to SONiC. On a shared host, omit `--builder` or
use a builder reserved for this project. Never delete Docker/containerd storage
directories by hand. See [Docker's builder prune reference](https://docs.docker.com/reference/cli/docker/buildx/prune/).

## Install outside the Actions checkout

Use a stable checkout so Actions cannot replace the maintenance script while
checking out an older build commit:

```sh
sudo -u github-runner -H git clone https://github.com/ITJamie/sonic-vs-builder.git /home/github-runner/sonic-vs-maintenance
```

Run the dry run as the runner service account, with Docker access:

```sh
sudo -u github-runner -H python3 /home/github-runner/sonic-vs-maintenance/scripts/maintenance.py \
  --cache-dir /home/github-runner/.cache/sonic-vs-builder \
  --work-root /home/github-runner/actions-runner/_work/_temp \
  --sonic-images --builder default
```

It prints selected tags, workspaces and the builder-prune command. No removal
happens without `--apply`. While a job is active, it prints `SKIP` instead; review
the complete dry-run plan during an idle period. After review, repeat with
`--apply` to execute it. Optional tuning includes `--keep-images 3`,
`--image-min-hours 6`, `--max-cache-gib 100`, or `--min-free-gib 380`.

Always use the same absolute cache directory as the build workflow's
`SONIC_CACHE_DIR`, so both commands share `maintenance.lock`. The script does not
require host sudo access. Root-owned nested-Docker source files are removed by
the existing Docker helper, restricted to the marked workspace. Update the
stable checkout with `git pull --ff-only` while maintenance is idle when you want
new maintenance code.

## Hourly schedule

Review the account, paths and builder in
[`sonic-vs-maintenance.service`](../deploy/sonic-vs-maintenance.service) before
installing it. Change the cache path if `SONIC_CACHE_DIR` points elsewhere.
The timer runs hourly, with up to five minutes of jitter, and skips active jobs.

```sh
sudo cp /home/github-runner/sonic-vs-maintenance/deploy/sonic-vs-maintenance.service /etc/systemd/system/
sudo cp /home/github-runner/sonic-vs-maintenance/deploy/sonic-vs-maintenance.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now sonic-vs-maintenance.timer
systemctl list-timers sonic-vs-maintenance.timer
journalctl -u sonic-vs-maintenance.service --no-pager -n 100
```

An idle manual maintenance run can also use
`sudo systemctl start sonic-vs-maintenance.service`. The timer is supplied but
is not installed or enabled automatically by the build workflow.

## Capacity checks and recovery

### Retry publication from a retained output

Use the retained workspace reported in the job summary, its release tag, and the
exact builder SHA from that workflow run. With GitHub CLI authenticated:

```sh
GH_TOKEN="$(gh auth token)" python3 scripts/releases.py stage \
  --work /absolute/marked/workspace --repository ITJamie/sonic-vs-builder \
  --tag sonic-202605-RUN-ATTEMPT --target BUILDER_COMMIT_SHA
GH_TOKEN="$(gh auth token)" python3 scripts/releases.py finish \
  --repository ITJamie/sonic-vs-builder --tag sonic-202605-RUN-ATTEMPT --images kvm
python3 scripts/sonic_vs.py cleanup --work /absolute/marked/workspace
```

Adjust `--images` to the selected outputs; all selected assets must be uploaded
before finishing. The upload rechecks the archive checksum. If an earlier upload
left conflicting asset names in a draft, use a fresh recovery tag (anchor it with
`scripts/releases.py anchor --repository OWNER/REPO --tag TAG --target SHA`) rather
than overwriting assets. After recovery, normal cleanup removes the workspace.
To deliberately discard an unpublished retained archive instead, use
`scripts/sonic_vs.py cleanup --work /absolute/marked/workspace --discard-output`.

### Free-space guard

The 300 GiB pre-build guard remains enabled. A cleanup run is best effort;
retained images, native caches or unrelated host files can prevent reaching the
350 GiB free-space target. Inspect the journal and these commands if a build
still fails its guard:

```sh
df -h /
docker system df -v
docker buildx du --builder default
du -sh /home/github-runner/.cache/sonic-vs-builder/v1/branches/*/amd64/*
```

If a failed job's normal workspace cleanup reports an error, preserve its log
artifact and inspect the exact marked directory. Scheduled cleanup waits two
days to avoid discarding fresh debugging evidence. For earlier manual recovery,
use the existing `sonic_vs.py cleanup --work /exact/marked/path` command only
after confirming the job is terminal. Neither cleanup path removes native caches.

The regression suite checks dry-run behavior, retention limits, unrelated tags,
container protection, symlink/marker rules, shared-lock exclusion and native-cache
preservation. CI also exercises deletion of isolated test tags with a real Docker
daemon; it never prunes the runner's shared Docker cache as part of testing.
