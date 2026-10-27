# linkrd 的 Arch Linux / AUR 打包

本目录提供 [linkrd](https://github.com/radxa-linkr/linkrd) 0.1.0 的两份 AUR 包：

| 包名 | 来源 | 说明 |
| --- | --- | --- |
| `linkrd` | 上游 `v0.1.0` tag 的 GitHub archive tarball | 稳定版，源码构建 |
| `linkrd-git` | `git+https://github.com/radxa-linkr/linkrd.git` | VCS 包，跟随 main 分支；`provides`/`conflicts` 与 `linkrd` 互斥 |

两个包都安装 `/usr/bin/linkrd`、man page、udev 规则、systemd **user** unit 和文档。

> **状态**：已在 macOS 上完成静态校验（`lint-aur.py` 全绿）。**尚未在真实 Arch
> 环境中运行 `makepkg` / `pacman` / `namcap`**，见文末“已知未验证项”。
> 最近一次结果：`PASS 192 / WARN 6 / FAIL 0`。6 项 WARN 全部是发布前待办
> （上游 LICENSE、`sha256sums=('SKIP')`、Maintainer 占位、`-git` 的 `pkgver`
> 占位），不是代码问题。

## 目录结构

```
packaging/aur/
├── README.md
├── lint-aur.py                 # 静态校验脚本（PKGBUILD/.SRCINFO/install 脚本）
├── linkrd/
│   ├── PKGBUILD
│   ├── .SRCINFO                # 手工维护，须与 PKGBUILD 一致
│   └── linkrd.install          # pacman install script（udev 重载 + 提示）
└── linkrd-git/
    ├── PKGBUILD
    ├── .SRCINFO
    └── linkrd.install          # 与 linkrd/linkrd.install 内容相同
```

静态校验：

```sh
python3 packaging/aur/lint-aur.py            # 全部包
python3 packaging/aur/lint-aur.py linkrd     # 只校验一个
python3 packaging/aur/lint-aur.py --strict   # 把 WARN 也当失败
```

脚本只做静态分析：`bash -n`、PKGBUILD 变量/数组提取、字段与依赖规则、
`package()` 安装路径、`.install` 约束、`.SRCINFO` 逐字段一致性与 makepkg 字段
顺序。若系统安装了 `shellcheck`，会额外检查 `.install` 与 PKGBUILD
（PKGBUILD 会忽略 makepkg 提供的变量和 errexit 相关噪声）。

## 在 Arch 上本地构建与验证

```sh
sudo pacman -S --needed base-devel rust cargo namcap
# 实际 MSRV 是 rust >= 1.89（edition 2024 要求 1.85，但源码用了 std 的
# File::lock/try_lock（1.89 稳定）且 time 0.3.55 要求 1.88）；Arch 的 rust 远高于此。
# Cargo.lock 已提交，所以 --locked/--frozen 可以工作。

cd packaging/aur/linkrd
# 前提：上游已发布 v0.1.0 tag（否则 source 会 404）
updpkgsums                          # 把 sha256sums 的 SKIP 换成真实值
makepkg --printsrcinfo > .SRCINFO   # 重新生成 .SRCINFO
makepkg -si                         # 源码构建 + 安装，会执行 check()
namcap PKGBUILD
namcap linkrd-0.1.0-1-x86_64.pkg.tar.zst
```

- 只验证构建不安装：`makepkg -f`；跳过测试：`makepkg --nocheck`
  （注意 `-C` 是 `--cleanbuild`，不是跳过 check）。
- 构建全程离线：`prepare()` 把依赖抓到 `$srcdir/cargo-home`，
  `build()`/`check()` 使用 `--locked --frozen`，不会在构建阶段访问网络。

`linkrd-git` 需要先让 `pkgver()` 更新版本，再生成 `.SRCINFO`：

```sh
cd packaging/aur/linkrd-git
makepkg -o                          # 拉取 git 源码并运行 pkgver()
makepkg --printsrcinfo > .SRCINFO
makepkg -si
```

`makepkg --printsrcinfo` 的输出不带注释头，与仓库中的 `.SRCINFO` 格式一致；
生成后请再跑一次 `python3 packaging/aur/lint-aur.py` 确认一致。

## 发布前置条件（AUR 提交前必须完成）

1. **上游打 v0.1.0 tag**（目前仓库只有一次 `init proj` 提交，没有 tag，所以
   `.../archive/v0.1.0.tar.gz` 现在返回 404）：

   ```sh
   git tag -a v0.1.0 -m "linkrd 0.1.0"
   git push origin v0.1.0
   ```

   tag 必须包含打包需要的文件：`man/linkrd.1`、
   `contrib/udev/70-linkrd.rules`、`contrib/systemd/linkrd.service`、
   `README.md`、`README.zh-CN.md`、`Cargo.lock`（`package()` 直接从源码树安装）。
   `PKGBUILD` 本身不需要在上游仓库中。

2. **替换 sha256sums**：`sha256sums=('SKIP')` 只是占位，发布前必须换成真实
   值（在 Arch 上 `cd packaging/aur/linkrd && updpkgsums`）。GitHub 自动生成的
   archive tarball 不保证长期逐字节稳定，校验和应在下载后由 `updpkgsums`
   生成；上游若改为上传自建 release tarball，请同步调整 `source`。

3. **补 LICENSE**：上游仓库没有 `LICENSE` 文件，`Cargo.toml` 也没有 `license`
   字段，因此两个包的 `license=('unknown')` 是**如实占位，不是最终许可证**。
   上游补上 LICENSE 后：
   - 把 `license` 改为对应 SPDX 标识；
   - 在 `package()` 中安装 `/usr/share/licenses/$pkgname/LICENSE`；
   - 重新生成 `.SRCINFO`。

4. **替换 Maintainer 占位符**：PKGBUILD 文件头的
   `# Maintainer: TODO <todo@example.invalid>` 必须换成实际维护者。

5. **重新生成 `.SRCINFO`** 并再次运行 `lint-aur.py`。

## 提交为两个 AUR git 仓库

首次提交前在 AUR 账户（https://aur.archlinux.org）添加 SSH public key。

```sh
# 稳定版
git clone ssh://aur@aur.archlinux.org/linkrd.git
cd linkrd
cp /path/to/linkrd/{PKGBUILD,.SRCINFO,linkrd.install} .
git add PKGBUILD .SRCINFO linkrd.install
git commit -m "Initial import: linkrd 0.1.0-1"
git push

# VCS 版
git clone ssh://aur@aur.archlinux.org/linkrd-git.git
cd linkrd-git
cp /path/to/linkrd-git/{PKGBUILD,.SRCINFO,linkrd.install} .
git add PKGBUILD .SRCINFO linkrd.install
git commit -m "Initial import: linkrd-git"
git push
```

AUR 仓库只应包含 `PKGBUILD`、`.SRCINFO`、`.install`（以及被 `source` 引用的
本地小文件）；不要提交构建产物或大文件。首次推送必须包含 `.SRCINFO`，AUR 会
校验它与 PKGBUILD 是否一致。

## 安装后启用用户服务与 udev 权限

包**不会**自动 enable/start 用户服务（发行版无法知道哪个桌面用户需要它）。
安装后由用户自己启用：

```sh
systemctl --user daemon-reload
systemctl --user enable --now linkrd.service
systemctl --user status linkrd.service
journalctl --user -u linkrd -f          # 查看日志
```

`/usr/lib/systemd/user/linkrd.service` 的 `WantedBy=graphical-session.target`，
因此只会在图形会话中启动；SSH/无桌面会话不会自动启动。需要额外参数（例如
指定串口）时用 `systemctl --user edit linkrd.service` 覆盖 `ExecStart=`，或先用
`linkrd --port /dev/ttyACM0` 前台调试。

udev 规则 `/usr/lib/udev/rules.d/70-linkrd.rules` 匹配 Linkr USB CDC-ACM
（VID `1D6B`、PID `0104`），用 `TAG+="uaccess"` 给当前 seat 的登录用户授予
`/dev/ttyACM*` 读写 ACL（不依赖永久加入 `dialout`/`uucp` 组，
`GROUP="dialout", MODE="0660"` 作为兜底）。`.install` 的 `post_install`/
`post_upgrade` 会执行 `udevadm control --reload-rules` 和
`udevadm trigger --subsystem-match=tty --action=change`（均带 `|| true` 容错）。

验证：

```sh
getfacl /dev/ttyACM0        # 应看到当前用户被授予 rw 的 ACL
ls -l /dev/ttyACM*          # 兜底情况下组为 dialout
```

如果设备没有 ACL，请重新插拔 Linkr，或手动执行一次上面的 `udevadm trigger`。
注意 `linkrd` 必须以桌面用户身份运行，不要用 `sudo`。

## 为什么不要让用户执行 `linkrd install --autostart`

上游 README 面向“手动下载二进制”的用户，会建议 `./linkrd install --autostart`。
在包管理场景下**不要**这样做：

- 它把二进制复制到 `~/.local/bin/linkrd`，修改 shell profile 的 `PATH`，并写入
  `~/.config/autostart/linkrd.desktop` 实现登录自启；
- pacman 升级只更新 `/usr/bin/linkrd`，而 autostart 仍指向 `~/.local/bin` 里的
  **旧副本** → 版本漂移，用户以为已升级实际跑的是旧二进制；
- `linkrd uninstall` 只清理它自己复制的副本与 autostart 文件，并不知道 pacman
  拥有的 `/usr/bin/linkrd`，会留下两套文件、两个启动入口；
- 自动启动由用户级 systemd unit 负责，启用/停用/重启都可用 `systemctl --user`
  管理，升级由 pacman 管理，职责清晰。

因此本包只安装 `/usr/bin/linkrd` + user unit，并让用户用
`systemctl --user enable --now linkrd.service` 启用。

## Arch 打包规范自查清单（静态核对）

- [x] 必需字段齐全：`pkgname` `pkgver` `pkgrel` `pkgdesc` `arch` `url`
      `license` `depends` `makedepends` `source` `sha256sums` `install`
- [x] `pkgdesc` 60/66 字符（≤80），不以包名开头
- [x] `arch=('x86_64' 'aarch64')`
- [x] `license=('unknown')`：如实占位，发布前按上文替换（不编造许可证）
- [x] `depends=('gcc-libs')`：Rust 二进制动态链接 `libgcc_s`；wayland/x11
      剪贴板后端均为纯 Rust 实现，未声明 `libwayland`/`libX11`
- [x] `makedepends=('cargo' 'rust')`，`linkrd-git` 额外 `git`
- [x] stable `source=("$pkgname-$pkgver.tar.gz::$url/archive/v$pkgver.tar.gz")`，
      `source` 与 `sha256sums` 数量一致（当前为 `SKIP`，待替换）
- [x] `package()` 所有安装路径都经 `$pkgdir` 且位于 `/usr` 下，无 `/usr/local`
- [x] 安装二进制、man page、udev 规则、systemd user unit、README 文档
- [x] 不自动 enable/start 服务；`.install` 仅 udevadm reload/trigger + echo 提示
- [x] `.install` 无 `pre_install`、无网络操作、无交互逻辑
- [x] 未用 `-O3`/`RUSTFLAGS` 覆盖 Cargo 构建参数（无 `options`、无 RUSTFLAGS）
- [x] `prepare()`：`cargo fetch --locked`，独立 `CARGO_HOME`；
      `build()`：`cargo build --release --locked --frozen`；
      `check()`：`cargo test --release --locked --frozen`（测试 hermetic，见下）
- [x] `.SRCINFO` 与 PKGBUILD 逐字段一致，字段顺序遵循 `makepkg --printsrcinfo`
- [x] `linkrd-git`：`provides`/`conflicts=('linkrd')`，`pkgver()` 正确处理
      上游无 tag 的情况（回退为 `0.1.0.r<提交数>.g<短哈希>`）
- [x] 维护者、许可证、校验和等未完成项已在 PKGBUILD 注释与本文档中标明

`check()` 说明：`tests/install.rs` 是 hermetic 的（`#![cfg(target_os = "linux")]`，
使用临时 `HOME`/`XDG_*` 目录，不访问剪贴板、串口或外部网络），Lead 已在真实
Linux 内核上跑通全部 42 个测试，因此本包保留 `check()`。极端环境（受限沙箱、
root、缺少 `/bin/sh`）下可用 `makepkg --nocheck` 跳过。

## 已知未验证项 / 风险

- **未在真实 Arch 环境验证**：没有运行过 `makepkg`、`pacman`、`namcap`；
  构建、strip、依赖解析、`.install` 在事务中的实际行为均未实测。
- **上游尚未发布 v0.1.0 tag**：`source` URL 当前 404，`sha256sums=('SKIP')`
  不是可发布的最终状态。
- **上游缺少 LICENSE**：`license=('unknown')` 是占位。
- **`linkrd-git` 的 `pkgver` 是占位值**：`0.1.0.r1.g22c63f0` 对应当前上游
  HEAD；提交 AUR 前按上文 `makepkg -o` 更新并重新生成 `.SRCINFO`。
- **动态依赖未用 namcap 验证**：`depends=('gcc-libs')` 基于 Cargo.toml 与依赖
  特征（`serialport` 关闭默认 features、wayland 后端纯 Rust）推断，未检查
  二进制的实际 `NEEDED`。
- **udev/uaccess 行为未实机验证**：ACL 是否生效取决于 systemd-logind、桌面
  会话与内核；`getfacl` 步骤需要在实机确认。
- **aarch64 未做交叉编译验证**：Arch 上 `makepkg` 原生构建即可，本仓库未验证
  arm64 目标。
- **check() 未在 Arch clean chroot 中实跑**（仅在 Lead 的 Linux 内核环境跑过）。
