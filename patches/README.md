# Branch patches

Put unified Git patches in `patches/<upstream-branch>/*.patch`. The directory
matches the exact workflow `branches` entry, not this builder's Git branch.
Use numbered filenames such as `0001-fix-description.patch`; files apply in
lexicographic order. Branches without patch files receive no branch patches.

VS Docker, KVM and Broadcom builds apply the selected files after `make init` initializes
the pinned submodules, and before configuration/building. Patches apply even with
`cache_mode=off`. Paths are relative to the sonic-buildimage checkout; use
`a/...` and `b/...` paths as emitted by `git diff`. To patch a file within a
submodule, include its full path from the sonic-buildimage root in the diff.
Patches requiring changes before `make init` are not supported by this mechanism.

Each file is checked with `git apply --check` before application. A reverse check
recognizes an already-present fix. Any other mismatch stops the build before
configuration, image validation or publication; review and update/remove the
patch when upstream changes. There is no fuzzy fallback or silent skipping of
failed patches. Avoid overlapping patches that undo or replace earlier patches;
consolidate them so reverse checks can recognize the final upstream state.

The builder logs each patch and retains its relative path, SHA-256 and status in
`patches.json`, including failure status. Successful image/release metadata has
the same `patches` list. A digest of the selected patch identities is added to
build/runtime Dockerfile templates, refreshing affected cached images when the
patch set changes. Native SONiC dependency checks still control package reuse;
the patch mechanism does not erase persistent caches.

When Docker layer caching is enabled on a supported branch, a separate early-stage
`ARG` also hashes patches, constraints and build hooks. This invalidates installation
layers when fixes change; the comment stamp alone only changes the completed image
tag and is insufficient for this purpose.

For each patch, explain the reason in its header, link the upstream issue/PR,
record a tested upstream commit, and specify when it should be removed. Keep
changes narrow and preserve upstream tests. Add a regression test when fixing
build behavior. Existing cache-hook compatibility helpers remain separate and
are recorded under `compatibility_fixes`.

## Current patches

- `202505/0001-skip-unused-legacy-builders.patch`: use upstream's `NOBUSTER=1`
  and `NOBULLSEYE=1` defaults for our three selected image targets. This avoids
  eagerly preparing unused legacy builders with missing security packages.
  Bookworm stays enabled; optional PTF, SDK and management test images are outside
  these outputs. Explicit `NOBUSTER=0`/`NOBULLSEYE=0` restore each environment.
  Tested against
  `ff3c0c9f686a1d0dbddbe813b233b3b7a51bd8e2`; CI exercises the actual upstream
  Makefile's configure and three target routes, preserving Bookworm and explicit
  legacy overrides. It evaluates the upstream Docker dependency graphs and checks
  that none of our selected outputs requires a Buster or Bullseye container.
- `202505/0002-backport-sairedis-optimization-warning-fix.patch`: backport the
  Python 3 extension compiler fix from merged [sairedis #2109](https://github.com/sonic-net/sonic-sairedis/pull/2109).
  GCC's optional optimization limit diagnostic remains visible but is nonfatal
  for this target. Other warnings retain `-Werror`, and no tests or source
  bindings are removed. CI reproduces the failure with GCC 12 and checks that the
  patched target flags compile while unrelated warnings still fail the build.
- `202505/0003-prepare-cached-wheel-python-dependencies.patch`: prepare the
  upstream-pinned `pexpect` and `pycparser` copies in the Bookworm builder so
  cached wheel installation works without a prior user-site dependency install.
  Only these two packages use `--ignore-installed --no-deps`; their Debian-owned
  copies stay intact. Subsequent wheel installs retain dependency resolution.
  CI reproduces both distutils uninstall failures, verifies pinned imports and
  `pip check`, and checks that Debian's files are unchanged.
- `202505/0004-restore-cached-kvm-platform-mapping.patch`: regenerate the
  virtual platform ASIC mapping before assembling the installer device list.
  Restoring `sonic-device-data` from cache skips the package recipe's source-tree
  side effect. The patch preserves upstream VS/VPP selection and the installer's
  ASIC compatibility check. CI reproduces the missing mapping with the pinned
  upstream function and verifies VS/VPP acceptance and wrong-ASIC rejection.
- `202605/0001-fix-blessed-wcwidth-constraint.patch`: change the Bookworm build
  constraint from `wcwidth==0.7.0` to `wcwidth==0.9.2`, satisfying the pinned
  `blessed==1.52.0` requirement. Tracks
  [sonic-buildimage #30604](https://github.com/sonic-net/sonic-buildimage/issues/30604).
  Tested against `8076a44fa579fde75f73299f03e7f3cac298d9e3`; CI reproduces
  the original resolver failure and verifies installation, `pip check`, and
  imports/basic execution under Linux amd64 Python 3.11 (Bookworm).
- `202605/0002-fix-vs-pycparser-package-ownership.patch`: remove the Debian-owned
  `python3-pycparser` package immediately before VS runtime wheel installation,
  allowing pip to install upstream's pinned `pycparser==3.0`. Tracks
  [sonic-buildimage #30609](https://github.com/sonic-net/sonic-buildimage/issues/30609).
  The regression test reproduces the distutils uninstall error and verifies the
  package removal followed by pip installation. No runtime version pins change.
- `202605/0003-preserve-groff-build-inputs.patch`: retain groff device files and
  macros in build containers so Trixie's Bash package can compile its PDF docs.
  Runtime image cleanup remains enabled. CI reproduces the original failure and
  checks PostScript/PDF generation under fakeroot after the patched cleanup.
- `202605/0004-increase-as7326-sdk-stable-size.patch`: increase AS7326-56X
  `stable_size` to 128 MiB (`0x8000000`) and select the SDK-provided Cancun
  `default/` firmware path. The older 88 MiB workaround from
  [PR #22641](https://github.com/sonic-net/sonic-buildimage/pull/22641), linked from
  [issue #21295](https://github.com/sonic-net/sonic-buildimage/issues/21295), still
  exhausted scache with SDK 6.5.35-SP1. Direct SDK initialization and normal
  syncd/SWSS startup succeeded on our BCM56873 test switch at 128 MiB, exposing
  all 56 front-panel ports. Forwarding and reboot validation remain outstanding.
  Patch integration checks application, both exact configuration changes and
  reapplication. See [the investigation](../docs/broadcom-sai-investigation.md).
- `202605/0005-enable-as7326-edgecore-vxlan-settings.patch`: add the nine
  VXLAN/RIOT SDK settings from Edgecore's AS7326 configuration at
  `d39662e996c0e8748c9b96ad88dc5f5f524e8048`. This includes its exact overlay
  allocations (4096 interfaces, 32768 egress entries and 16384 resilient-hash
  entries), FlexFlow/tunnel/RIOT selection, hierarchical ECMP, split-horizon
  configuration and host-as-route policy. It does not enable the broader
  upstream common profile or change aliases, lanes, MMU or other vendor settings.
  Integration checks exact changes and reapplication; VXLAN forwarding and
  resource headroom on SAI 15.2 still require hardware testing.
- `202605/0006-fix-edgecore-pddf-health-leds.patch`: restore the health/status LED
  interface for PDDF AS7326, AS7726 and AS7816, with matching driver color labels
  and AS7726 health config. Preserve AS5835's native implementation.
- `202605/0007-port-edgecore-sdk-settings.patch`: extend stable-cache and exact
  vendor VXLAN settings to AS5835 and AS7726, preserving their distinct resource
  allocations. AS7726 also uses the SDK's Cancun `default/` path. AS7816's
  Tomahawk2 profile stays intact.
- `202605/0008-fix-edgecore-linux-watchdog.patch`: add/fix hardware-watchdog
  adapters for all four families, using a common Linux implementation whose
  queries do not open/arm hardware. Fix the PDDF missing `syslog` import.
- `202605/0009-recover-edgecore-thermal-monitor.patch`: use full fan speed while
  sensor readings are unavailable, retry and recover the existing thermal policy,
  and restart unexpected monitor failures. Preserve native AS5835 and PDDF mode
  selection; correct AS7726 airflow comparison without changing thresholds.
- `202605/0010-order-edgecore-idt-before-pddf.patch`: order AS7326 and AS7726
  IDT clock initialization before PDDF binds their muxes, following upstream
  [PR #30498](https://github.com/sonic-net/sonic-buildimage/pull/30498).

- `202605/0011-read-as7326-psu-voltage-limits.patch`: expose the two PSU output
  voltage limits using Edgecore's PDDF register definitions (`0xa4`/`0xa5`).
  Hardware-tested with two FSF019-611 PSUs: both returned 11.390–12.593 V after
  PMBus device reload and `pmon` restart; voltage health errors cleared.
  No limits are hardcoded and PSU fan target reporting remains unresolved.
- `202605/0012-skip-as7326-unsupported-psu-fan-target-checks.patch`: exclude
  only `PSU1_FAN1.speed` and `PSU2_FAN1.speed` target comparisons. Retain
  fan status, direction, measured speed and PSU voltage/power-good checks.
- `202605/0013-decode-as7326-fsf019-airflow-label.patch`: map the observed
  FSF019-611 `F2B*` value to `EXHAUST`, consistent with Edgecore's model-based
  decoder. Other airflow labels and direction checks remain intact. With these
  two patches and `0011`, the test switch reports Services/Hardware OK and a
  green status LED. No image build or reboot test has been performed yet.

The four-family support matrix, exact scope, source pins and required hardware
checks are in [Edgecore platform support](../docs/edgecore-platform-support.md).
The CI regression applies/reapplies all patches to pinned upstream and executes
the actual patched methods with simulated sensors and watchdog devices. These
software checks do not establish hardware validation for untested models.
