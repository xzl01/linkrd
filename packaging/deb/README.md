packaging/deb -- prebuilt Debian/Ubuntu packages
===============================================

This directory builds ready-to-install binary packages from the prebuilt Linux
executables in `.build/artifacts/` without dpkg/dpkg-deb (the packaging host is
macOS).  The Debian source package in `debian/` is independent: it builds from
the upstream tree on a Debian/Ubuntu machine with debhelper 13 and the cargo
build dependencies.

Files
-----

  build-deb.sh     build linkrd_0.1.0-1_<arch>.deb from a prebuilt executable
  validate-deb.py  structural validator; exits non-zero on any failed check
  SMOKE.md         checks that need a real Linux host (dpkg, lintian, systemd,
                   udev, real USB hardware)

Quick start
-----------

  # Build the official (musl static) packages from .build/artifacts/:
  ./packaging/deb/build-deb.sh

  # Validate every package below dist/ and dist/gnu/:
  python3 packaging/deb/validate-deb.py

Output:

  dist/linkrd_0.1.0-1_amd64.deb     static musl, no Depends
  dist/linkrd_0.1.0-1_arm64.deb     static musl, no Depends

Optional glibc-linked candidates (not built by default) go to dist/gnu/:

  ./packaging/deb/build-deb.sh \
      --binary .build/artifacts/linkrd-linux-amd64-gnu --arch amd64 --kind gnu \
      --binary .build/artifacts/linkrd-linux-arm64-gnu --arch arm64 --kind gnu

  dist/gnu/linkrd_0.1.0-1_amd64.deb      Depends: libc6 (>= 2.28)
  dist/gnu/linkrd_0.1.0-1_arm64.deb      Depends: libc6 (>= 2.28)

The static packages are the ones to distribute: they have no libc
dependency and run on any glibc/musl distribution.  The gnu candidates are
only for testing on hosts with glibc >= 2.28.

Explicit --binary/--arch/--kind combinations
--------------------------------------------

  # One package for a test build, into a scratch directory:
  ./packaging/deb/build-deb.sh \
      --binary /tmp/linkrd-linux-amd64 --arch amd64 --kind static \
      --outdir /tmp/debs

  # A non-ELF placeholder is accepted for structural tests (a WARNING is
  # printed and the validator reports a WARN instead of an ELF check):
  cp /bin/ls /tmp/placeholder
  ./packaging/deb/build-deb.sh --binary /tmp/placeholder --arch amd64 \
      --outdir /tmp/debs

`--binary` may be repeated; each one needs an `--arch`.  `--kind` defaults to
`static`.  Without `--binary`, `.build/artifacts/linkrd-linux-<arch>[-gnu]`
is discovered automatically (x86_64/aarch64 spellings are also accepted);
`--prefer gnu` changes which candidate wins if both exist.

Package contents (all modes 0644 except the binary)
---------------------------------------------------

  /usr/bin/linkrd                                      0755
  /usr/lib/udev/rules.d/70-linkrd.rules                0644
  /usr/lib/systemd/user/linkrd.service                 0644
  /usr/share/man/man1/linkrd.1.gz                      0644
  /usr/share/doc/linkrd/README.md                      0644
  /usr/share/doc/linkrd/README.zh-CN.md                0644
  /usr/share/doc/linkrd/README.debian                  0644
  /usr/share/doc/linkrd/changelog.Debian.gz            0644
  /usr/share/doc/linkrd/copyright                      0644

There is no conffile and no file below /etc.  The package does not enable or
start the systemd user unit; that stays a per-user decision (see
debian/README.debian).  The postinst only reloads the udev rules when udevadm
is available and ignores failures, so installation works in containers and
chroots.

Metadata
--------

The maintainer scripts are embedded verbatim by build-deb.sh:
`./postinst` contains `#!/bin/sh`, `set -e`, the two guarded `udevadm` calls
and `exit 0` (no debhelper placeholder: these packages are assembled without
debhelper, and validate-deb.py asserts that no `#DEBHELPER#` remnant ships).  `./md5sums` covers exactly every regular file
in data.tar.gz (sorted by path, no `./` prefix).  `Installed-Size` uses the
same estimate as du/dpkg-deb (per-file KiB rounded up plus 4 KiB per
directory); the validator allows a 2% difference.  The version is taken from
the top entry of debian/changelog (`0.1.0-1`).

Determinism
-----------

All mtimes come from SOURCE_DATE_EPOCH (default: the changelog date
2026-10-27T11:30:00+08:00), tar members are sorted, gzip uses level 9 with a
fixed timestamp and no original file name, and ar headers use fixed root
ownership and modes.  Two builds from the same inputs produce byte-identical
.deb files; set SOURCE_DATE_EPOCH to reproduce the timestamp explicitly.

Validator
---------

`validate-deb.py [package.deb ...]` checks the ar member order and
debian-binary content, control fields and their consistency with the file name,
Installed-Size accuracy, md5sums coverage and digests, data.tar path/owner/mode
rules, the key files and their contents (udev rule VID/PID, user unit
ExecStart, DEP-5 copyright, gzip man page and changelog), the ELF architecture
and static/dynamic linkage of the binary and the dependency declaration that
must match it.  It prints one PASS/FAIL line per check, a per-package summary
and exits 1 if anything failed (2 if no package was found).
