# Future work

## Review Broadcom SAI compatibility on AS7326

See [Broadcom SAI versions and AS7326 investigation](broadcom-sai-investigation.md)
for the release/container comparison, upstream regression references, unsuccessful
manual workarounds, and proposed 202505/202311 comparison tests. Direct SDK
testing subsequently identified stable-cache exhaustion: 128 MiB enabled normal
initialization on the test switch. The build patch now includes that allocation
and the tested firmware path; reboot, forwarding and platform-health validation
remain outstanding.

## Give built images a recognisable version name

Requested: use a clear build identity in future images instead of an automatically
generated GRUB title such as `SONiC-OS-HEAD.0-dirty-20261110.174833`.

Implemented using upstream's native `SONIC_IMAGE_VERSION` override, consistently
across configuration and selected image builds. The version now uses the pipeline
attempt's start date/hour in UTC instead of the long GitHub run ID:

```text
202605-itjamie.20261112-22-ztp
```

The resulting GRUB entry would be:

```text
SONiC-OS-202605-itjamie.20261112-22-ztp
```

Releases use `SONiC 202605 — 20261112-22` and tag
`sonic-202605-20261112-22`. Same-branch/hour collisions receive `-2`, `-3`, etc.;
existing tags/releases are preserved. Long run IDs and attempt numbers remain in
metadata and the build-log link, alongside the precise start timestamp, upstream
SHA, builder SHA, applied patches and ZTP setting. Existing releases are not
renamed. Each output uses the identity reserved once during workflow preparation.

Status: implemented. Native source integration covers all seven release branches;
VS smoke checks validate the embedded version. Confirm the resulting names in
GRUB, `show version` and the installed-image list after the next hardware install.
