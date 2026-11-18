# Accton backport regressions

PSU and CPU tests copied from the reviewed `accton-fixes` series at
`2d7307eed68a21ce1b4a05461a7cd2b0c6e9a50d`, PR
[sonic-buildimage #30780](https://github.com/sonic-net/sonic-buildimage/pull/30780).

`edgecore_platform_integration.py` runs them against the fully patched, pinned
202605 source. It provides the source root and a minimal thermal base class;
the platform implementations and PMBus C callbacks under test are real.
The AS5835 driver test addresses the shared `common/modules/ym2651y.c` directly
because the sparse fixture does not materialize its platform symlink.

The parity manifest records all 36 backported implementation files. It retains
202605's fan conversion format and source differences rather than copying newer
master dependencies. The integration also compares startup `main()` syntax
trees with the original 202605 files to guard the original fan startup policy.
