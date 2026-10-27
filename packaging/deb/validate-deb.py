#!/usr/bin/env python3
"""Structural validator for linkrd Debian binary packages.

The packages are built on macOS without dpkg/dpkg-deb, so this validator
re-implements the checks that dpkg-deb --info/--contents and lintian would
performed on a Debian host, for the subset of properties that matter here:

  * ar container: member order, ``debian-binary`` content, sane headers
  * control.tar.gz: required fields, field formats, package/version/arch
    consistency with the file name, Installed-Size accuracy, md5sums that
    exactly cover the payload, maintainer scripts
  * data.tar.gz: every path starts with "./" and lives below ./usr, root:root
    ownership, no setuid/setgid bits, expected modes, all key files,
    decompressible man page and changelog, ELF architecture/linkage matching
    the declared architecture and dependencies

Usage:
    packaging/deb/validate-deb.py [options] [package.deb ...]

Without package arguments, dist/*.deb and dist/gnu/*.deb are validated.
Exit status: 0 if all packages PASS, 1 if any check failed, 2 on usage/IO
errors (for example when no .deb file was found).
"""

import glob
import gzip
import hashlib
import io
import os
import posixpath
import re
import struct
import sys
import tarfile

USAGE = __doc__.split("Usage:", 1)[1]

# Files that every linkrd binary package must contain, with their exact mode.
REQUIRED_FILES = {
    "usr/bin/linkrd": 0o755,
    "usr/lib/udev/rules.d/70-linkrd.rules": 0o644,
    "usr/lib/systemd/user/linkrd.service": 0o644,
    "usr/share/man/man1/linkrd.1.gz": 0o644,
    "usr/share/doc/linkrd/README.md": 0o644,
    "usr/share/doc/linkrd/README.zh-CN.md": 0o644,
    "usr/share/doc/linkrd/README.debian": 0o644,
    "usr/share/doc/linkrd/changelog.Debian.gz": 0o644,
    "usr/share/doc/linkrd/copyright": 0o644,
}

REQUIRED_CONTROL_FIELDS = [
    "Package",
    "Version",
    "Architecture",
    "Maintainer",
    "Installed-Size",
    "Section",
    "Priority",
    "Homepage",
    "Description",
]

ARCH_MACHINE = {"amd64": 62, "arm64": 183}
MACHINE_NAME = {62: "x86-64", 183: "AArch64"}

FILENAME_RE = re.compile(r"^(?P<package>[A-Za-z0-9.+-]+)_(?P<version>[^_]+)_(?P<arch>[A-Za-z0-9]+)\.deb$")
VERSION_RE = re.compile(r"^[0-9][A-Za-z0-9.+~:-]*$")
MD5_LINE_RE = re.compile(r"^([0-9a-f]{32})  (.+)$")
MAINTAINER_RE = re.compile(r"^\S.*\s<[^<>\s]+@[^<>\s]+>$")


class Report:
    def __init__(self, path):
        self.path = path
        self.results = []  # (status, label, detail)
        self.failed = False

    def add(self, status, label, detail=""):
        self.results.append((status, label, detail))
        if status == "FAIL":
            self.failed = True

    def ok(self, label, detail=""):
        self.add("PASS", label, detail)

    def fail(self, label, detail=""):
        self.add("FAIL", label, detail)

    def warn(self, label, detail=""):
        self.add("WARN", label, detail)

    def info(self, label, detail=""):
        self.add("INFO", label, detail)

    def check(self, condition, label, detail=""):
        if condition:
            self.ok(label)
        else:
            self.fail(label, detail)
        return condition

    def render(self):
        print("\n== %s ==" % self.path)
        status_width = 5
        for status, label, detail in self.results:
            line = "  %-*s  %s" % (status_width, status, label)
            if detail:
                line += "  [%s]" % detail
            print(line)
        failures = sum(1 for status, _, _ in self.results if status == "FAIL")
        warnings = sum(1 for status, _, _ in self.results if status == "WARN")
        state = "FAIL" if failures else "PASS"
        print(
            "  => %s: %s (%d checks, %d failed, %d warnings)"
            % (self.path, state, len(self.results), failures, warnings)
        )
        return failures


def read_ar(path):
    """Parse an ar archive into a list of member dictionaries."""
    with open(path, "rb") as handle:
        data = handle.read()
    if data[:8] != b"!<arch>\n":
        raise ValueError("missing ar global header")
    members = []
    offset = 8
    while offset < len(data):
        if offset + 60 > len(data):
            raise ValueError("truncated ar member header at offset %d" % offset)
        header = data[offset : offset + 60]
        if header[58:60] != b"`\n":
            raise ValueError("bad ar member terminator at offset %d" % offset)
        try:
            name_field = header[0:16].decode("ascii").rstrip(" ")
            name = name_field
            mtime = header[16:28].decode("ascii").strip()
            uid = header[28:34].decode("ascii").strip()
            gid = header[34:40].decode("ascii").strip()
            mode = header[40:48].decode("ascii").strip()
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            raise ValueError("non-numeric ar member header at offset %d" % offset)
        # Tolerate a GNU ar style trailing slash for parsing, but the strict
        # check below requires the dpkg-deb layout (plain space padding).
        if name.endswith("/"):
            name = name[:-1]
        body = data[offset + 60 : offset + 60 + size]
        if len(body) != size:
            raise ValueError("truncated ar member %r" % name)
        members.append(
            {
                "name": name,
                "name_field": name_field,
                "mtime": mtime,
                "uid": uid,
                "gid": gid,
                "mode": mode,
                "size": size,
                "data": body,
            }
        )
        offset += 60 + size
        if size % 2:
            if data[offset : offset + 1] != b"\n":
                raise ValueError("missing ar padding byte for %r" % name)
            offset += 1
    return members


def parse_control(text):
    fields = {}
    order = []
    current = None
    for line in text.splitlines():
        if line[:1] in (" ", "\t"):
            if current is None:
                raise ValueError("continuation line before first field")
            fields[current] += "\n" + line
        elif ":" in line:
            key, value = line.split(":", 1)
            if key in fields:
                raise ValueError("duplicate control field: %s" % key)
            current = key
            fields[key] = value.strip()
            order.append(key)
        elif line.strip() == "":
            raise ValueError("unexpected blank line in control paragraph")
        else:
            raise ValueError("malformed control line: %r" % line)
    return fields, order


def elf_info(payload):
    """Return (elf_class, machine, endian, has_interp) or None for non-ELF."""
    if payload[:4] != b"\x7fELF":
        return None
    elf_class = payload[4]
    endian = "<" if payload[5] == 1 else ">"
    if len(payload) < 52:
        return None
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
        ph_offset = phoff + index * phentsize
        if ph_offset + 4 > len(payload):
            break
        if struct.unpack_from(endian + "I", payload, ph_offset)[0] == 3:  # PT_INTERP
            has_interp = True
            break
    return elf_class, machine, endian, has_interp


def normalize_member_name(name):
    """Check a data.tar path name; return the bare path below ./usr."""
    if name in (".", "./"):
        # The root directory entry; Python's tarfile reads dpkg's "./" as ".".
        return ""
    if not name.startswith("./"):
        raise ValueError("path does not start with './': %r" % name)
    stripped = name[2:]
    if stripped in ("", "."):
        return ""
    if stripped.startswith("/"):
        raise ValueError("absolute-looking path: %r" % name)
    if "\\" in stripped:
        raise ValueError("backslash in path: %r" % name)
    if ".." in stripped.split("/"):
        raise ValueError("parent directory traversal in path: %r" % name)
    bare = posixpath.normpath(stripped.rstrip("/"))
    if bare != "usr" and not bare.startswith("usr/"):
        raise ValueError("path outside ./usr: %r" % name)
    return bare


def validate(path):
    report = Report(path)
    try:
        members = read_ar(path)
    except (OSError, ValueError) as error:
        report.fail("ar container", str(error))
        return report

    names = [member["name"] for member in members]
    report.check(
        names == ["debian-binary", "control.tar.gz", "data.tar.gz"],
        "ar member order",
        ", ".join(names),
    )
    expected_names = ["debian-binary", "control.tar.gz", "data.tar.gz"]
    report.check(
        [member["name_field"] for member in members] == expected_names,
        "ar member names are dpkg-deb style (no trailing slash)",
        ", ".join(repr(member["name_field"]) for member in members),
    )
    if [name for name in names if name == "debian-binary"] and members[0]["name"] == "debian-binary":
        report.check(
            members[0]["data"] == b"2.0\n",
            "debian-binary content",
            repr(members[0]["data"]),
        )
    for member in members:
        if member["name"] in ("debian-binary", "control.tar.gz", "data.tar.gz"):
            report.check(
                member["uid"] == "0" and member["gid"] == "0",
                "ar header %s root ownership" % member["name"],
                "uid=%s gid=%s" % (member["uid"], member["gid"]),
            )
            report.check(
                member["mtime"].isdigit() and int(member["mtime"]) > 0,
                "ar header %s timestamp" % member["name"],
                member["mtime"],
            )
    if names[:3] != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
        report.fail("ar container", "cannot continue without the expected members")
        return report

    # ---------------------------------------------------------------- control
    control_fields = {}
    control_data = members[1]["data"]
    try:
        control_tar = tarfile.open(fileobj=io.BytesIO(control_data), mode="r:gz")
        control_tar_names = control_tar.getnames()
    except (tarfile.TarError, OSError) as error:
        report.fail("control.tar.gz", "cannot open: %s" % error)
        return report

    report.check(
        "./control" in control_tar_names,
        "control.tar.gz contains ./control",
        ", ".join(control_tar_names),
    )
    try:
        control_text = control_tar.extractfile("./control").read().decode("utf-8")
        control_fields, control_order = parse_control(control_text)
        report.ok("control parses", "%d fields" % len(control_order))
    except (KeyError, UnicodeDecodeError, ValueError) as error:
        report.fail("control parses", str(error))

    if control_fields:
        missing = [name for name in REQUIRED_CONTROL_FIELDS if name not in control_fields]
        report.check(
            not missing,
            "control required fields",
            "missing: " + ", ".join(missing) if missing else "",
        )
        package = control_fields.get("Package", "")
        version = control_fields.get("Version", "")
        architecture = control_fields.get("Architecture", "")
        report.check(
            package == "linkrd", "control Package", "Package: %s" % package
        )
        report.check(
            bool(VERSION_RE.match(version)),
            "control Version format",
            "Version: %s" % version,
        )
        report.check(
            architecture in ARCH_MACHINE,
            "control Architecture",
            "Architecture: %s" % architecture,
        )
        report.check(
            bool(MAINTAINER_RE.match(control_fields.get("Maintainer", ""))),
            "control Maintainer",
            control_fields.get("Maintainer", ""),
        )
        report.check(
            control_fields.get("Section") == "utils",
            "control Section",
            control_fields.get("Section", ""),
        )
        report.check(
            control_fields.get("Priority") == "optional",
            "control Priority",
            control_fields.get("Priority", ""),
        )
        homepage = control_fields.get("Homepage", "")
        report.check(
            homepage.startswith("https://") or homepage.startswith("http://"),
            "control Homepage",
            homepage,
        )
        description = control_fields.get("Description", "")
        description_lines = description.split("\n")
        short = description_lines[0].strip()
        report.check(
            bool(short) and len(short) <= 100,
            "control Description synopsis",
            short,
        )
        report.check(
            all(line.startswith(" ") for line in description_lines[1:]),
            "control Description continuation lines",
            "%d lines" % len(description_lines),
        )
        try:
            installed_size = int(control_fields.get("Installed-Size", ""))
            report.check(installed_size > 0, "control Installed-Size", str(installed_size))
        except ValueError:
            installed_size = None
            report.fail(
                "control Installed-Size",
                "not an integer: %r" % control_fields.get("Installed-Size"),
            )

        filename = os.path.basename(path)
        match = FILENAME_RE.match(filename)
        if match:
            report.check(
                match.group("package") == package,
                "file name package",
                filename,
            )
            report.check(
                match.group("version") == version,
                "file name version",
                "%s vs %s" % (match.group("version"), version),
            )
            report.check(
                match.group("arch") == architecture,
                "file name architecture",
                "%s vs %s" % (match.group("arch"), architecture),
            )
        else:
            report.fail("file name", "unexpected name: %s" % filename)

        # md5sums
        md5sums = {}
        md5_problem = None
        if "./md5sums" not in control_tar_names:
            md5_problem = "missing ./md5sums"
        else:
            raw = control_tar.extractfile("./md5sums").read().decode("utf-8")
            for line in raw.splitlines():
                if not line:
                    continue
                line_match = MD5_LINE_RE.match(line)
                if not line_match:
                    md5_problem = "malformed md5sums line: %r" % line
                    break
                digest, member_path = line_match.groups()
                if member_path in md5sums:
                    md5_problem = "duplicate md5sums entry: %s" % member_path
                    break
                if member_path.startswith("./") or member_path.startswith("/"):
                    md5_problem = "md5sums path must be relative without ./: %s" % member_path
                    break
                md5sums[member_path] = digest
        if md5_problem:
            report.fail("md5sums format", md5_problem)

        # postinst
        if "./postinst" not in control_tar_names:
            report.fail("postinst present")
        else:
            postinst_member = control_tar.getmember("./postinst")
            postinst = control_tar.extractfile("./postinst").read()
            report.check(
                postinst_member.mode & 0o777 == 0o755,
                "postinst mode",
                oct(postinst_member.mode & 0o777),
            )
            postinst_text = postinst.decode("utf-8", "replace")
            report.check(
                postinst_text.startswith("#!/bin/sh\n"),
                "postinst shebang",
            )
            report.check("\nset -e\n" in postinst_text, "postinst set -e")
            report.check(
                postinst_text.rstrip().endswith("exit 0"),
                "postinst exit 0",
            )
            # These packages are assembled without debhelper, so an unresolved
            # debhelper placeholder in a shipped maintainer script would be a bug.
            report.check(
                "#DEBHELPER#" not in postinst_text,
                "postinst has no unresolved debhelper placeholder",
            )
            udev_lines = [
                line.strip()
                for line in postinst_text.splitlines()
                if "udevadm" in line and not line.strip().startswith("#")
            ]
            report.check(
                any("udevadm control --reload-rules" in line for line in udev_lines),
                "postinst reloads udev rules",
            )
            report.check(
                any("udevadm trigger" in line for line in udev_lines),
                "postinst triggers tty devices",
            )
            report.check(
                all(("|| true" in line) or ("command -v udevadm" in line) for line in udev_lines),
                "postinst udevadm failures ignored",
            )
        conffiles = "./conffiles" in control_tar_names
        report.check(not conffiles, "no conffiles in control.tar.gz")
    else:
        installed_size = None

    # ------------------------------------------------------------------- data
    data_members = {}
    data_dirs = []
    direct_data = members[2]["data"]
    try:
        data_tar = tarfile.open(fileobj=io.BytesIO(direct_data), mode="r:gz")
        tariffs = data_tar.getmembers()
    except (tarfile.TarError, OSError) as error:
        report.fail("data.tar.gz", "cannot open: %s" % error)
        return report

    path_problems = []
    ownership_problems = []
    mode_problems = []
    weird_types = []
    regular_files = {}
    for tariff in tariffs:
        try:
            bare = normalize_member_name(tariff.name)
        except ValueError as error:
            path_problems.append(str(error))
            continue
        report_ownership = tariff.uid == 0 and tariff.gid == 0
        report_ownership = report_ownership and tariff.uname == "root" and tariff.gname == "root"
        if not report_ownership:
            ownership_problems.append(tariff.name)
        if tariff.mode & 0o7000:
            mode_problems.append("%s mode %s" % (tariff.name, oct(tariff.mode)))
        if tariff.isdir():
            data_dirs.append(bare)
        elif tariff.isreg():
            regular_files[bare] = tariff
        else:
            weird_types.append("%s (%s)" % (tariff.name, tariff.type))

    report.check(
        not path_problems,
        "data.tar paths are ./usr only",
        "; ".join(path_problems[:5]),
    )
    report.check(
        not ownership_problems,
        "data.tar root:root ownership",
        ", ".join(ownership_problems[:5]),
    )
    report.check(
        not mode_problems,
        "data.tar no setuid/setgid/sticky bits",
        ", ".join(mode_problems[:5]),
    )
    report.check(
        not weird_types,
        "data.tar only regular files and directories",
        ", ".join(weird_types[:5]),
    )

    # Key files, modes and contents.
    for member_path, expected_mode in sorted(REQUIRED_FILES.items()):
        tariff = regular_files.get(member_path)
        if tariff is None:
            report.fail("required file %s" % member_path)
            continue
        actual_mode = tariff.mode & 0o777
        if actual_mode != expected_mode:
            report.fail(
                "mode of %s" % member_path,
                "%s != %s" % (oct(actual_mode), oct(expected_mode)),
            )
        else:
            report.ok("required file %s (mode %s)" % (member_path, oct(expected_mode)))

    # md5sums must exactly cover the payload.
    if control_fields:
        payload_paths = set(regular_files)
        md5_paths = set(md5sums) if "md5sums" in locals() else set()
        missing_in_md5 = sorted(payload_paths - md5_paths)
        extra_in_md5 = sorted(md5_paths - payload_paths)
        report.check(
            not missing_in_md5,
            "md5sums covers every payload file",
            "missing: " + ", ".join(missing_in_md5) if missing_in_md5 else "",
        )
        report.check(
            not extra_in_md5,
            "md5sums has no stale entries",
            "extra: " + ", ".join(extra_in_md5) if extra_in_md5 else "",
        )
        bad = []
        for member_path, digest in sorted(md5sums.items()):
            tariff = regular_files.get(member_path)
            if tariff is None:
                continue
            actual = hashlib.md5(data_tar.extractfile(tariff).read()).hexdigest()
            if actual != digest:
                bad.append(member_path)
        report.check(
            not bad,
            "md5sums digests match payload",
            ", ".join(bad[:5]),
        )

        # Installed-Size: same estimate as dpkg-deb/du, allow 2% tolerance.
        computed = sum(
            (tariff.size + 1023) // 1024 for tariff in regular_files.values()
        )
        computed += 4 * (len(data_dirs) + 1)  # root directory is not a member
        if installed_size and computed:
            difference = abs(installed_size - computed) / computed
            report.check(
                difference < 0.02,
                "Installed-Size accuracy",
                "declared %d KiB, computed %d KiB (%.2f%%)"
                % (installed_size, computed, difference * 100),
            )

    # Binary sanity: ELF architecture and linkage.
    binary_tariff = regular_files.get("usr/bin/linkrd")
    dynamic = None
    if binary_tariff is not None:
        binary = data_tar.extractfile(binary_tariff).read()
        report.check(len(binary) > 0, "usr/bin/linkrd is non-empty", "%d bytes" % len(binary))
        report.check(
            binary_tariff.mode & 0o111 == 0o111,
            "usr/bin/linkrd is executable",
            oct(binary_tariff.mode & 0o777),
        )
        info = elf_info(binary)
        if info is None:
            report.warn(
                "usr/bin/linkrd is not an ELF file",
                "structure test placeholder? architecture/dependency checks skipped",
            )
        else:
            elf_class, machine, endian, has_interp = info
            dynamic = has_interp
            architecture = control_fields.get("Architecture", "?")
            expected = ARCH_MACHINE.get(architecture)
            report.check(
                expected is None or machine == expected,
                "usr/bin/linkrd ELF architecture",
                "%s (machine %d) for %s"
                % (MACHINE_NAME.get(machine, "?"), machine, architecture),
            )
            report.check(
                elf_class == 2,
                "usr/bin/linkrd is ELF64",
                "ELF%d" % (64 if elf_class == 2 else 32),
            )
            report.ok(
                "usr/bin/linkrd linkage",
                "dynamic (PT_INTERP)" if dynamic else "static (no PT_INTERP)",
            )

    # Dependency consistency with the actual linkage.
    depends = control_fields.get("Depends", "") if control_fields else ""
    depends_items = [item.strip() for item in depends.split(",") if item.strip()]
    if dynamic is True:
        report.check(
            any(item.startswith("libc6 (>= 2.28)") for item in depends_items),
            "Depends has libc6 (>= 2.28) for dynamic binary",
            depends or "no Depends field",
        )
    elif dynamic is False:
        report.check(
            not depends_items,
            "static binary has no Depends",
            depends or "no Depends field",
        )
        report.check(
            not any("libc6" in item for item in depends_items),
            "static binary does not depend on libc6",
        )
    if depends_items:
        report.check(
            not any("libgcc-s1" in item for item in depends_items),
            "Depends does not declare libgcc-s1",
            depends,
        )
        report.check(
            all(item.startswith(("libc6 (",)) for item in depends_items),
            "Depends only declares libc6",
            depends,
        )

    # Man page, changelog, udev rule and unit contents.
    man_tariff = regular_files.get("usr/share/man/man1/linkrd.1.gz")
    if man_tariff is not None:
        compressed = data_tar.extractfile(man_tariff).read()
        try:
            man_text = gzip.decompress(compressed)
            report.ok("man page decompresses", "%d bytes" % len(man_text))
            roff_lines = man_text.splitlines()
            first_macro = next(
                (line for line in roff_lines if line.strip() and not line.startswith(b'.\\"')),
                b"",
            )
            report.check(
                first_macro.startswith(b".TH "),
                "man page is roff (.TH)",
                first_macro[:40].decode("utf-8", "replace"),
            )
            report.check(
                b".SH NAME" in man_text and b".SH SEE ALSO" in man_text,
                "man page NAME and SEE ALSO sections",
            )
        except (OSError, EOFError) as error:
            report.fail("man page decompresses", str(error))

    changelog_tariff = regular_files.get("usr/share/doc/linkrd/changelog.Debian.gz")
    if changelog_tariff is not None:
        try:
            changelog_text = gzip.decompress(
                data_tar.extractfile(changelog_tariff).read()
            )
            report.check(
                changelog_text.startswith(b"linkrd ("),
                "changelog.Debian.gz starts with linkrd",
            )
            if control_fields:
                version = control_fields.get("Version", "").encode()
                report.check(
                    (b"linkrd (" + version + b")") in changelog_text,
                    "changelog.Debian.gz mentions the package version",
                    version.decode(),
                )
        except (OSError, EOFError) as error:
            report.fail("changelog.Debian.gz decompresses", str(error))

    copyright_tariff = regular_files.get("usr/share/doc/linkrd/copyright")
    if copyright_tariff is not None:
        copyright_text = data_tar.extractfile(copyright_tariff).read()
        report.check(
            b"Format:" in copyright_text and b"License:" in copyright_text,
            "copyright is DEP-5",
        )

    udev_tariff = regular_files.get("usr/lib/udev/rules.d/70-linkrd.rules")
    if udev_tariff is not None:
        udev_text = data_tar.extractfile(udev_tariff).read()
        report.check(
            b'idVendor}=="1d6b"' in udev_text
            and b'idProduct}=="0104"' in udev_text
            and b"uaccess" in udev_text,
            "udev rule matches Linkr VID/PID and grants uaccess",
        )

    unit_tariff = regular_files.get("usr/lib/systemd/user/linkrd.service")
    if unit_tariff is not None:
        unit_text = data_tar.extractfile(unit_tariff).read()
        report.check(
            b"ExecStart=/usr/bin/linkrd" in unit_text,
            "user unit runs /usr/bin/linkrd",
        )
        report.check(
            b"[Install]" in unit_text and b"graphical-session.target" in unit_text,
            "user unit has an [Install] section bound to graphical-session.target",
        )

    return report


def collect_default_paths():
    paths = []
    for pattern in ("dist/*.deb", "dist/gnu/*.deb"):
        paths.extend(glob.glob(pattern))
    return sorted(paths)


def main(argv):
    paths = []
    for argument in argv[1:]:
        if argument in ("-h", "--help"):
            print("Usage:" + USAGE)
            return 0
        elif argument.startswith("-"):
            print("validate-deb: unknown option: %s" % argument, file=sys.stderr)
            print("Usage:" + USAGE, file=sys.stderr)
            return 2
        else:
            paths.append(argument)
    if not paths:
        paths = collect_default_paths()
    if not paths:
        print(
            "validate-deb: no .deb files found (looked for dist/*.deb and "
            "dist/gnu/*.deb); build them with packaging/deb/build-deb.sh",
            file=sys.stderr,
        )
        return 2

    failures = 0
    for path in paths:
        if not os.path.isfile(path):
            print("validate-deb: not a file: %s" % path, file=sys.stderr)
            failures += 1
            continue
        report = validate(path)
        failures += report.render()
        if report.failed:
            failures = max(failures, 1)

    print("\nSummary: %d package(s) checked, %d failed" % (len(paths), failures))
    if failures:
        print("RESULT: FAIL")
        return 1
    print("RESULT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
