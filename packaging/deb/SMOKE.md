SMOKE checks that need a real Linux host
========================================

The packaging host is macOS and has no dpkg, dpkg-deb, lintian, udev or
systemd, so `.deb` generation and `validate-deb.py` only prove the container
structure.  The checks below still need a real Debian/Ubuntu machine (a VM,
container, qemu system or actual hardware).  Suggested minimum: Debian 13
(amd64) plus an arm64 target for the arm64 packages.

Legend: [ ] not yet verified, [x] done on a Linux host with the result noted.

dpkg
----

[ ] `dpkg-deb --info dist/linkrd_0.1.0-1_amd64.deb` shows Package, Version
    0.1.0-1, Architecture amd64, Section utils, Priority optional,
    Installed-Size, Maintainer, Homepage and the description; there is no
    Depends field for the static package.
[ ] `dpkg-deb --contents ... | sort` matches the file list in
    packaging/deb/README.md; all owner/group are root/root and the binary is
    `-rwxr-xr-x`.
[ ] `dpkg -i dist/linkrd_0.1.0-1_amd64.deb` on a clean system succeeds and
    `dpkg -s linkrd` reports `Status: install ok installed`.
[ ] `dpkg -L linkrd` lists /usr/bin/linkrd,
    /usr/lib/udev/rules.d/70-linkrd.rules,
    /usr/lib/systemd/user/linkrd.service and the man page.
[ ] Installing the gnu candidate on Debian 13 pulls in libc6 only (no
    libgcc-s1 requirement in practice; check `dpkg -I .../gnu/*.deb` shows
    `Depends: libc6 (>= 2.28)`).
[ ] Install in a Docker container without udevadm; the postinst must still
    exit 0 (`dpkg -i` succeeds).
[ ] Upgrade (install 0.1.0-1 twice, or a rebuilt package) and purge leave no
    orphaned files; purge does not touch ~/.local/share/linkrd.

lintian
-------

[ ] `lintian dist/linkrd_0.1.0-1_amd64.deb` reports no errors.  Expected
    hints to review: no-manual-page? (none), package-contains-no-arch-dependent-files?
    (no), maintainer-script-* (postinst only uses guarded udevadm calls).
[ ] `lintian --pedantic` on the source package after
    `dpkg-buildpackage -us -uc -b`: the only expected blocker is the
    `copyright-file-unknown-license` style tag for the missing upstream
    LICENSE (documented in debian/copyright).

systemd user unit
-----------------

[ ] `systemd-analyze --user verify /usr/lib/systemd/user/linkrd.service`
    reports no warnings.
[ ] As the desktop user: `systemctl --user daemon-reload &&
    systemctl --user enable --now linkrd.service` starts the unit;
    `systemctl --user status linkrd.service` is active (running) and
    `journalctl --user -u linkrd.service` shows the "linkrd starting" record.
[ ] The unit is NOT enabled by the package installation (fresh install:
    `systemctl --user is-enabled linkrd.service` returns "disabled").
[ ] Disabling the unit stops the agent; it is not restarted at the next
    login.
[ ] A graphical-session.target dependency behaves as expected: stop/restart
    of the user session stops and restarts linkrd.

udev / device access
--------------------

[ ] `udevadm verify /usr/lib/udev/rules.d/70-linkrd.rules` (udev >= 250) or
    `udevadm test /sys/class/tty/ttyACM0` parses the rule without warnings.
[ ] With a Linkr device plugged in, `getfacl /dev/ttyACM0` shows the
    `user:<desktop-user>:rw-` seat ACL from TAG+="uaccess"; no dialout group
    membership is needed on a systemd-logind system.
[ ] On a seat-ACL-less system the fallback applies: device is root:dialout
    mode 0660 and a dialout member can read/write it.
[ ] Re-plugging does not need a manual `udevadm trigger` after the postinst
    ran (the postinst reloads rules only once).

Application behaviour (real hardware)
-------------------------------------

[ ] `linkrd --list-ports` lists the Linkr /dev/ttyACM* port and the man page
    output renders (`man linkrd`, `linkrd --help`).
[ ] Foreground `linkrd --port /dev/ttyACM0 --log-level debug` connects,
    reports "ACM connected; waiting for Hello", and clipboard changes
    round-trip with the Linkr host (Wayland session and X11 session).
[ ] `linkrd install --autostart` is not needed and, per debian/README.debian,
    must not be used on the packaged system; a manual install followed by
    `linkrd uninstall` leaves a clean ~/.local/bin and ~/.config/autostart.
[ ] `systemctl --user restart linkrd.service` after a package upgrade does not
    leave two instances (runtime.lock).
[ ] Log rotation: force >10 MiB of logs and observe linkrd.log.1 ...
    linkrd.log.6 (7 files max) in ~/.local/share/linkrd/logs.

Architecture / linkage
----------------------

[ ] arm64 package: `dpkg -i dist/linkrd_0.1.0-1_arm64.deb` on arm64 (or
    qemu-aarch64-static in a chroot) works, `file /usr/bin/linkrd` says
    "ELF 64-bit LSB executable, ARM aarch64, statically linked".
[ ] amd64 static package: `file`/`readelf -l` show "statically linked" and no
    PT_INTERP; the binary runs on a musl system (Alpine) as well.
[ ] gnu candidates: `ldd /usr/bin/linkrd` shows libc.so.6 and no
    libgcc_s.so.1 requirement; runs on glibc 2.28+.

Source package
--------------

[ ] On Debian 13 with network access (or a populated CARGO_HOME/vendor layer):
    `dpkg-buildpackage -us -uc -b` succeeds; `debian/rules clean` removes
    target/ and debian/cargo-home.
[ ] debhelper 13.14+ installs the udev rule to /usr/lib/udev/rules.d; on
    Debian 12 (debhelper 13.11) it lands in /lib/udev/rules.d, which must be
    the same directory through the merged-/usr symlink (verify with
    `dpkg -L linkrd`).
[ ] `DEB_BUILD_OPTIONS=nocheck nostrip dpkg-buildpackage -us -uc -b` builds
    without running tests and keeps the symbols.
[ ] `uscan --report` with debian/watch: upstream has no release tags yet, so
    a failure here is expected until upstream tags v0.1.0.
[ ] `dpkg-source -x`/`dpkg-buildpackage` on the source package: the
    debian/linkrd.udev and debian/linkrd.user.service symlinks to contrib/
    resolve inside the packed source tree.

Reproducibility
---------------

[ ] Build twice with the same SOURCE_DATE_EPOCH and compare
    `sha256sum dist/*.deb` (the macOS build already produces identical bytes;
    re-check after the Debian source build as well).
