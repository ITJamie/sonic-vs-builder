# Broadcom SAI versions and AS7326 initialization investigation

Recorded: 2026-11-12. Status: initialization failure diagnosed and corrected on
the test switch; build patch updated, broader validation outstanding.

## Resolution found by direct SDK initialization

Later on 2026-11-12, an isolated diagnostic container using the installed SAI
library exposed the error hidden by the generic `init bcm` failure:

```text
_bcm_esw_scache_ptr_get: Scache out of space.max=92274688 bytes,
used=92082660 bytes, alloc_size=1361924 bytes
_bcm_modules_init: bcm_init failed in extender (No resources for operation)
INIT: Unable to initialize BCM driver on unit 0: No resources for operation
```

The older workaround allocates 88 MiB (`0x5800000`), which is insufficient for
this SDK/configuration. A temporary configuration allocating **128 MiB
(`stable_size=0x8000000`)** passed SDK initialization and SAI switch creation
(`create_switch result: 0`). The SDK listed all 48 x 25G and 8 x 100G ports.
This is a tested allocation, not a measurement of the minimum required size.

The diagnostic probe used the installed library's SAI entry points and SDK
shell exports, with switching services stopped. An existing host device with
registered major 126 was mapped to the SDK's expected `/dev/linux-user-bde`
inside the temporary container; no host device nodes were created. The final
probe matched syncd's 512 MiB shared-memory allocation and supplied the switch
MAC address. Earlier probe-only missing-device, shared-memory and missing-MAC
failures must not be confused with the original SONiC failure.

Applied the 128 MiB setting to the test switch's platform file, retaining the
previously tested firmware `default/` path, and started normal syncd/SWSS.
Verification confirmed both services active, orchagent and portsyncd running
for over a minute, 56 `PORT_TABLE:Ethernet*` entries, and all 56 front-panel
ports visible in `show interfaces status`. No reboot was performed for this
successful test. Persistence across reboot and actual forwarding/VXLAN/CoPP
remain unverified. The firmware-path change was not independently removed to
establish whether it is needed alongside the larger allocation.

Artifacts retained on the switch:

- Prior configuration: `/host/as7326-sdk-before-128m-20261112.config.bcm`.
- SAI debug output: `/host/as7326-sdk-diag-20261112.log`.
- Temporary probe/configuration files: `/tmp/sonic-sdk-probe.py` and
  `/tmp/sonic-sdk-diag.config.bcm`.

Watchdog control and two AS7326 platform services still reported failed after
the ASIC fix; this result does not establish overall platform health.

The build patch now sets 128 MiB and the SDK-provided firmware `default/` path.
Validate a fresh image before treating future build outputs as corrected.
The historical comparison and unsuccessful tests below are retained as context;
the older-image tests are now optional follow-up rather than the primary fix.

## Edgecore VXLAN settings follow-up

Read-only checks confirmed the running AS7326 with SAI 15.2.1.0.0.0.50.1 lacks
the `common_config_support` opt-in and does not load the shared Trident3 profile.
The SDK's loaded configuration had none of Edgecore's nine VXLAN/RIOT settings.
It advertised legacy VXLAN/EVPN capabilities; this observation does not establish
end-to-end VXLAN functionality or prove every explicit setting is mandatory.

At the user's request, the branch patch
`202605/0005-enable-as7326-edgecore-vxlan-settings.patch` now adds those exact
nine settings from Edgecore commit `d39662e996c0e8748c9b96ad88dc5f5f524e8048`.
The overlay interface allocation is 4096, matching Edgecore rather than the
shared TD3 profile's 8192. The patch preserves unrelated upstream settings,
port mappings and aliases and does not activate the complete common profile.
No VXLAN settings were applied to the running switch during this follow-up.
Test initialization, stable-cache headroom and L2/L3 VXLAN forwarding with the
new profile before treating it as hardware-validated.

## Scope and version comparison

The affected switch is `x86_64-accton_as7326_56x-r0`, HwSKU
`Accton-AS7326-56X`, with a Broadcom BCM56873_A0 (Trident3-X7) ASIC.
It uses the XGS SAI package, rather than the DNX package.

The following values were read from upstream release branches on 2026-11-12.
They are branch snapshots, not necessarily the versions shipped on each release's
original release date. Branches continue to receive updates; recheck the source
and record immutable commits before using this table to plan another build.

| SONiC branch | Broadcom XGS SAI package | syncd container base | Integration change |
| --- | --- | --- | --- |
| 202111 | [6.1.0.3](https://github.com/sonic-net/sonic-buildimage/blob/202111/platform/broadcom/sai.mk) | [Debian 10, Buster](https://github.com/sonic-net/sonic-buildimage/blob/202111/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | Older SAI 6.1 generation. |
| 202211 | [8.4.0.2](https://github.com/sonic-net/sonic-buildimage/blob/202211/platform/broadcom/sai.mk) | [Debian 11, Bullseye](https://github.com/sonic-net/sonic-buildimage/blob/202211/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | Separate XGS and DNX version selections; newer container base. |
| 202311 | [10.1.42.1](https://github.com/sonic-net/sonic-buildimage/blob/202311/platform/broadcom/sai.mk) | [Debian 11, Bullseye](https://github.com/sonic-net/sonic-buildimage/blob/202311/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | SAI advances to 10.1 while syncd remains on Bullseye. |
| 202405 | [11.2.30.5](https://github.com/sonic-net/sonic-buildimage/blob/202405/platform/broadcom/sai.mk) | [Debian 11, Bullseye](https://github.com/sonic-net/sonic-buildimage/blob/202405/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | SAI 11.2 generation, implicated in historical AS7326 regression reports. |
| 202505 | [13.2.1.36](https://github.com/sonic-net/sonic-buildimage/blob/202505/platform/broadcom/sai.mk) | [Debian 12, Bookworm](https://github.com/sonic-net/sonic-buildimage/blob/202505/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | Both SAI generation and syncd userspace change. |
| 202511 | [14.3.0.0.0.0.41.1](https://github.com/sonic-net/sonic-buildimage/blob/202511/platform/broadcom/sai-xgs.mk) | [Debian 12, Bookworm](https://github.com/sonic-net/sonic-buildimage/blob/202511/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | SAI advances to 14.3; XGS definitions are in a separate makefile. |
| 202605 | [15.2.1.0.0.0.50.1](https://github.com/sonic-net/sonic-buildimage/blob/202605/platform/broadcom/sai-xgs.mk) | [Debian 13, Trixie](https://github.com/sonic-net/sonic-buildimage/blob/202605/platform/broadcom/docker-syncd-brcm/Dockerfile.j2) | Both SAI generation and syncd userspace change again. |

This describes integration/version changes, not a complete Broadcom feature or
bug-fix changelog. It describes the syncd container userspace, which must be
distinguished from the SONiC host OS, host kernel, and build container versions.

The container dependency chain is:

```text
docker-syncd-brcm -> docker-config-engine-<release>
                 -> docker-base-<release> -> Debian
```

For example, see the 202605 [config-engine Dockerfile](https://github.com/sonic-net/sonic-buildimage/blob/202605/dockers/docker-config-engine-trixie/Dockerfile.j2)
and [base Dockerfile](https://github.com/sonic-net/sonic-buildimage/blob/202605/dockers/docker-base-trixie/Dockerfile.j2).

## Where the SAI and SDK components come from

The SAI makefiles download prebuilt `libsaibcm_<version>_amd64.deb` packages from
SONiC's package infrastructure. A Broadcom identifier such as `SAI_13.2.1_GA`
selects a release directory; this is not a Git checkout of the complete vendor
SAI implementation. Current URL definitions are linked in the table above.

The separate [sonic-net/saibcm-modules repository](https://github.com/sonic-net/saibcm-modules)
contains Broadcom SDK GPL kernel modules. The 202605
[submodule mapping](https://github.com/sonic-net/sonic-buildimage/blob/202605/.gitmodules)
selects `sdk-6.5.35-xgs` for the XGS modules. Kernel module package version numbers
must not be treated as the embedded userspace SDK version without verification.

The installed failing image reports three distinct versions:

| Component | Observed version |
| --- | --- |
| Broadcom SAI implementation | 15.2.1.0.0.0.50.1 |
| OCP SAI API | 1.18.1 |
| Broadcom SDK | sdk-6.5.35-SP1 |

Older userspace SDK versions were not independently established during this
comparison. Do not infer them from the SAI package version alone.

## Original failure and earlier tests

The installed 202605 image was built from upstream commit
`de9703231aa99a5173f37825a1b120aa602f1a26`, with version
`HEAD.0-dirty-20261110.174833`. Its host reports Debian 13.7 and kernel
`6.12.41+deb13-sonic-amd64`.

Broadcom detects the ASIC, attaches it and reports clock PLL locks, but
`init bcm` returns an internal error. SAI switch creation returns
`SAI_STATUS_FAILURE`; orchagent then deliberately aborts. SWSS fails, APPL_DB
has no `PORT_TABLE:*` entries, and `show interfaces status` has no port rows.
CONFIG_DB already contains the 56 front-panel ports and the correct platform
and HwSKU. ZTP was disabled and inactive during subsequent tests.

Platform configuration file:

```text
/usr/share/sonic/device/x86_64-accton_as7326_56x-r0/Accton-AS7326-56X/td3-as7326-48x25G+8x100G.config.bcm
```

Tests performed on the installed switch:

1. Changed `stable_size=0x5500000` to `0x5800000`, following upstream
   [PR #22641](https://github.com/sonic-net/sonic-buildimage/pull/22641).
   Confirmed the changed value inside syncd. SDK initialization still failed.
2. Found `sai_load_hw_config` pointing to the absent directory
   `/etc/bcm/flex/bcm56870_a0_premium_issu/b870.6.4.1/`.
   The container ships `b870.6.15.0`, with `default` symlinked to that directory.
   Changed the setting to `/etc/bcm/flex/bcm56870_a0_premium_issu/default/`.
   The Cancun missing-directory warnings disappeared, but `init bcm` still failed.
3. Read syncd's startup scripts. No `/tmp/sai.profile` was present, and
   `/etc/sai.d/sai.profile` selected the exact HwSKU configuration file above.
   There was no evidence of a generated profile overriding the tested file.

At that stage the normal logs did not expose the SDK's underlying internal
error. Neither earlier workaround fixed the installed SDK. The direct test
above subsequently established stable-cache exhaustion at 88 MiB.

The initial 88 MiB workaround was committed in
`6661fd3805bc785747d3a60f7b2249a097941995`. The revised
`patches/202605/0004-increase-as7326-sdk-stable-size.patch` now includes the
tested 128 MiB allocation and firmware-path change.

A fresh [202605 Broadcom-only build](https://github.com/ITJamie/sonic-vs-builder/actions/runs/43276423763)
was dispatched with ZTP disabled and both cache options enabled. Dispatch is not
evidence of a successful build or hardware fix; review its final metadata and
test results separately.

## Historical clue and proposed comparison tests

[Issue #21295](https://github.com/sonic-net/sonic-buildimage/issues/21295)
reports the same AS7326 missing-interface/SDK-initialization symptoms. Comments
identify [PR #20450](https://github.com/sonic-net/sonic-buildimage/pull/20450)
as a regression boundary: that 202405 change upgraded XGS SAI from
`10.1.42.0-1` to `11.2.4.1`, while syncd remained on Bullseye. PR #22641's
stable-size change was reported to fix older images, but did not fix our test.
Matching symptoms do not establish an identical cause in SAI 15.2.

Proposed future investigation, not a completed test plan execution:

- Build/test **202505 Broadcom with ZTP disabled** as an intermediate
  SAI 13.2 / Bookworm stack. It changes both the SAI/SDK stack and userspace
  relative to 202605, so a successful result would not isolate Trixie as the cause.
- Build/test **202311 Broadcom with ZTP disabled** as a pre-SAI-11.2 baseline.
  It is a potentially useful comparison, not a guarantee of AS7326 compatibility.
- If necessary, compare the known-good/bad 202405 commits from issue #21295 to
  narrow the SAI transition while retaining the same Debian container family.
- Before each test, retain the current configuration and a bootable recovery
  image. Record upstream/builder commits, patches, ZTP state, SAI/API/SDK versions,
  firmware paths, effective SDK configuration, and full initialization logs.
- Collect a diagnostic archive (`sudo generate_dump`) from the failing switch
  for upstream platform/SDK maintainers. Review its contents before public upload.
- Evaluate a SAI-only backport/bisect only after checking matching kernel modules,
  ABI/API requirements and firmware compatibility; replacing a library alone is
  not an established safe comparison method.

As of this review there was no 202505 Broadcom installer among our GitHub release
assets; the available 202505 image assets were VS Docker images. Recheck releases
before scheduling another build. No additional build is authorized by this note.
