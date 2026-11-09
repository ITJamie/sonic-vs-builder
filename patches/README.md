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

- `202505/0001-skip-unused-buster-builder.patch`: use upstream's `NOBUSTER=1`
  default for our three selected image targets. This avoids eagerly preparing
  the unused Buster builder, whose security mirror returns 404. Bullseye and
  Bookworm stay enabled; optional Buster-based PTF test images are outside these
  outputs. An explicit `NOBUSTER=0` restores the upstream setting. Tested against
  `ff3c0c9f686a1d0dbddbe813b233b3b7a51bd8e2`; CI exercises the actual upstream
  Makefile's configure and three target routes, preserving both remaining build
  environments and the explicit override.
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
