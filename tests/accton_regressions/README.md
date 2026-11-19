# Accton backport regressions

PSU, CPU, PWM, BIOS and sensor-recovery tests copied from the reviewed `accton-fixes` series at
`5b9c212e474020111a9827a77c10665e0a16f782`, PR
[sonic-buildimage #30780](https://github.com/sonic-net/sonic-buildimage/pull/30780).

`edgecore_platform_integration.py` runs them against the fully patched, pinned
202605 source. It provides the source root and a minimal thermal base class;
the platform implementations and PMBus C callbacks under test are real.
The AS5835 driver test addresses the shared `common/modules/ym2651y.c` directly
because the sparse fixture does not materialize its platform symlink.

The parity manifest records all 50 backported implementation files. It retains
202605's fan conversion format and source differences rather than copying newer
master dependencies. The integration also compares startup `main()` syntax
trees with the original 202605 files to guard the original fan startup policy.

The AS7326 PWM test is adapted to 202605's lambda-based conversions; its values
and policy round trips match the master tests. The sparse fixture supplies real
sensor/component base classes and substitutes hardware discovery only. Input
telemetry regressions cover AS7326/AS7726 while excluding AS5835 inputs.
