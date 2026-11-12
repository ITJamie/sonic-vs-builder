# Future work

## Give built images a recognisable version name

Requested: use a clear build identity in future images instead of an automatically
generated GRUB title such as `SONiC-OS-HEAD.0-dirty-20261110.174833`.

Use upstream's native `SONIC_IMAGE_VERSION` override, consistently across
configuration and selected image builds. Suggested version format:

```text
202605-itjamie.42851292945.1-ztp
```

The resulting GRUB entry would be:

```text
SONiC-OS-202605-itjamie.42851292945.1-ztp
```

Derive the branch, workflow run ID and attempt automatically; append `-ztp`
when enabled. Keep the upstream SHA, builder SHA, applied patches and ZTP setting
in release metadata. Validate the name for all supported upstream branches and
verify consistent identity in GRUB, `show version` and the installed-image list.
Set the actual SONiC image version rather than changing only the GRUB label.

Status: deferred; naming behavior has not been changed.
