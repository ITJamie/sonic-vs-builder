"""Reproduce GCC's optional optimization diagnostic with real upstream flags."""

from pathlib import Path
import json
import shlex
import subprocess
import tempfile
import urllib.request
import uuid

ROOT = Path(__file__).parents[1]
COMMIT = "14db8ad4452f70f375795b2d7d7bf797d2c012b5"
PATH = "src/sonic-sairedis/pyext/py3/Makefile.am"
PATCH = ROOT / "patches/202505/0002-backport-sairedis-optimization-warning-fix.patch"

with tempfile.TemporaryDirectory() as directory:
    work = Path(directory).resolve()
    target = work / PATH
    target.parent.mkdir(parents=True)
    original = urllib.request.urlopen(
        f"https://raw.githubusercontent.com/sonic-net/sonic-sairedis/{COMMIT}/pyext/py3/Makefile.am",
        timeout=60).read().decode()
    target.write_text(original)
    subprocess.run(["git", "init", "-q", str(work)], check=True)
    subprocess.run(["git", "-C", str(work), "apply", "--check", str(PATCH)], check=True)
    subprocess.run(["git", "-C", str(work), "apply", str(PATCH)], check=True)

    def flags(contents):
        logical_lines = contents.replace("\\\n", " ").splitlines()
        assignment = next(line for line in logical_lines
                          if line.startswith("_pysairedis_la_CXXFLAGS ="))
        (work / "flags.mk").write_text(
            "CXXFLAGS_COMMON = -Werror -Wdisabled-optimization\n" + assignment +
            '\nprint:\n\t@echo "$(_pysairedis_la_CXXFLAGS)"\n')
        return shlex.split(subprocess.check_output(
            ["make", "-s", "-f", "flags.mk", "print"], cwd=work, text=True))

    before = flags(original)
    after = flags(target.read_text())
    assert after == [*before, "-Wno-error=disabled-optimization"]
    (work / "flags.json").write_text(json.dumps({"before": before, "after": after}))
    # A small generated function plus a deliberately tiny optimization budget
    # triggers the same GCC diagnostic without compiling the huge SAI wrapper.
    statements = "\n".join(f"if (values[{i}]) result += external({i});" for i in range(300))
    (work / "generated.cpp").write_text(
        "extern int external(int);\nint probe(volatile int *values) { int result = 0;\n" +
        statements + "\nreturn result; }\n")
    (work / "unrelated.cpp").write_text("int probe() { int unused = 0; return 0; }\n")
    (work / "probe.py").write_text('''
import json
import pathlib
import subprocess

flags = json.load(open('/probe/flags.json'))
version = subprocess.check_output(['g++', '-dumpversion'], text=True).strip()
assert version.split('.')[0] == '12', version
def compile(name, options):
    return subprocess.run(['g++', '-O2', '--param=max-gcse-memory=1', *options,
                           '-c', '/probe/' + name, '-o', '/tmp/probe.o'],
                          text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
broken = compile('generated.cpp', flags['before'])
assert broken.returncode != 0 and 'disabled-optimization' in broken.stdout, broken.stdout
print('Reproduced GCC 12 disabled-optimization build error', flush=True)
fixed = compile('generated.cpp', flags['after'])
assert fixed.returncode == 0, fixed.stdout
assert 'warning:' in fixed.stdout and 'disabled-optimization' in fixed.stdout, fixed.stdout
assert pathlib.Path('/tmp/probe.o').stat().st_size > 0
unrelated = compile('unrelated.cpp', [*flags['after'], '-Wunused-variable'])
assert unrelated.returncode != 0 and 'unused-variable' in unrelated.stdout, unrelated.stdout
print('Patched target flags compile; optimization warning stays visible; unrelated warnings remain fatal', flush=True)
''')
    (work / "Dockerfile").write_text(
        "FROM debian:bookworm-slim\n"
        "RUN apt-get update && apt-get install -y --no-install-recommends g++ python3\n"
        "COPY flags.json generated.cpp unrelated.cpp probe.py /probe/\n"
        "RUN python3 -u /probe/probe.py\n")
    tag = "sonic-vs-builder-sairedis-test:" + uuid.uuid4().hex[:12]
    try:
        subprocess.run(["docker", "build", "--platform", "linux/amd64", "--progress=plain",
                        "-t", tag, str(work)], check=True)
    finally:
        subprocess.run(["docker", "image", "rm", tag], check=False)
