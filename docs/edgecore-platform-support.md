# Edgecore platform fixes for 202605

The requested inventory contains six product variants and four platform APIs:

| Product variants | Upstream platform / HwSKU | API in upstream 202605 | ASIC family |
| --- | --- | --- | --- |
| 5835-54X-O-AC-B, 5835-54X-O-AC-F | `x86_64-accton_as5835_54x-r0` / `Accton-AS5835-54X` | Native | Maverick2 |
| 7326-56X-O-AC-B, 7326-56X-O-AC-F | `x86_64-accton_as7326_56x-r0` / `Accton-AS7326-56X` | PDDF | Trident3 |
| 7726-32X-O-AC-F | `x86_64-accton_as7726_32x-r0` / `Accton-AS7726-32X` | PDDF | Trident3 |
| 7816-64X-O-AC-F | `x86_64-accton_as7816_64x-r0` / `Accton-AS7816-64X` | PDDF | Tomahawk2 |

The airflow product suffix does not select a different SONiC platform directory.
The existing fan/PSU direction detection remains in use. These changes apply to
202605 Broadcom builds; no platform migration, alias changes or port remapping
is performed. Older upstream branches do not automatically receive these patches.

## Included changes

| Area | AS5835-54X | AS7326-56X | AS7726-32X | AS7816-64X |
| --- | --- | --- | --- | --- |
| Health / status LED | Preserve existing native methods and config | Add PDDF methods and matching LED labels | Add PDDF methods, LED labels and missing health config | Add PDDF methods and matching LED labels |
| CPU hardware watchdog | Add native adapter and lazy chassis getter | Add missing adapter | Replace existing adapter's unsafe constructor behavior | Add missing adapter |
| Thermal control | Full speed on missing/nonfinite readings, then recover | Full-speed startup and sensor fallback/retry | Same, plus correct exhaust/intake policy selection | Full-speed startup and sensor fallback/retry |
| Service recovery | Restart unexpected monitor failures | Same | Same | Same |
| IDT clock ordering | Not applicable | Before PDDF mux binding | Before PDDF mux binding | Not applicable |
| SDK stable cache | 128 MiB | Existing tested 128 MiB patch | 128 MiB | Preserve vendor/upstream profile |
| VXLAN / RIOT | Exact vendor values, 8192 overlay egress entries | Existing vendor patch, 32768 entries | Exact vendor values, 32768 entries | Preserve vendor/upstream profile; no RIOT overrides |
| Cancun firmware path | No Trident3 path added | Existing `default/` fix | Use SDK-provided `default/` | No Trident3 path added |

AS5835 and AS7726 use their own Edgecore SDK profiles, rather than copying
AS7326's allocations. The newer SDK exhausted an 88 MiB cache on the AS7326
test switch, and 128 MiB worked. Extending that allocation to AS5835/AS7726 is
a software configuration port, not evidence that those two require exactly
128 MiB or that all feature combinations fit. AS7816's Edgecore profile contains
none of these stable-cache/RIOT overrides; its different ASIC is left intact.
MMU, port lanes, aliases, clock register values and unrelated resource settings
are preserved.

## Health and LED details

Upstream's PDDF chassis classes omit `initizalize_system_led`, `get_status_led`
and `set_status_led`. The misspelled initializer is retained because the pinned
`show/system_health.py` calls that exact method. The initializer is a no-op, as
in Edgecore's implementation; health management selects the actual color.

The PDDF kernel driver expects lowercase labels such as `green` and `red`.
Changing the chassis methods alone leaves uppercase JSON labels unusable.
The patches update both JSON and health config, preserving I2C register values.
AS7326's diagnostic fault color follows Edgecore's red mapping; its locator
LED retains amber. AS5835's existing native uppercase color scheme is separate
and is deliberately preserved. Existing ignored devices are retained; failures
are not hidden by adding service exclusions.

## AS7326 PSU voltage validation

Patch `0011` adds Edgecore's missing `psu_v_out_min` and `psu_v_out_max`
definitions to both PSU PMBus devices. Registers `0xa4` and `0xa5` are decoded
by the existing PDDF driver; the existing platform API converts millivolts to
volts. No FSF045-specific constants or health-check exclusions are added.

On 2026-11-13, both FSF019-611 PSUs on the AS7326 test switch returned
11.390–12.593 V limits, with measured outputs around 12.06–12.19 V. Targeted
PMBus re-registration and a `pmon` restart confirmed the values in sysfs, the
platform API and STATE_DB, and cleared the voltage health errors. The existing
100G link stayed up and cooling remained active. This validates the live edit;
an image containing this patch has not yet been built or reboot-tested.

PSU fan target, under-speed and over-speed APIs remain unsupported. Edgecore's
target-speed override returns measured speed, which is not an independent
setpoint; this patch does not adopt that workaround.

Patches `0012` and `0013` complete the live health compatibility fix. `0012`
excludes only `PSU1_FAN1.speed` and `PSU2_FAN1.speed` target comparisons,
following the narrow exclusions proposed in upstream PR #8198. Presence,
status, airflow, RPM-derived speed reporting and PSU voltage/power-good checks
remain enabled. The existing PDDF status reports a stopped fan as failed;
independent detection of a slowly spinning fan still needs real hardware
thresholds or alarms. Missing target APIs remain explicitly `N/A` in STATE_DB.

Once speed comparisons were excluded, direction checking exposed literal
`F2B*` values from both FSF019-611 PSUs. The lookup previously accepted only
`F2B`, leaving an untranslated label that mismatched the chassis's `EXHAUST`.
`0013` adds that exact observed spelling to the PSU direction map. Edgecore's
custom PSU driver independently identifies FSF019 as front-to-back/exhaust.
Unknown labels are not normalized and no direction exclusion is added.

After restarting `system-health` and `pmon` on 2026-11-13, Services and Hardware
both reported OK, and the chassis API read back a green status LED. Both PSU
directions were `EXHAUST`, voltage limits remained correct, there were no failed
systemd units and Ethernet48 remained up at 100G. These are live-edit results;
the patches have not yet been built into an image or tested across a reboot.

## Watchdog details

The observed AS7326 error chain was:

1. `ModuleNotFoundError: No module named 'sonic_platform.watchdog'`.
2. The PDDF exception handler then raises `NameError: syslog is not defined`.

The kernel exposes an inactive `iTCO_wdt` device on the test switch. The new
adapters support `iTCO_wdt` and `wdat_wdt`, and explicitly fail when neither is
available. A shared implementation lives in the required `sonic-platform-common`
package, so native AS5835 does not acquire a PDDF runtime dependency. The tracked
watchdog base documentation is updated alongside the added helper, ensuring
the upstream dependency checker sees a changed source tree when reusing wheels.

Construction, status queries and an already-inactive disarm never open the
character device: opening `/dev/watchdog*` itself arms Linux watchdog hardware.
Explicit operations use 32-bit ioctl arguments, the driver-returned timeout,
kernel state across CLI processes and idempotent disarm. Driver errors and
`nowayout` do not report successful disable. Exiting an explicitly armed CLI
does not send magic-close `V`; the requested timer remains active.

This CPU watchdog is distinct from the fan controller's watchdog. The patch
does not copy Edgecore's forced I2C fan-watchdog writes or enable an automatic
runtime CPU watchdog policy. The existing boot service requests disarm.

## Thermal and clock recovery

The AS7326 boot log showed `None * 1000` terminating the monitor after its initial
38% fan setting. All four board sensors subsequently read correctly. Startup
readiness is a likely cause; the exact transient bus failure has not been proven.

Required missing, invalid or nonfinite readings now select 100% fan PWM and return
to the existing polling loop. Valid readings resume each model's thermal policy.
No temperature thresholds or normal fan policy tables are changed. PDDF monitors
start at full speed, and unexpected process failures restart after five seconds.
AS7726 accepts both its vendor plugin's uppercase `EXHAUST`/`INTAKE` values
and standard lowercase API values, selecting its existing distinct airflow
policy; unknown direction also selects full speed.

AS7326's clock service failed because PDDF claimed its mux before IDT initialization.
The ordering fix is also applied to AS7726's equivalent service. Their second
mux addresses differ (AS7326 `0x70`, AS7726 `0x76`); each original script remains
unchanged. AS5835 and AS7816 receive no clock-ordering changes.

## Verification and remaining hardware checks

The regression fixture applies all ten 202605 patches to pinned upstream,
including adding a module inside an initialized submodule, then checks reverse
recognition and Python compilation. It executes the patched API/monitor methods
with simulated sensor failures and Linux watchdog sysfs/ioctls. It compares SDK
values against the actual pinned Edgecore profiles and checks that unrelated SDK
properties, PDDF sensor inventory and register values are unchanged.

Run:

```sh
python3 -m unittest discover -s tests -v
python3 tests/edgecore_platform_integration.py
```

Only AS7326 has been inspected live. The new platform patches have not been
installed on any switch. On each family, and both listed airflow variants:

1. Install the resulting Broadcom image and verify platform/HwSKU selection.
2. Check `systemctl --failed`, monitor service uptime, `show environment`,
   `show platform fan`, and `show system-health summary` / `detail`.
3. Verify physical diagnostic LED colors match normal/fault states.
4. Observe fan control through startup, sensor loss/recovery and airflow-specific
   temperature transitions in a controlled hardware test.
5. Read watchdog identity/state/nowayout through `/sys/class/watchdog/watchdog*/`.
   Arm/disarm/expiry tests require a maintenance window and console access;
   they were not performed during the read-only investigation.
6. Verify cold boot and reboot, ASIC initialization, all physical ports, and link
   recovery. Test forwarding, VXLAN/EVPN and resource usage before deployment.
7. Validate warm restart separately; stable-cache sizing alone does not prove it.

## Sources

- Upstream buildimage: [`5ef9e45ccf7f004fb07bf3f9be2f471cf00573bf`](https://github.com/sonic-net/sonic-buildimage/tree/5ef9e45ccf7f004fb07bf3f9be2f471cf00573bf).
- Its platform-common submodule: [`910b38bc5d94eb3475cd670a42a0f95250bdc184`](https://github.com/sonic-net/sonic-platform-common/tree/910b38bc5d94eb3475cd670a42a0f95250bdc184).
- Edgecore public `202311.X`: [`d39662e996c0e8748c9b96ad88dc5f5f524e8048`](https://github.com/edge-core/sonic-buildimage/tree/d39662e996c0e8748c9b96ad88dc5f5f524e8048).
- [AS7326 platform enhancements, PR #13809](https://github.com/sonic-net/sonic-buildimage/pull/13809).
- [AS7726 platform enhancements, PR #23737](https://github.com/sonic-net/sonic-buildimage/pull/23737).
- [AS5835 platform enhancements, PR #24028](https://github.com/sonic-net/sonic-buildimage/pull/24028).
- [PDDF watchdog support, PR #25049](https://github.com/sonic-net/sonic-buildimage/pull/25049).
- [AS7326 IDT ordering, PR #30498](https://github.com/sonic-net/sonic-buildimage/pull/30498).
- [Older SDK cache workaround, PR #22641](https://github.com/sonic-net/sonic-buildimage/pull/22641).
- [Linux watchdog API](https://docs.kernel.org/watchdog/watchdog-api.html).
