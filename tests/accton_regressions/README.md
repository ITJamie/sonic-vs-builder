# Accton backport regressions

PSU, CPU, PWM, BIOS and sensor-recovery tests copied from the reviewed `accton-fixes` series at
`25e1c0590f93cd6a7e44c134d0c5e77e65e2e9b3`, PR
[sonic-buildimage #30780](https://github.com/sonic-net/sonic-buildimage/pull/30780).

`edgecore_platform_integration.py` runs them against the fully patched, pinned
202605 source. It provides the source root and a minimal thermal base class;
the platform implementations and PMBus C callbacks under test are real.
The AS5835 driver test addresses the shared `common/modules/ym2651y.c` directly
because the sparse fixture does not materialize its platform symlink.

The parity manifest records all 55 backported implementation files. It retains
202605's fan conversion format and source differences rather than copying newer
master dependencies. The integration also compares startup `main()` syntax
trees with the original 202605 files to guard the original fan startup policy.

The AS7326 PWM test is adapted to 202605's lambda-based conversions; its values
and policy round trips match the master tests. The sparse fixture supplies real
sensor/component base classes and substitutes hardware discovery only. Input
telemetry regressions cover AS7326/AS7726 while excluding AS5835 inputs.

AS7816 tests also cover YM-2851F limits/capacity, real airflow reporting,
CPU/BIOS/output sensors, explicit unsupported inputs and recovery to the
original 52 percent fan policy with all six board sensor indices preserved.

Capacity tests additionally cover both AS7326/AS7726 capacity APIs, each PSU
register mapping, mW-to-W conversion, changing manufacturer ratings and
missing/failed reads without a fixed-rating fallback.
