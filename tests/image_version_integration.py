"""Exercise date-based identities through pinned upstream Make/YAML/installer paths."""
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import urllib.request

from jinja2 import Template

sys.path.insert(0, str(Path(__file__).parents[1] / "scripts"))
from sonic_vs import fix_vs_image_version, version_name

COMMITS = {
    "202111": "d0d72344748d8cb0ebc3d26cae00f73bbdafa61f",
    "202211": "1a8b0411868a4783bdb5ab9425f9b7f04c80ca6a",
    "202311": "e5cd212fec5c47e88be1afbaac079005c45f6db4",
    "202405": "cfc766e13eccab4e7603808db3bfa9c5b2dfef17",
    "202505": "ff3c0c9f686a1d0dbddbe813b233b3b7a51bd8e2",
    "202511": "72b5bece858478655d2f93d887e6d9ab222ea2ee",
    "202605": "5ef9e45ccf7f004fb07bf3f9be2f471cf00573bf",
}
PATHS = ("slave.mk", "Makefile.work", "platform/vs/sonic-version.mk",
         "platform/vs/sonic-version/build_sonic_version.sh",
         "files/build_templates/sonic_version.yml.j2", "build_debian.sh",
         "build_image.sh", "onie-mk-demo.sh", "installer/install.sh",
         "installer/default_platform.conf")


def fetch(item):
    branch, path = item
    if branch == "202111" and path in ("installer/install.sh", "installer/default_platform.conf"):
        # This branch predates the shared installer/platform split.
        path = "installer/x86_64/install.sh"
    url = f"https://raw.githubusercontent.com/sonic-net/sonic-buildimage/{COMMITS[branch]}/{path}"
    try:
        return item, urllib.request.urlopen(url, timeout=60).read().decode()
    except OSError as error:
        raise RuntimeError(f"Cannot fetch {url}: {error}") from error


with ThreadPoolExecutor(max_workers=12) as pool:
    sources = dict(pool.map(fetch, [(b, p) for b in COMMITS for p in PATHS]))

with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    j2 = root / "j2"
    # Execute the real upstream VS generator with the real upstream YAML
    # template; this adapter replaces only the j2 CLI, not template rendering.
    j2.write_text(f"#!{sys.executable}\nfrom pathlib import Path\nimport os,sys\n"
                  "from jinja2 import Template\n"
                  "print(Template(Path(sys.argv[1]).read_text()).render(**os.environ))\n")
    j2.chmod(0o755)
    for branch in COMMITS:
        source = root / branch
        source.mkdir()
        native = sources[branch, "slave.mk"]
        override = re.search(r"(?m)^ifeq \(\$\(SONIC_IMAGE_VERSION\),\).*?^endif", native, re.S).group()
        vs_rule = source / "platform/vs/sonic-version.mk"
        vs_rule.parent.mkdir(parents=True)
        vs_rule.write_text(sources[branch, "platform/vs/sonic-version.mk"])
        fix_vs_image_version(source)
        assert "sonic_version=$(SONIC_IMAGE_VERSION)" in vs_rule.read_text()
        assert fix_vs_image_version(source) == []
        makefile = source / "probe.mk"
        makefile.write_text(override + "\ninclude platform/vs/sonic-version.mk\n"
                            ".PHONY: identity\nidentity:\n\t@printf '%s\\n' '$(SONIC_IMAGE_VERSION)' '$(sonic_version)'\n")
        # The outer Docker invocation forwards the same native override.
        assert "SONIC_IMAGE_VERSION=$(SONIC_IMAGE_VERSION)" in sources[branch, "Makefile.work"]
        assert 'export build_version="${SONIC_IMAGE_VERSION}"' in sources[branch, "build_debian.sh"]
        assert 'IMAGE_VERSION="${SONIC_IMAGE_VERSION}"' in sources[branch, "build_image.sh"]
        assert "s/%%IMAGE_VERSION%%/$image_version/g" in sources[branch, "onie-mk-demo.sh"]
        installer = sources[branch, "installer/install.sh"]
        platform = sources[branch, "installer/default_platform.conf"]
        assert "menuentry" in platform and "demo_volume_revision_label" in platform
        template = sources[branch, "files/build_templates/sonic_version.yml.j2"]
        (source / "sonic_version.yml.j2").write_text(template)
        generator = source / "build_sonic_version.sh"
        generator.write_text(sources[branch, "platform/vs/sonic-version/build_sonic_version.sh"])
        for enabled in (False, True):
            version = version_name(branch, "20261112-22-2", enabled)
            values = subprocess.check_output(["make", "-s", "-f", str(makefile),
                "identity", f"SONIC_IMAGE_VERSION={version}", "SONIC_GET_VERSION=WRONG-HEAD-dirty"],
                cwd=source, text=True).splitlines()
            assert values == [version, version], values
            # Supply a tiny Git executable only for unrelated commit/branch
            # metadata; retain upstream's actual version export and rendering.
            git = root / "git"
            git.write_text("#!/bin/sh\nprintf '%s\\n' test-source\n")
            git.chmod(0o755)
            env = dict(os.environ, PATH=f"{root}:{os.environ['PATH']}", sonic_version=values[1])
            subprocess.run(["bash", str(generator), "version.yml"], cwd=source, env=env, check=True)
            assert f"build_version: '{version}'" in (source / "version.yml").read_text()
            # Host build_debian exports the same value to this template.
            assert f"build_version: '{version}'" in Template(template).render(build_version=values[0])
            rendered = installer.replace("%%IMAGE_VERSION%%", version).replace("%%DEMO_TYPE%%", "OS")
            assignments = []
            for name in ("image_version", "demo_type", "demo_volume_revision_label", "image_dir"):
                assignments.append(re.search(rf'(?m)^{name}="[^"\n]*"$', rendered).group())
            identity = subprocess.check_output(["bash", "-eu", "-c", "\n".join(assignments) +
                '\nprintf "%s\\n" "$demo_volume_revision_label" "$image_dir"'], text=True).splitlines()
            assert identity == [f"SONiC-OS-{version}", f"image-{version}"], identity
        print(f"{branch}: native override, VS/host YAML, installer/GRUB identity verified", flush=True)
