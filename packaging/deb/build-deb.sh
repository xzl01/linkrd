#!/usr/bin/env bash
#
# build-deb.sh -- build installable Debian binary packages for linkrd from
# prebuilt Linux executables, without dpkg-deb/dpkg (works on macOS).
#
# The packages are generated according to the Debian binary package format:
#
#   ar archive:
#     debian-binary     "2.0\n"
#     control.tar.gz    ./control, ./md5sums, ./postinst
#     data.tar.gz       files below ./usr, root:root, mode from the manifest
#
# Everything is deterministic: fixed mtimes from SOURCE_DATE_EPOCH (default:
# the date of the top debian/changelog entry), sorted tar members, gzip -9n
# with a fixed timestamp, root:root ownership and the classic ar format.
#
# Usage:
#   packaging/deb/build-deb.sh --binary <path> --arch amd64|arm64 \
#       [--kind static|gnu] [--outdir <dir>] ...
#   packaging/deb/build-deb.sh [--outdir <dir>] [--prefer static|gnu]
#   packaging/deb/build-deb.sh --help
#
# --binary may be given more than once; each --binary needs an --arch.  The
# default kind is "static" (musl).  gnu packages are written to
# <outdir>/gnu/ and have a "libc6 (>= 2.28)" dependency; static packages have
# no Depends at all.  Without --binary the script scans .build/artifacts/.
#
# Examples:
#   packaging/deb/build-deb.sh --binary .build/artifacts/linkrd-linux-amd64 \
#       --arch amd64 --binary .build/artifacts/linkrd-linux-arm64 --arch arm64
#   packaging/deb/build-deb.sh --binary /tmp/linkrd-linux-amd64 --arch amd64
#       --kind gnu
#   packaging/deb/build-deb.sh                  # auto-discover, static only
#
# Unsupported on this build host: dpkg -i, lintian, udev, systemd user units
# and the real hardware.  See SMOKE.md for the checks that need a real
# Debian/Ubuntu machine.

set -euo pipefail

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
CHANGELOG="$ROOT/debian/changelog"
ARTIFACT_DIR="$ROOT/.build/artifacts"

usage() {
	cat <<'EOF'
Usage: build-deb.sh [options]

Generate linkrd_<version>_<arch>.deb from prebuilt Linux executables.

Options:
  --binary <path>   Prebuilt Linux executable (repeatable; each needs --arch).
  --arch <arch>     amd64 or arm64.  Applies to the preceding/following
                    --binary within the same group.
  --kind <kind>     static (default, musl, no Depends) or gnu (glibc,
                    Depends: libc6 (>= 2.28)).  gnu output goes to
                    <outdir>/gnu/.
  --outdir <dir>    Output directory.  Default: <repo>/dist
  --prefer <kind>   Auto-discovery preference: static (default) or gnu.
  --auto            Force auto-discovery of .build/artifacts/.
  -h, --help        Show this help.

Without --binary the script scans .build/artifacts/ for
linkrd-linux-<arch>[-gnu] (also accepts x86_64/aarch64 spellings) and builds
one package per architecture.  Default kind is static; gnu is only built when
explicitly requested with --kind gnu or with --prefer gnu.

Environment:
  SOURCE_DATE_EPOCH  Timestamp used for tar/gzip/ar mtimes and Installed-Size
                     determinism.  Defaults to the top changelog entry date.
EOF
}

die() {
	printf 'build-deb: %s\n' "$*" >&2
	exit 1
}

OUTDIR="$ROOT/dist"
PREFER=static
JOBS=()
PENDING_BIN=""
PENDING_ARCH=""
PENDING_KIND=""
FORCE_AUTO=0

flush_job() {
	[ -n "$PENDING_BIN" ] || return 0
	[ -n "$PENDING_ARCH" ] || die "--binary $PENDING_BIN requires --arch amd64|arm64"
	case "$PENDING_ARCH" in
	amd64 | arm64) ;;
	*) die "invalid --arch '$PENDING_ARCH' (expected amd64 or arm64)" ;;
	esac
	local kind=${PENDING_KIND:-static}
	case "$kind" in
	static | gnu) ;;
	*) die "invalid --kind '$kind' (expected static or gnu)" ;;
	esac
	[ -f "$PENDING_BIN" ] || die "binary not found: $PENDING_BIN"
	[ -r "$PENDING_BIN" ] || die "binary not readable: $PENDING_BIN"
	JOBS+=("$PENDING_BIN|$PENDING_ARCH|$kind")
	PENDING_BIN=""
	PENDING_ARCH=""
	PENDING_KIND=""
}

while [ $# -gt 0 ]; do
	case "$1" in
	--binary)
		[ $# -ge 2 ] || die "--binary requires a path"
		flush_job
		PENDING_BIN=$2
		shift 2
		;;
	--arch)
		[ $# -ge 2 ] || die "--arch requires amd64 or arm64"
		PENDING_ARCH=$2
		shift 2
		;;
	--kind)
		[ $# -ge 2 ] || die "--kind requires static or gnu"
		PENDING_KIND=$2
		shift 2
		;;
	--outdir)
		[ $# -ge 2 ] || die "--outdir requires a directory"
		OUTDIR=$2
		shift 2
		;;
	--prefer)
		[ $# -ge 2 ] || die "--prefer requires static or gnu"
		PREFER=$2
		shift 2
		;;
	--auto)
		FORCE_AUTO=1
		shift
		;;
	-h | --help)
		usage
		exit 0
		;;
	*)
		die "unknown argument: $1 (see --help)"
		;;
	esac
done
flush_job

case "$PREFER" in
static | gnu) ;;
*) die "invalid --prefer '$PREFER' (expected static or gnu)" ;;
esac

DEB_VERSION=$(sed -n '1s/^[^(]*(\([^)]*\)).*/\1/p' "$CHANGELOG")
[ -n "$DEB_VERSION" ] || die "cannot read the version from $CHANGELOG"
case "$DEB_VERSION" in
[0-9]*) ;;
*) die "unexpected version '$DEB_VERSION' in $CHANGELOG" ;;
esac

# Find a prebuilt executable for one architecture.  Sets FOUND_PATH and
# FIND_KIND ("static" or "gnu"); returns 1 when no artifact is present.
FOUND_PATH=""
FIND_KIND=""
find_artifact() {
	local arch=$1 alt kind=$2 candidate
	FOUND_PATH=""
	FIND_KIND=""
	case "$arch" in
	amd64) alt=x86_64 ;;
	arm64) alt=aarch64 ;;
	*) return 1 ;;
	esac
	local candidates=()
	if [ "$kind" = gnu ]; then
		candidates=(
			"linkrd-linux-$arch-gnu"
			"linkrd-linux-$alt-gnu"
			"linkrd-$arch-gnu"
			"linkrd-$alt-gnu"
		)
	else
		candidates=(
			"linkrd-linux-$arch"
			"linkrd-linux-$alt"
			"linkrd-$arch"
			"linkrd-$alt"
			"linkrd-linux-$arch-static"
			"linkrd-$arch-static"
		)
	fi
	for candidate in "${candidates[@]}"; do
		if [ -f "$ARTIFACT_DIR/$candidate" ]; then
			FOUND_PATH=$ARTIFACT_DIR/$candidate
			FIND_KIND=$kind
			return 0
		fi
	done
	return 1
}

if [ "${#JOBS[@]}" -eq 0 ]; then
	[ -d "$ARTIFACT_DIR" ] || die "no --binary given and $ARTIFACT_DIR does not exist"
	for arch in amd64 arm64; do
		FOUND_PATH=""
		if [ "$PREFER" = gnu ]; then
			find_artifact "$arch" gnu || true
		fi
		if [ -z "$FOUND_PATH" ]; then
			find_artifact "$arch" static || true
		fi
		if [ -z "$FOUND_PATH" ]; then
			find_artifact "$arch" gnu || true
		fi
		if [ -z "$FOUND_PATH" ]; then
			printf 'build-deb: no prebuilt binary for %s in %s, skipping\n' \
				"$arch" "$ARTIFACT_DIR" >&2
			continue
		fi
		JOBS+=("$FOUND_PATH|$arch|$FIND_KIND")
	done
	[ "${#JOBS[@]}" -gt 0 ] || die "no usable prebuilt binaries found; pass --binary/--arch"
fi

printf 'build-deb: linkrd %s, %d package(s), outdir %s\n' \
	"$DEB_VERSION" "${#JOBS[@]}" "$OUTDIR"

for job in "${JOBS[@]}"; do
	IFS='|' read -r bin arch kind <<<"$job"
	bin=$(cd -- "$(dirname -- "$bin")" && pwd)/$(basename -- "$bin")
	job_outdir=$OUTDIR
	if [ "$kind" = gnu ]; then
		job_outdir="$OUTDIR/gnu"
	fi
	mkdir -p "$job_outdir"
	printf 'build-deb: packing %s (%s, %s)\n' "$bin" "$arch" "$kind"
	python3 - "$ROOT" "$bin" "$arch" "$kind" "$job_outdir" "$DEB_VERSION" <<'PY'
"""Create one Debian binary package without dpkg-deb (pure Python stdlib)."""

import email.utils
import gzip
import hashlib
import io
import os
import struct
import sys
import tarfile

ROOT, BINARY, ARCH, KIND, OUTDIR, VERSION = sys.argv[1:7]
MAINTAINER = "hiifong <f@f.style>"
HOMEPAGE = "https://github.com/radxa-linkr/linkrd"
PACKAGE = "linkrd"


def fail(message):
    raise SystemExit("build-deb: " + message)


def read(relpath):
    with open(os.path.join(ROOT, relpath), "rb") as handle:
        return handle.read()


def changelog_epoch():
    text = read("debian/changelog").decode("utf-8")
    # The timestamp is the last field of the first entry's trailer line.
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("-- "):
            try:
                stamp = line.rsplit("  ", 1)[-1]
                return int(email.utils.parsedate_to_datetime(stamp).timestamp())
            except (ValueError, TypeError, IndexError):
                break
    return 1793071800  # 2026-10-27T11:30:00+08:00, top changelog entry


EPOCH = int(os.environ.get("SOURCE_DATE_EPOCH") or 0) or changelog_epoch()


def gz(payload):
    buffer = io.BytesIO()
    with gzip.GzipFile(
        fileobj=buffer, mode="wb", compresslevel=9, mtime=EPOCH, filename=""
    ) as handle:
        handle.write(payload)
    return buffer.getvalue()


def control_description():
    """Reuse the binary stanza description from debian/control."""
    text = read("debian/control").decode("utf-8")
    for paragraph in text.split("\n\n"):
        fields = {}
        name = None
        for line in paragraph.splitlines():
            if line[:1] in (" ", "\t"):
                if name is None:
                    fail("malformed debian/control paragraph")
                fields[name] += "\n" + line
            elif ":" in line:
                name, value = line.split(":", 1)
                fields[name] = value.strip()
        if fields.get("Package") == PACKAGE:
            return fields["Description"]
    fail("no Package: linkrd stanza in debian/control")


def elf_info(payload):
    """Return (elf_class, machine, has_interp) or None for non-ELF input."""
    if payload[:4] != b"\x7fELF":
        return None
    elf_class = payload[4]
    endian = "<" if payload[5] == 1 else ">"
    machine = struct.unpack_from(endian + "H", payload, 18)[0]
    if elf_class == 2:
        phoff = struct.unpack_from(endian + "Q", payload, 32)[0]
        phentsize = struct.unpack_from(endian + "H", payload, 54)[0]
        phnum = struct.unpack_from(endian + "H", payload, 56)[0]
    elif elf_class == 1:
        phoff = struct.unpack_from(endian + "I", payload, 28)[0]
        phentsize = struct.unpack_from(endian + "H", payload, 42)[0]
        phnum = struct.unpack_from(endian + "H", payload, 44)[0]
    else:
        return None
    has_interp = False
    for index in range(phnum):
        offset = phoff + index * phentsize
        if offset + 4 > len(payload):
            break
        if struct.unpack_from(endian + "I", payload, offset)[0] == 3:  # PT_INTERP
            has_interp = True
            break
    return elf_class, machine, has_interp


def check_executable(payload):
    """Sanity-check the archive member and warn about kind/ELF mismatches."""
    info = elf_info(payload)
    if info is None:
        print(
            "build-deb: WARNING: %s is not an ELF executable; packaging it anyway "
            "(useful for structural tests only)" % BINARY,
            file=sys.stderr,
        )
        return
    elf_class, machine, has_interp = info
    expected = {"amd64": 62, "arm64": 183}[ARCH]
    if machine != expected:
        fail(
            "%s has ELF machine %d, expected %d for %s"
            % (BINARY, machine, expected, ARCH)
        )
    if KIND == "static" and has_interp:
        print(
            "build-deb: WARNING: %s is dynamically linked but --kind static was "
            "requested; the package will not declare libc6" % BINARY,
            file=sys.stderr,
        )
    if KIND == "gnu" and not has_interp:
        print(
            "build-deb: WARNING: %s is statically linked but --kind gnu was "
            "requested; the package will declare libc6" % BINARY,
            file=sys.stderr,
        )


def tar_bytes(entries):
    """entries: list of (name, payload, mode); directories get 0755."""
    buffer = io.BytesIO()
    with tarfile.open(
        fileobj=buffer, mode="w", format=tarfile.GNU_FORMAT
    ) as archive:
        for name, payload, mode in entries:
            member = tarfile.TarInfo(name)
            member.uid = member.gid = 0
            member.uname = member.gname = "root"
            member.mtime = EPOCH
            if payload is None:  # directory
                member.type = tarfile.DIRTYPE
                member.mode = 0o755
                member.size = 0
                archive.addfile(member)
            else:
                member.type = tarfile.REGTYPE
                member.mode = mode
                member.size = len(payload)
                archive.addfile(member, io.BytesIO(payload))
    return buffer.getvalue()


def ar_bytes(members):
    output = bytearray(b"!<arch>\n")
    for name, payload in members:
        header = (
            name.ljust(16)
            + str(EPOCH).ljust(12)
            + "0".ljust(6)
            + "0".ljust(6)
            + "100644".ljust(8)
            + str(len(payload)).ljust(10)
            + "`\n"
        ).encode("ascii")
        if len(header) != 60:
            fail("internal error: bad ar header length")
        output += header
        output += payload
        if len(payload) % 2:
            output += b"\n"
    return bytes(output)


binary_payload = open(BINARY, "rb").read()
check_executable(binary_payload)

control = control_description()
installed_size_note = "Installed-Size: %d\n"

files = [
    ("usr/bin/linkrd", binary_payload, 0o755),
    (
        "usr/lib/udev/rules.d/70-linkrd.rules",
        read("contrib/udev/70-linkrd.rules"),
        0o644,
    ),
    (
        "usr/lib/systemd/user/linkrd.service",
        read("contrib/systemd/linkrd.service"),
        0o644,
    ),
    ("usr/share/man/man1/linkrd.1.gz", gz(read("man/linkrd.1")), 0o644),
    ("usr/share/doc/linkrd/README.md", read("README.md"), 0o644),
    ("usr/share/doc/linkrd/README.zh-CN.md", read("README.zh-CN.md"), 0o644),
    ("usr/share/doc/linkrd/README.debian", read("debian/README.debian"), 0o644),
    ("usr/share/doc/linkrd/changelog.Debian.gz", gz(read("debian/changelog")), 0o644),
    ("usr/share/doc/linkrd/copyright", read("debian/copyright"), 0o644),
]
files.sort(key=lambda entry: entry[0])

directories = {"./"}
for path, _, _ in files:
    parts = path.split("/")
    for depth in range(1, len(parts)):
        directories.add("./" + "/".join(parts[:depth]) + "/")
ordered_directories = sorted(directories, key=lambda path: (path.count("/"), path))

data_entries = [(path, None, 0o755) for path in ordered_directories]
data_entries += [("./" + path, payload, mode) for path, payload, mode in files]

installed_size = sum((len(payload) + 1023) // 1024 for _, payload, _ in files)
installed_size += 4 * len(ordered_directories)

md5sums = "".join(
    "%s  %s\n" % (hashlib.md5(payload).hexdigest(), path)
    for path, payload, _ in files
)

postinst = b"""#!/bin/sh
set -e

# The package installs /usr/lib/udev/rules.d/70-linkrd.rules.  Reload the udev
# rule set and re-apply it to existing tty devices, so that an already plugged
# Linkr device receives the seat ACL without being re-plugged.  udevadm is
# absent in containers, chroots and non-udev systems; failures are ignored.
if [ "$1" = "configure" ] && command -v udevadm >/dev/null 2>&1; then
    udevadm control --reload-rules 2>/dev/null || true
    udevadm trigger --subsystem-match=tty --action=change 2>/dev/null || true
fi

exit 0
"""

control_text = "Package: %s\n" % PACKAGE
control_text += "Version: %s\n" % VERSION
control_text += "Architecture: %s\n" % ARCH
control_text += "Maintainer: %s\n" % MAINTAINER
control_text += installed_size_note % installed_size
if KIND == "gnu":
    control_text += "Depends: libc6 (>= 2.28)\n"
control_text += "Recommends: systemd\n"
control_text += "Section: utils\n"
control_text += "Priority: optional\n"
control_text += "Homepage: %s\n" % HOMEPAGE
control_text += "Description: %s\n" % control
if not control_text.endswith("\n"):
    control_text += "\n"

control_tar = tar_bytes(
    [
        ("./control", control_text.encode("utf-8"), 0o644),
        ("./md5sums", md5sums.encode("utf-8"), 0o644),
        ("./postinst", postinst, 0o755),
    ]
)
data_tar = tar_bytes(data_entries)

# dpkg-deb writes ar member names with plain space padding (no GNU ar
# trailing slash), which stricter consumers such as python-debian and 7z
# expect.  Keep exactly that layout.
archive = ar_bytes(
    [
        ("debian-binary", b"2.0\n"),
        ("control.tar.gz", gz(control_tar)),
        ("data.tar.gz", gz(data_tar)),
    ]
)

filename = "%s_%s_%s.deb" % (PACKAGE, VERSION, ARCH)
output_path = os.path.join(OUTDIR, filename)
temporary = output_path + ".tmp"
with open(temporary, "wb") as handle:
    handle.write(archive)
os.replace(temporary, output_path)

print(
    "built: %s (%d bytes, sha256 %s, Installed-Size %d KiB)"
    % (output_path, len(archive), hashlib.sha256(archive).hexdigest(), installed_size)
)
PY
done

printf 'build-deb: done\n'
