#!/usr/bin/env python3
"""lint-aur.py — linkrd AUR 包的静态校验。

在 macOS/Linux 上做尽可能多的静态检查：PKGBUILD 语法、变量/数组提取、
pkgdesc/arch/source/依赖约束、安装路径、install 脚本约束、.SRCINFO 与
PKGBUILD 的逐字段一致性等。

它**不能**替代 Arch Linux 上的真实验证：
  * `makepkg` / `makepkg --printsrcinfo`（真实构建、源校验和、构建环境）
  * `namcap`（包内容、依赖、文件权限检查）
  * `pacman -U`（安装/升级/卸载脚本的实际行为）

用法:
  python3 packaging/aur/lint-aur.py              # 校验全部包
  python3 packaging/aur/lint-aur.py linkrd       # 只校验一个包
  python3 packaging/aur/lint-aur.py --strict     # 把 WARN 也当作失败

退出码: 0 = 没有 FAIL；1 = 存在 FAIL。
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.normpath(os.path.join(ROOT, "..", ".."))
ALL_PACKAGES = ("linkrd", "linkrd-git")

# makepkg --printsrcinfo 的字段顺序（见 pacman scripts/libmakepkg/srcinfo.sh）。
CANONICAL_GLOBAL = [
    "pkgbase", "pkgdesc", "pkgver", "pkgrel", "epoch", "url", "install",
    "changelog", "arch", "groups", "license", "checkdepends", "makedepends",
    "depends", "optdepends", "provides", "conflicts", "replaces", "noextract",
    "options", "backup", "source", "validpgpkeys", "sha256sums",
]
CANONICAL_PKGNAME = [
    "pkgname", "pkgdesc", "url", "install", "changelog", "arch", "groups",
    "license", "checkdepends", "depends", "optdepends", "provides",
    "conflicts", "replaces", "options", "backup",
]
SCALAR_FIELDS = {
    "pkgbase", "pkgname", "pkgver", "pkgrel", "epoch", "pkgdesc", "url",
    "install", "changelog",
}

ALLOWED_ARCH = {
    "any", "i686", "x86_64", "pentium4", "arm", "armv7h", "armv6h",
    "aarch64", "riscv64",
}
# makepkg 支持的 options 值（+ 前缀形式在 PKGBUILD 里写成 !xxx）。
KNOWN_OPTIONS = {
    "strip", "!strip", "debug", "!debug", "lto", "!lto", "emptydirs",
    "!emptydirs", "zipman", "!zipman", "purge", "!purge", "check", "!check",
    "docs", "!docs", "libtool", "!libtool", "staticlibs", "!staticlibs",
    "upx", "!upx", "ccache", "!ccache", "buildflags", "!buildflags",
    "makeflags", "!makeflags", "optipng", "!optipng",
}
# linkrd 的 wayland/x11 后端都是纯 Rust 实现，运行时不得依赖这些系统库。
FORBIDDEN_RUNTIME_LIBS = {
    "wayland", "libwayland", "libwayland-client", "libx11", "libxcb",
    "libxkbcommon", "xorg-xrandr",
}
REQUIRED_RUNTIME_DEPS = {"gcc-libs"}
REQUIRED_MAKEDEPS = {"cargo", "rust"}

# 将 PKGBUILD 以 bash 变量/函数的形式导出（NUL 分隔记录）。
EXTRACT_SCRIPT = r"""
set -u
if ! source ./PKGBUILD >/dev/null 2>&1; then
  printf 'SOURCE_ERROR\000'
  exit 1
fi
for var in pkgbase pkgname pkgver pkgrel epoch pkgdesc url license arch \
           groups checkdepends makedepends depends optdepends provides \
           conflicts replaces options backup source noextract validpgpkeys \
           sha256sums install changelog; do
  if declare -p "$var" >/dev/null 2>&1; then
    if [[ $(declare -p "$var") == declare\ -a* || $(declare -p "$var") == declare\ -A* ]]; then
      printf 'D\000%s\000array\000' "$var"
      eval "vals=(\"\${$var[@]}\")"
      for v in "${vals[@]}"; do
        printf 'V\000%s\000%s\000' "$var" "$v"
      done
      unset vals
    else
      printf 'D\000%s\000scalar\000' "$var"
      printf 'V\000%s\000%s\000' "$var" "${!var}"
    fi
  fi
done
for fn in pkgver prepare build check package; do
  if declare -F "$fn" >/dev/null 2>&1; then
    printf 'F\000%s\000' "$fn"
  fi
done
"""


class Report:
    def __init__(self, title: str) -> None:
        self.title = title
        self.lines: list[tuple[str, str]] = []

    def pass_(self, msg: str) -> None:
        self.lines.append(("PASS", msg))

    def fail(self, msg: str) -> None:
        self.lines.append(("FAIL", msg))

    def warn(self, msg: str) -> None:
        self.lines.append(("WARN", msg))

    def check(self, cond: bool, msg: str, detail: str = "") -> bool:
        if cond:
            self.pass_(msg)
        else:
            self.fail(msg + ("（%s）" % detail if detail else ""))
        return cond

    def counts(self) -> tuple[int, int, int]:
        p = sum(1 for s, _ in self.lines if s == "PASS")
        w = sum(1 for s, _ in self.lines if s == "WARN")
        f = sum(1 for s, _ in self.lines if s == "FAIL")
        return p, w, f

    def print(self) -> None:
        print("== %s ==" % self.title)
        for status, msg in self.lines:
            print("  %-4s %s" % (status, msg))
        p, w, f = self.counts()
        print("  --> PASS %d, WARN %d, FAIL %d" % (p, w, f))
        print()


def run(cmd: list[str], cwd: str) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)


# ---------------------------------------------------------------------------
# PKGBUILD / 文件解析
# ---------------------------------------------------------------------------

def extract_pkgbuild(directory: str) -> tuple[dict, dict, set, str | None]:
    """返回 (declared, values, functions, error)。"""
    proc = run(["bash", "-c", EXTRACT_SCRIPT], directory)
    if proc.returncode != 0:
        return {}, {}, set(), proc.stdout + proc.stderr
    parts = proc.stdout.split("\0")
    declared: dict[str, str] = {}
    values: dict[str, list[str]] = {}
    functions: set[str] = set()
    i = 0
    while i < len(parts):
        tag = parts[i]
        if tag == "SOURCE_ERROR":
            return {}, {}, set(), "source ./PKGBUILD 失败"
        if tag == "D":
            declared[parts[i + 1]] = parts[i + 2]
            values.setdefault(parts[i + 1], [])
            i += 3
        elif tag == "V":
            values.setdefault(parts[i + 1], []).append(parts[i + 2])
            i += 3
        elif tag == "F":
            functions.add(parts[i + 1])
            i += 2
        elif tag == "":
            i += 1
        else:  # 意外输出（例如 PKGBUILD 顶层 echo），忽略
            i += 1
    return declared, values, functions, None


def parse_srcinfo(path: str) -> tuple[list, str | None]:
    """解析 .SRCINFO，返回 [(section, [(key, value), ...]), ...]。"""
    with open(path, "r", encoding="utf-8") as fh:
        text = fh.read()
    sections: list[list] = []
    current: list | None = None
    for lineno, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        if line.startswith("#"):
            continue
        loc = re.match(r"^([a-zA-Z0-9_]+) = (.*)$", line)
        if loc:
            current = [loc.group(1), []]
            sections.append(current)
            continue
        attr = re.match(r"^\t([a-zA-Z0-9_]+) = (.*)$", line)
        if attr and current is not None:
            current[1].append((attr.group(1), attr.group(2)))
            continue
        return [], "第 %d 行无法解析: %r" % (lineno, line)
    return sections, None


def function_body(text: str, func: str) -> str | None:
    match = re.search(r"(?m)^" + re.escape(func) + r"\(\)\s*\{", text)
    if not match:
        return None
    i = match.end()
    depth = 1
    while i < len(text) and depth:
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
        i += 1
    return text[match.end():i - 1]


def logical_lines(body: str) -> list[str]:
    """把以反斜杠续行的 shell 片段合并成逻辑行。"""
    lines: list[str] = []
    pending = ""
    for raw in body.splitlines():
        line = raw.rstrip()
        if pending:
            line = pending + " " + line.lstrip()
            pending = ""
        if line.endswith("\\"):
            pending = line[:-1]
            continue
        lines.append(line)
    if pending:
        lines.append(pending)
    return lines


def strip_heredoc(text: str) -> list[tuple[int, str]]:
    """返回不在 heredoc 内、非纯注释的行。"""
    out: list[tuple[int, str]] = []
    heredoc: str | None = None
    for lineno, raw in enumerate(text.splitlines(), 1):
        stripped = raw.strip()
        if heredoc is not None:
            if stripped == heredoc:
                heredoc = None
            continue
        match = re.search(r"<<-?\s*['\"]?([A-Za-z_][A-Za-z0-9_]*)['\"]?", raw)
        if match:
            heredoc = match.group(1)
            out.append((lineno, stripped))
            continue
        if not stripped or stripped.startswith("#"):
            continue
        out.append((lineno, stripped))
    return out


# ---------------------------------------------------------------------------
# 单个包
# ---------------------------------------------------------------------------

class PackageLint:
    def __init__(self, name: str, directory: str) -> None:
        self.name = name
        self.dir = directory
        self.report = Report(name)
        self.raw = ""
        self.declared: dict[str, str] = {}
        self.values: dict[str, list[str]] = {}
        self.functions: set[str] = set()
        self.is_vcs = name.endswith("-git")

    # -- 小工具 ---------------------------------------------------------
    def scalar(self, field: str) -> str:
        vals = self.values.get(field, [])
        return vals[0] if vals else ""

    def array(self, field: str) -> list[str]:
        return list(self.values.get(field, []))

    def pkgbuild_body(self, func: str) -> str | None:
        return function_body(self.raw, func)

    def expand(self, text: str) -> str:
        text = text.replace("${pkgname}", self.scalar("pkgname"))
        text = text.replace("$pkgname", self.scalar("pkgname"))
        text = text.replace("${pkgver}", self.scalar("pkgver"))
        text = text.replace("$pkgver", self.scalar("pkgver"))
        return text

    # -- 入口 -----------------------------------------------------------
    def run(self) -> Report:
        if not self.check_files():
            self.report.print()
            return self.report
        self.check_syntax()
        error = self.load()
        if error or not self.declared:
            self.report.print()
            return self.report
        self.check_fields()
        self.check_functions()
        self.check_install_script()
        self.check_shellcheck()
        self.check_srcinfo()
        self.report.print()
        return self.report

    # -- 文件与语法 -----------------------------------------------------
    def check_files(self) -> bool:
        r = self.report
        pkgbuild = os.path.join(self.dir, "PKGBUILD")
        srcinfo = os.path.join(self.dir, ".SRCINFO")
        if not r.check(os.path.isfile(pkgbuild), "PKGBUILD 存在"):
            return False
        r.check(os.path.isfile(srcinfo), ".SRCINFO 存在")
        with open(pkgbuild, "r", encoding="utf-8") as fh:
            self.raw = fh.read()
        if "\r\n" in self.raw:
            r.fail("PKGBUILD 使用 CRLF 换行（Arch 上应为 LF）")
        extra = sorted(
            f for f in os.listdir(self.dir)
            if f not in ("PKGBUILD", ".SRCINFO") and not f.endswith(".install")
        )
        if extra:
            r.warn("目录中存在额外的非 AUR 文件: %s" % ", ".join(extra))
        return True

    def check_syntax(self) -> None:
        result = run(["bash", "-n", "PKGBUILD"], self.dir)
        self.report.check(
            result.returncode == 0, "PKGBUILD 语法（bash -n）",
            result.stderr.strip() or "",
        )

    def load(self) -> str | None:
        declared, values, functions, error = extract_pkgbuild(self.dir)
        if error:
            self.report.fail("PKGBUILD 变量提取失败: %s" % error.strip())
            return error
        self.declared, self.values, self.functions = declared, values, functions
        self.report.pass_("PKGBUILD 可被 bash source（变量/数组提取成功）")
        return None

    # -- 字段 -----------------------------------------------------------
    def check_fields(self) -> None:
        r = self.report
        required_scalars = ["pkgname", "pkgver", "pkgrel", "pkgdesc", "url"]
        for field in required_scalars:
            r.check(bool(self.scalar(field)), "必需字段 %s 存在且非空" % field
                    if not self.scalar(field) else "%s = %s"
                    % (field, self.scalar(field)))
        required_arrays = ["arch", "license", "depends", "makedepends",
                           "source", "sha256sums"]
        for field in required_arrays:
            r.check(bool(self.array(field)), "必需数组 %s 非空" % field)

        pkgname = self.scalar("pkgname")
        r.check(pkgname == self.name,
                "pkgname == 目录名（%s）" % self.name, pkgname)
        r.check(bool(re.match(r"^[a-z0-9][a-z0-9+._@-]*$", pkgname)),
                "pkgname 符合 Arch 命名规则", pkgname)

        pkgver = self.scalar("pkgver")
        r.check(bool(re.match(r"^[0-9][A-Za-z0-9._+]*$", pkgver)),
                "pkgver 合法（以数字开头，不含 - 或 :）", pkgver)
        r.check(bool(re.match(r"^[1-9][0-9]*$", self.scalar("pkgrel"))),
                "pkgrel 为正整数", self.scalar("pkgrel"))

        pkgdesc = self.scalar("pkgdesc")
        r.check(0 < len(pkgdesc) <= 80, "pkgdesc 长度 1..80（实际 %d）" % len(pkgdesc))
        r.check(not pkgdesc.lower().startswith(pkgname.lower()),
                "pkgdesc 不以包名开头", pkgdesc)
        r.check(pkgdesc == pkgdesc.strip(), "pkgdesc 无首尾空白")

        url = self.scalar("url")
        r.check(bool(re.match(r"^https?://", url)), "url 是 http(s) URL", url)

        arch = self.array("arch")
        unknown_arch = [a for a in arch if a not in ALLOWED_ARCH]
        r.check(not unknown_arch, "arch 取值合法", ", ".join(unknown_arch))
        r.check("aarch64" in arch or "any" in arch,
                "arch 已覆盖 aarch64（Radxa Linkr 的 arm64 场景）")

        license_ = self.array("license")
        if "unknown" in license_:
            r.warn("license=('unknown')：上游缺 LICENSE/Cargo license 字段，"
                   "发布前需上游补 LICENSE 并改成 SPDX 标识")
        else:
            r.pass_("license 已填写: %s" % ", ".join(license_))

        depends = self.array("depends")
        r.check(REQUIRED_RUNTIME_DEPS.issubset(set(depends)),
                "depends 含 gcc-libs（Rust 二进制动态链接 libgcc_s）",
                ", ".join(depends))
        forbidden = sorted(set(depends) & FORBIDDEN_RUNTIME_LIBS)
        r.check(not forbidden,
                "depends 未错误声明 wayland/x11 库（后端为纯 Rust 实现）",
                ", ".join(forbidden))

        makedepends = self.array("makedepends")
        r.check(REQUIRED_MAKEDEPS.issubset(set(makedepends)),
                "makedepends 含 cargo 与 rust", ", ".join(makedepends))
        if self.is_vcs:
            r.check("git" in makedepends, "VCS 包的 makedepends 含 git",
                    ", ".join(makedepends))

        # install 脚本
        install = self.scalar("install")
        if r.check(bool(install), "声明了 install 安装脚本"):
            r.check(install.endswith(".install"), "install 字段以 .install 结尾",
                    install)
            install_path = os.path.join(self.dir, install)
            exists = os.path.isfile(install_path)
            r.check(exists, "install 引用的脚本存在: %s" % install)
            if exists:
                mode = os.stat(install_path).st_mode
                if mode & 0o111:
                    r.warn("%s 带可执行位（pacman 不要求，644 即可）" % install)

        # source 与校验和
        source, sums = self.array("source"), self.array("sha256sums")
        r.check(len(source) == len(sums),
                "source 与 sha256sums 数量一致（%d vs %d）"
                % (len(source), len(sums)))
        unexpanded = [s for s in source if "$" in s or "`" in s]
        r.check(not unexpanded, "source 内变量已全部展开",
                "; ".join(unexpanded))
        bad_sums = [s for s in sums
                    if s != "SKIP" and not re.match(r"^[0-9a-f]{64}$", s)]
        r.check(not bad_sums, "sha256sums 均为 64 位小写十六进制或 SKIP",
                ", ".join(bad_sums))
        if self.is_vcs:
            r.check(bool(source) and source[0].startswith("git+"),
                    "VCS 包 source 使用了 git+ 协议",
                    source[0] if source else "")
            r.check(bool(source) and source[0].endswith(".git"),
                    "VCS 包 source 指向 .git 仓库",
                    source[0] if source else "")
            r.check(sums == ["SKIP"], "VCS 包 sha256sums=('SKIP')",
                    ", ".join(sums))
        else:
            if "SKIP" in sums:
                r.warn("sha256sums=('SKIP')：上游尚未发布 tag，"
                       "发布前必须替换为真实 sha256（updpkgsums）")
            else:
                r.pass_("stable 包已填写真实 sha256sums")
            if source:
                r.check("::" in source[0] and
                        source[0].split("::", 1)[0] ==
                        "%s-%s.tar.gz" % (self.name, pkgver),
                        "source 文件名符合 $pkgname-$pkgver.tar.gz",
                        source[0])
                r.check("/archive/v%s.tar.gz" % pkgver in source[0],
                        "source 使用上游 tag archive URL（v%s）" % pkgver,
                        source[0])

        # options / backup
        options = self.array("options")
        bad_options = [o for o in options if o not in KNOWN_OPTIONS]
        r.check(not bad_options, "options 均为 makepkg 支持的取值",
                ", ".join(bad_options))
        r.check(not any(re.search(r"-O[0-9]", o) for o in options),
                "options 未用 -O3 之类的优化参数覆盖 Cargo flags")
        backup = self.array("backup")
        bad_backup = [b for b in backup if b.startswith("/")]
        r.check(not bad_backup, "backup 路径为相对路径", ", ".join(bad_backup))

        # 不得覆盖 Cargo 的构建参数
        rustflags = [
            line for line in self.raw.splitlines()
            if re.search(r"(?<!#)\bRUSTFLAGS\s*=", line)
        ]
        r.check(not rustflags,
                "未用 RUSTFLAGS 覆盖 Cargo 构建参数", "; ".join(rustflags))

        if "Maintainer: TODO" in self.raw:
            r.warn("Maintainer 行仍是 TODO 占位符，提交 AUR 前必须替换")

    # -- 函数 -----------------------------------------------------------
    def check_functions(self) -> None:
        r = self.report
        funcs = self.functions

        for func in ("prepare", "build", "package"):
            if func not in funcs:
                r.fail("缺少必需函数 %s()" % func)
        if "prepare" in funcs:
            body = self.pkgbuild_body("prepare") or ""
            r.check("cargo fetch" in body and "--locked" in body,
                    "prepare() 使用 cargo fetch --locked")
            r.check("CARGO_HOME" in body,
                    "prepare() 中设置 CARGO_HOME（与 build 一致，可离线）")
            r.check("CARGO_NET_OFFLINE=false" in body,
                    "prepare() 显式允许联网抓取依赖")
        if "build" in funcs:
            body = self.pkgbuild_body("build") or ""
            r.check("cargo build" in body and "--release" in body,
                    "build() 使用 cargo build --release")
            r.check("--locked" in body, "build() 使用 --locked")
            r.check("--frozen" in body or "--offline" in body,
                    "build() 使用 --frozen/--offline（依赖在 prepare() 抓取）")
            r.check("CARGO_HOME" in body,
                    "build() 中设置与 prepare() 一致的 CARGO_HOME")
        if "check" in funcs:
            body = self.pkgbuild_body("check") or ""
            r.check("cargo test" in body, "check() 使用 cargo test（可选，已启用）")
            r.check("--locked" in body, "check() 使用 --locked")
            r.pass_("check() 测试为 hermetic（临时 HOME/XDG、linux-only）；"
                    "环境不支持时可用 makepkg --nocheck 跳过")
        else:
            r.warn("未提供 check()（可接受；说明见 README 的未验证项）")

        if self.is_vcs:
            r.check("pkgver" in funcs, "-git 包提供 pkgver()")
            body = self.pkgbuild_body("pkgver") or ""
            r.check("git describe" in body and "--tags" in body,
                    "pkgver() 用 git describe --tags 生成版本")
            r.check("git rev-list" in body and "git rev-parse" in body,
                    "pkgver() 对“上游无 tag”情况有回退逻辑")
            r.check("git describe" in body and "sed " in body,
                    "pkgver() 把 vX.Y.Z-N-gHASH 转成 makepkg 版本格式")
            r.check('cd "$srcdir' in body,
                    "pkgver() 进入 makepkg 的 git checkout 目录"
                    "（仓库名为 linkrd，而不是 $pkgname）")
            r.warn("pkgver= 只是占位值；提交 AUR 前先 `makepkg -o` 让 pkgver() "
                   "更新，再重新生成 .SRCINFO")
            provides, conflicts = self.array("provides"), self.array("conflicts")
            r.check("linkrd" in provides, "-git 包 provides=('linkrd')",
                    ", ".join(provides))
            r.check("linkrd" in conflicts, "-git 包 conflicts=('linkrd')",
                    ", ".join(conflicts))
        else:
            r.check("pkgver" not in funcs,
                    "stable 包不使用 pkgver()（源码来自 tag tarball）")
            if self.array("provides") or self.array("conflicts"):
                r.warn("stable 包声明了 provides/conflicts，请确认确有必要")

        body = self.pkgbuild_body("package")
        if body is None:
            return
        r.check("cd " in body, "package() 进入解压后的源码目录")
        r.check("systemctl" not in body,
                "package() 不调用 systemctl（包不得自动启用/启动用户服务）")
        r.check("linkrd install" not in body,
                "package() 不调用 linkrd install --autostart")
        r.check("$pkgdir" in body, "package() 使用 $pkgdir 安装文件")

        # 所有 install 目标必须在 $pkgdir 下且位于 /usr
        install_dests: list[str] = []
        unprefixed: list[str] = []
        for line in logical_lines(body):
            if not re.match(r"^\s*install\s", line):
                continue
            r.check("$pkgdir" in line,
                    "install 目标均写入 $pkgdir: %s" % line.strip()[:60])
            tokens = line.split()
            if tokens:
                dest = tokens[-1].strip("\"'")
                if dest.startswith("/") and not dest.startswith("$pkgdir"):
                    unprefixed.append(dest)
            install_dests.extend(
                self.expand(m) for m in re.findall(r"\$pkgdir(/[^\s\"';)]+)", line)
            )
        r.check(not unprefixed,
                "没有直接写到 $pkgdir 之外的绝对路径", ", ".join(unprefixed))
        outside = [d for d in install_dests if not d.startswith("/usr/")]
        r.check(not outside, "所有安装路径都在 /usr 下", ", ".join(outside))
        local = [d for d in install_dests if d.startswith("/usr/local/")]
        r.check(not local, "没有文件安装到 /usr/local", ", ".join(local))

        pkgname = self.scalar("pkgname")
        required = {
            "/usr/bin/linkrd",
            "/usr/share/man/man1/linkrd.1",
            "/usr/lib/udev/rules.d/70-linkrd.rules",
            "/usr/lib/systemd/user/linkrd.service",
            "/usr/share/doc/%s/README.md" % pkgname,
            "/usr/share/doc/%s/README.zh-CN.md" % pkgname,
        }
        missing = sorted(required - set(install_dests))
        r.check(not missing, "package() 安装了全部必需文件（二进制/man/udev/"
                             "systemd user unit/文档）", ", ".join(missing))

    # -- .install 脚本 ---------------------------------------------------
    def check_install_script(self) -> None:
        r = self.report
        install = self.scalar("install")
        if not install:
            return
        path = os.path.join(self.dir, install)
        if not os.path.isfile(path):
            return
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
        syntax = run(["bash", "-n", install], self.dir)
        r.check(syntax.returncode == 0, "%s 语法（bash -n）" % install,
                syntax.stderr.strip() or "")
        for func in ("post_install", "post_upgrade", "post_remove"):
            r.check(bool(re.search(r"(?m)^%s\(\)" % func, text)),
                    "%s 定义 %s()" % (install, func))
        r.check("udevadm control --reload-rules" in text,
                "post_* 重新加载 udev 规则")
        r.check("udevadm trigger" in text and "subsystem-match=tty" in text,
                "post_* 触发 tty 设备事件")
        r.check("command -v udevadm" in text or "|| true" in text,
                "udevadm 缺失时安装脚本不会失败（容错）")
        r.check("systemctl --user" in text,
                "安装脚本提示 systemctl --user 启用方式")

        exec_lines = strip_heredoc(text)
        forbidden_cmds = []
        for lineno, line in exec_lines:
            if re.search(r"systemctl\s+--user\s+(enable|start|restart)", line):
                forbidden_cmds.append("%d: %s" % (lineno, line))
            if "linkrd" in line and "install" in line and "--autostart" in line:
                forbidden_cmds.append("%d: %s" % (lineno, line))
            if re.search(r"\b(curl|wget|pacman\s+-S|git\s+clone)\b", line):
                forbidden_cmds.append("%d: %s" % (lineno, line))
            if "$pkgname" in line or "$pkgver" in line:
                forbidden_cmds.append("%d: 使用未定义的 $pkgname/$pkgver: %s"
                                      % (lineno, line))
        r.check(not forbidden_cmds,
                "安装脚本不自动启用服务、不联网、不引用未定义的包变量",
                "; ".join(forbidden_cmds))
        r.check("pre_install" not in text,
                "安装脚本没有 pre_install（不做网络/交互逻辑）")

    # -- shellcheck（可选工具，未安装则跳过） ---------------------------
    def check_shellcheck(self) -> None:
        r = self.report
        shellcheck = shutil.which("shellcheck")
        if shellcheck is None:
            r.pass_("shellcheck 未安装，跳过可选检查（不影响静态校验结论）")
            return

        def summary(res: subprocess.CompletedProcess) -> str:
            lines = (res.stdout + res.stderr).strip().splitlines()
            return lines[-1] if lines else ""

        install = self.scalar("install")
        if install and os.path.isfile(os.path.join(self.dir, install)):
            res = run([shellcheck, "-s", "bash", "-x", install], self.dir)
            r.check(res.returncode == 0,
                    "shellcheck %s 无告警" % install, summary(res))
        res = run([shellcheck, "-s", "bash", "-e",
                   "SC2034,SC2154,SC2164,SC1090,SC1091", "PKGBUILD"], self.dir)
        r.check(res.returncode == 0,
                "shellcheck PKGBUILD 无告警"
                "（忽略 makepkg 变量与 errexit 相关噪声）", summary(res))

    # -- .SRCINFO -------------------------------------------------------
    def expected_srcinfo(self) -> list[tuple[str, str]]:
        expected: list[tuple[str, str]] = []
        for field in CANONICAL_GLOBAL:
            if field in ("pkgbase",):
                continue
            if field not in self.declared:
                continue
            vals = self.values.get(field, [])
            if field in SCALAR_FIELDS:
                if vals and vals[0] != "":
                    expected.append((field, vals[0]))
            else:
                expected.extend((field, v) for v in vals if v != "")
        return expected

    def check_srcinfo(self) -> None:
        r = self.report
        path = os.path.join(self.dir, ".SRCINFO")
        with open(path, "rb") as fh:
            raw_bytes = fh.read()
        if raw_bytes and not raw_bytes.endswith(b"\n"):
            r.fail(".SRCINFO 以换行结尾")
        if b"\r\n" in raw_bytes:
            r.fail(".SRCINFO 使用 CRLF 换行（应为 LF）")
        sections, error = parse_srcinfo(path)
        if error:
            r.fail(".SRCINFO 可解析（%s）" % error)
            return
        expected_pkgbase = self.scalar("pkgbase") or self.scalar("pkgname")
        if not r.check(len(sections) == 2,
                       ".SRCINFO 结构为 pkgbase + 单个 pkgname 段",
                       "实际 %d 个段" % len(sections)):
            return
        base_name, base_attrs = sections[0]
        pkg_name, pkg_attrs = sections[1]
        r.check(base_name == "pkgbase",
                ".SRCINFO 第一部分是 pkgbase")
        r.check(pkg_name == "pkgname", ".SRCINFO 第二部分是 pkgname")
        r.check(pkg_attrs == [], ".SRCINFO 的 pkgname 段只含 pkgname",
                "; ".join("%s = %s" % kv for kv in pkg_attrs))
        expected = [("pkgbase", expected_pkgbase)] + self.expected_srcinfo()
        # pkgbase 段头部写作 "pkgbase = <name>"，把名字作为该段的值处理。
        actual = [("pkgbase", expected_pkgbase)] + base_attrs
        if r.check(expected == actual,
                   ".SRCINFO 与 PKGBUILD 完全一致（字段、值、makepkg 顺序）"):
            r.pass_("pkgname 段: %s" % self.scalar("pkgname"))
        else:
            missing = [x for x in expected if x not in actual]
            extra = [x for x in actual if x not in expected]
            if missing:
                r.fail(".SRCINFO 缺少字段（%s）"
                       % "; ".join("%s = %s" % kv for kv in missing))
            if extra:
                r.fail(".SRCINFO 多出字段（%s）"
                       % "; ".join("%s = %s" % kv for kv in extra))
            if not missing and not extra:
                r.fail(".SRCINFO 字段顺序与 makepkg --printsrcinfo 不一致")
        # 段内字段顺序必须符合 makepkg 的规范顺序。
        order = {name: i for i, name in enumerate(CANONICAL_GLOBAL)}
        indices = [order.get(k, -1) for k, _ in base_attrs]
        r.check(all(i >= 0 for i in indices) and
                indices == sorted(indices),
                ".SRCINFO pkgbase 字段顺序符合 makepkg 规范")
        order_pkg = {name: i for i, name in enumerate(CANONICAL_PKGNAME)}
        indices_pkg = [order_pkg.get(k, -1) for k, _ in pkg_attrs]
        r.check(all(i >= 0 for i in indices_pkg) and
                indices_pkg == sorted(indices_pkg),
                ".SRCINFO pkgname 字段顺序符合 makepkg 规范")


# ---------------------------------------------------------------------------
# 跨包 / 文档 / 当前源码树检查
# ---------------------------------------------------------------------------

def cross_checks(packages: list[str]) -> Report:
    report = Report("跨包与文档检查")
    installs = {}
    for name in packages:
        path = os.path.join(ROOT, name, "PKGBUILD")
        if not os.path.isfile(path):
            report.fail("缺少 %s/PKGBUILD" % name)
            continue
        declared, values, functions, error = extract_pkgbuild(os.path.join(ROOT, name))
        if error:
            report.fail("%s: 无法提取 install 字段" % name)
            continue
        install = values.get("install", [""])[0]
        installs[name] = install
    if len(installs) == 2:
        first, second = installs.values()
        report.check(first == second,
                     "两个包的 install 脚本文件名一致（%s）" % first,
                     "%s vs %s" % (first, second))
        paths = [os.path.join(ROOT, n, i) for n, i in installs.items()]
        if all(os.path.isfile(p) for p in paths):
            with open(paths[0], "r", encoding="utf-8") as fh:
                a = fh.read()
            with open(paths[1], "r", encoding="utf-8") as fh:
                b = fh.read()
            report.check(a == b, "两个包的 .install 脚本内容一致（避免行为漂移）")

    readme = os.path.join(ROOT, "README.md")
    if report.check(os.path.isfile(readme), "packaging/aur/README.md 存在"):
        with open(readme, "r", encoding="utf-8") as fh:
            text = fh.read().lower()
        checks = {
            "makepkg -si": "README 说明 `makepkg -si` 本地构建流程",
            "systemctl --user": "README 说明 systemd --user 启用方式",
            "--autostart": "README 说明为何不要用 linkrd install --autostart",
            "sha256": "README 说明发布前替换 sha256sums",
            "aur@aur.archlinux.org": "README 说明 AUR git 提交流程",
            "未验证": "README 诚实标注未在 Arch 上实际 makepkg 验证",
        }
        for needle, msg in checks.items():
            report.check(needle in text, msg)

    # 当前源码树（将被打成 v0.1.0 tarball 的仓库）必须包含打包所需文件。
    source_files = [
        ("man/linkrd.1", "man page 由并行工作产出，必须包含在 v0.1.0 tag 中"),
        ("contrib/udev/70-linkrd.rules", "udev 规则必须包含在 tag 中"),
        ("contrib/systemd/linkrd.service", "systemd user unit 必须包含在 tag 中"),
        ("README.md", "README 文档必须包含在 tag 中"),
        ("README.zh-CN.md", "中文 README 必须包含在 tag 中"),
    ]
    for rel, why in source_files:
        path = os.path.join(REPO_ROOT, rel)
        if os.path.isfile(path):
            report.pass_("源码树包含 %s" % rel)
        else:
            report.warn("源码树缺少 %s（%s）" % (rel, why))
    return report


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="linkrd AUR 包的静态校验（macOS/Linux；不替代 makepkg/namcap）")
    parser.add_argument("packages", nargs="*", default=list(ALL_PACKAGES),
                        help="要校验的包目录名（默认: %s）"
                             % " ".join(ALL_PACKAGES))
    parser.add_argument("--strict", action="store_true",
                        help="把 WARN 视为失败")
    args = parser.parse_args(argv)

    if shutil.which("bash") is None:
        print("FAIL: 找不到 bash，无法做 PKGBUILD 语法/变量校验")
        return 1

    print("linkrd AUR 静态校验（macOS/Linux 静态检查；真实构建仍需 Arch 上的 "
          "makepkg/namcap）")
    print("=" * 78)
    print()

    reports: list[Report] = []
    for name in args.packages:
        directory = os.path.join(ROOT, name)
        if not os.path.isdir(directory):
            report = Report(name)
            report.fail("包目录不存在: %s" % directory)
            report.print()
            reports.append(report)
            continue
        reports.append(PackageLint(name, directory).run())

    cross = cross_checks(list(args.packages))
    cross.print()
    reports.append(cross)

    total = [0, 0, 0]
    for report in reports:
        p, w, f = report.counts()
        total[0] += p
        total[1] += w
        total[2] += f
    print("=" * 78)
    print("总结: PASS %d, WARN %d, FAIL %d" % tuple(total))
    failures = total[2] > 0 or (args.strict and total[1] > 0)
    if failures:
        print("结果: 失败（请修复上面的 FAIL%s）"
              % ("，--strict 下还包括 WARN" if args.strict else ""))
        return 1
    print("结果: 全部静态检查通过%s"
          % ("（仍有 %d 项警告需人工确认）" % total[1] if total[1] else ""))
    print()
    print("注意: 本脚本只在 macOS/Linux 上做静态分析；以下必须在 Arch 上验证：")
    print("  - makepkg -si（真实源码构建）/ namcap（包内容与依赖）")
    print("  - 上游 v0.1.0 tag 与真实 sha256sums（当前为 SKIP）")
    print("  - pacman -U 安装/升级/卸载时 .install 脚本的实际行为")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
