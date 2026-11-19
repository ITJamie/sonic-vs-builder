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
  and restart unexpected monitor failures. Preserve the original startup speeds,
  native AS5835 and PDDF mode
  selection; correct AS7726 airflow comparison without changing thresholds.
- `202605/0010-order-edgecore-idt-before-pddf.patch`: order AS7326 and AS7726
  IDT clock initialization before PDDF binds their muxes, following upstream
  [PR #30498](https://github.com/sonic-net/sonic-buildimage/pull/30498).

The former `0011` voltage definitions and `0012` narrow PSU fan exclusions are
folded into the final file patches `0023` and `0020`. Their behavior is retained;
PSU fan status, direction, measured speed and power-good checks remain enabled.

- `202605/0013-decode-as7326-fsf019-airflow-label.patch`: map the observed
  FSF019-611 `F2B*` value to `EXHAUST`, consistent with Edgecore's model-based
  decoder. Other airflow labels and direction checks remain intact.

- `202605/0014-complete-as7726-psu-cpu-monitoring.patch`: complete AS7726
  PMBus voltage limits, hardware temperature warning limits (`OT_WARN_LIMIT`,
  register `0x51`), the observed FSF019 `F2B*` airflow label, and CPU package/core
  monitoring. Only unsupported PSU fan target comparisons are excluded; PSU
  temperature monitoring remains enabled. The health configuration in `0006`
  includes the final exclusions to keep reapplication detection reliable.
- `202605/0015-complete-as5835-psu-cpu-monitoring.patch`: correct manufacturer
  voltage-limit decoding in the shared `ym2651y` driver, retain chassis fan PWM
  targets while explicitly reporting unsupported PSU fan targets, and add CPU
  thermals using kernel labels/thresholds. Only the two PSU target comparisons
  are excluded from health checking.

The follow-ups ported from the current `accton-fixes` branch are:

| Patch | Change |
| --- | --- |
| `0016` | Enable sensormond for AS7326/AS7726 output sensors. |
| `0017` | AS7326 integer PWM reporting; preserve the 202605 lambda format and original targets. |
| `0018` | Retry missing PDDF sysfs paths while retaining positive caching. |
| `0019` | AS7326 CPU package/core readings and kernel thresholds. |
| `0020` | AS7326 hardware PSU temperature limits and enabled temperature health checks. |
| `0021` | Read-only BIOS components on AS7326/AS7726. |
| `0022` | AS5835 native PSU output voltage/current sensors and sensormond. |
| `0023` | AS7326/AS7726 input voltage/current/power API and input voltage/current sensors. |

Files modified by several fixes are consolidated into their final patch so
apply/reapply recognition works on completed source trees. Earlier watchdog,
health/LED, voltage-limit and CPU changes to those files are included there.
In particular, `0023` contains the complete PDDF JSON, chassis and PSU sensor
integration; `0022` contains the complete native AS5835 chassis changes.
AS5835 input telemetry remains unsupported; manufacturer ratings are not alarms.
Input power is exposed through the existing PSU API, without daemon changes.

The series is reconciled with `accton-fixes` at
`5b9c212e474020111a9827a77c10665e0a16f782` and watchdog helper
`1c8e439f9f14ef04dd05fa62d79e635c215d24d6`. It preserves 202605 dependency pins
and fan conversion syntax. The experimental master sairedis rollback is excluded.
The parity fixture records 50 resulting implementation files, checks coverage of
all reviewed platform changes, and documents the branch adaptations. Integration
applies/reapplies all 21 patches, checks original fan startup code, and runs
85 hardware-free regressions against the actual patched source.

The four-family support matrix, exact scope, source pins and required hardware
checks are in [Edgecore platform support](../docs/edgecore-platform-support.md).
The CI regression applies/reapplies all patches to pinned upstream and executes
the actual patched methods with simulated sensors and watchdog devices. These
software checks do not establish hardware validation for untested models.
