# linkrd 发行打包总览（Debian / Arch Linux AUR）

本目录（连同仓库根部的 `debian/`、`man/`、`contrib/`）是 linkrd 0.1.0 的发行打包产物与工具。
代码审阅结论与上游待办见 [`docs/REVIEW.md`](../docs/REVIEW.md)；Linux 真机验证的完整报告见
[`docs/LINUX-VERIFICATION.md`](../docs/LINUX-VERIFICATION.md)。

## 1. 交付物

| 产物 | 路径 | 说明 |
|---|---|---|
| Debian 二进制包（x86_64） | `dist/linkrd_0.1.0-1_amd64.deb` | musl 静态链接，无 `Depends`，任意 glibc/musl 发行版可直接安装 |
| Debian 二进制包（aarch64） | `dist/linkrd_0.1.0-1_arm64.deb` | 同上（Radxa 设备生态常用 arm64） |
| Debian 二进制包（glibc 动态，可选） | `dist/gnu/linkrd_0.1.0-1_{amd64,arm64}.deb` | `Depends: libc6 (>= 2.28)`，需要 glibc ≥ 2.28（Ubuntu 20.04+/Debian 11+） |
| Debian 源包 | `debian/` | 标准 debhelper 13 源包，可在 Debian/Ubuntu 上 `dpkg-buildpackage -us -uc -b` 自行构建 |
| AUR 源包（稳定版） | `packaging/aur/linkrd/` | `PKGBUILD` + `.SRCINFO` + `linkrd.install`，从上游 tag tarball 源码构建 |
| AUR 源包（VCS） | `packaging/aur/linkrd-git/` | `PKGBUILD` + `.SRCINFO`，跟踪上游默认分支，`provides/conflicts=linkrd` |
| 手册页 | `man/linkrd.1` | deb 与 AUR 共用，覆盖全部子命令与参数 |
| udev 规则 / systemd 用户单元 | `contrib/udev/70-linkrd.rules`、`contrib/systemd/linkrd.service` | deb 与 AUR 共用（`debian/` 内是指向它们的符号链接） |
| 构建与校验工具 | `packaging/deb/build-deb.sh`、`packaging/deb/validate-deb.py`、`packaging/aur/lint-aur.py` | 在无 dpkg/makepkg 的 macOS 上生成并校验产物 |

GitHub release 页建议同时附上 `dist/*.deb` 与 `dist/SHA256SUMS`。

## 2. 快速开始

### Debian / Ubuntu

```sh
sudo apt install ./linkrd_0.1.0-1_amd64.deb      # 或 arm64
systemctl --user daemon-reload
systemctl --user enable --now linkrd.service     # 按桌面用户启用，包不会自动启用
linkrd --version
journalctl --user -u linkrd -f                    # 日志；同时写入 ~/.local/share/linkrd/logs/
```

- udev 规则已把 Linkr 设备（VID `1D6B` / PID `0104`）的 `/dev/ttyACM*` 通过 logind `uaccess`
  ACL 授权给当前座位用户，通常**不需要**加入 `dialout` 组；老系统无 ACL 支持时规则回退到
  `GROUP="dialout" MODE="0660"`。
- **不要**再执行 `linkrd install --autostart`：它会把二进制复制到 `~/.local/bin` 并写用户级
  autostart/`--startup` 记录，包升级无法更新该副本。详见 `debian/README.debian`。
- 无 Radxa Linkr 设备时服务会持续重连（每 5 s 重启一次进程，日志有 WARN），属预期行为。

### Arch Linux（AUR）

```sh
cd packaging/aur/linkrd
makepkg -si                     # 需要 base-devel rust cargo
sudo systemctl --user daemon-reload   # 注：--user 不需要 sudo，见 README
systemctl --user enable --now linkrd.service
```

上游发布 `v0.1.0` tag 之后才能直接 `makepkg`：当前 `sha256sums` 为 `SKIP`，必须在 tag 发布后
`updpkgsums` 并重新生成 `.SRCINFO`。步骤见 `packaging/aur/README.md`。

## 3. 关键设计决策

1. **发行包用 `/usr/bin/linkrd` + systemd 用户单元，禁用上游自安装流程。**
   `linkrd install --autostart` 是给“单文件下载”场景设计的：复制自身到 `~/.local/bin`、
   改写 shell profile、写用户级 autostart，并依赖 `~/.config/linkrd/installation.json`
   才能用 `--startup` 启动。这与包管理器的升级路径冲突（旧副本劫持 PATH、升级后仍跑旧二进制）。
   因此包只提供 `/usr/bin/linkrd`，由 `linkrd.service`（用户单元）启动。
2. **单元不自动 enable。** Linkrd 是“按桌面用户运行”的代理，由 root 在装包时替所有用户
   启用没有意义；也避免没接设备的用户无谓地常驻进程。启用方式写在 man page、README.debian、
   `.install` 提示与包描述里。
3. **启动失败由 systemd 重试，而不是 `--startup`。** 发布形态不用 `--startup`（它要求安装记录），
   而是 `ExecStart=/usr/bin/linkrd` + `Restart=on-failure` + `RestartSec=5s`：
   桌面会话环境（`WAYLAND_DISPLAY`/`DISPLAY`/DBus）尚未就绪时进程会退出，systemd 5 s 后重启；
   两次启动间隔 5 s，远低于 systemd 默认 `StartLimitBurst=5/10s` 的限流阈值，不会进入 failed。
   若需要更保守（例如设备长期不在场），可自行 `systemctl --user edit linkrd.service` 调整 `RestartSec`。
4. **静态 musl 作为预编译 deb 的主格式。** `clipboard-rs` 的 Wayland 后端是纯 Rust
   （`wayland-backend` 未启用 `client_system`/`dlopen`），X11 走纯 Rust `x11rb`，
   `serialport` 关闭了 `libudev`，因此 Linux 侧没有系统库依赖 —— 静态 musl 二进制可在任意
   glibc 版本上运行，也免去 `libwayland-client`/`libX11` 的运行期依赖。
   源包（`debian/`、AUR）按发行版原生方式动态链接，符合各自发行版策略。
5. **udev 规则用 `uaccess`。** 比起要求用户加入 `dialout`/`uucp` 组并重新登录，logind 的座位 ACL
   随登录自动生效；`GROUP`/`MODE` 只是无 ACL 环境的兜底。

## 4. 预编译产物的生成（可复现）

宿主 macOS arm64，无 docker，使用 rustup 目标 + zig 交叉链接：

```sh
# 一次性准备（产物已在 .build/ 下，.gitignore 忽略）
#   .build/rustup（rustup 工具链）、.build/tools/zig-aarch64-macos-0.14.1
#   .build/tools/bin/cargo-zigbuild
source .build/env.sh && export HOME=$PWD/.build/home   # cargo-zigbuild 的缓存必须落在工作区

cargo zigbuild --locked --release --target x86_64-unknown-linux-musl
cargo zigbuild --locked --release --target aarch64-unknown-linux-musl
cargo zigbuild --locked --release --target x86_64-unknown-linux-gnu.2.28
cargo zigbuild --locked --release --target aarch64-unknown-linux-gnu.2.28

cp target/x86_64-unknown-linux-musl/release/linkrd  .build/artifacts/linkrd-linux-amd64
cp target/aarch64-unknown-linux-musl/release/linkrd .build/artifacts/linkrd-linux-arm64
# gnu 变体同理（-gnu 后缀）

packaging/deb/build-deb.sh --binary .build/artifacts/linkrd-linux-amd64 --arch amd64 \
                            --binary .build/artifacts/linkrd-linux-arm64 --arch arm64
packaging/deb/build-deb.sh --prefer gnu        # → dist/gnu/（glibc 动态变体）
python3 packaging/deb/validate-deb.py          # 结构/元数据/一致性校验
python3 packaging/aur/lint-aur.py              # AUR 静态规范校验
```

`build-deb.sh` 不依赖 `dpkg-deb`：它按 Debian 二进制包格式（`ar` + `control.tar.gz` +
`data.tar.gz`）生成包，成员名/属主/权限/`md5sums`/`Installed-Size` 与 dpkg-deb 产物一致，
固定 `SOURCE_DATE_EPOCH` 后重复构建 sha256 不变。

## 5. 已执行的真实验证

宿主无 Linux/dpkg/makepkg，因此除静态校验外，还用 qemu 启动了真实 Linux 内核验证
（完整报告 [`docs/LINUX-VERIFICATION.md`](../docs/LINUX-VERIFICATION.md)；原始日志在 `.build/` 下，未入库）：

| 验证 | 环境 | 结果 |
|---|---|---|
| amd64 / arm64 静态二进制 smoke（`--version`/`--help`/`--list-ports`/`status`） | Alpine 3.22 内核 + arm64/aarch64 访客 | PASS |
| Rust 测试套件（framing/protocol/logging/session/transport/install 共 42 例） | aarch64 Linux 访客（真实 `/proc`、`/dev/ptmx`、loopback TCP、`/bin/bash`） | 42 passed / 0 failed |
| amd64 测试套件 | x86_64 Linux 访客（TCG 纯软件模拟） | 42 passed / 0 failed |
| `.deb` 用真实 `dpkg` 安装 / 查询 / 校验 / 卸载 / 清除 | Ubuntu 24.04 arm64 与 x86_64 访客，dpkg 1.22 | `dpkg -i`、`dpkg --verify`（md5sums）、`dpkg -L`、`dpkg -r`、`dpkg -P` 全部 rc=0 |
| 包内文件与手册页 | 同上 | `/usr/bin/linkrd` 可执行、`mandoc -T lint` 与 `-T lint -W all` **0 输出 rc=0**、udev 规则与用户单元落位正确 |
| 结构校验 | `packaging/deb/validate-deb.py`（67/68 项 × 4 包） | 全 PASS |
| AUR 静态规范校验 | `packaging/aur/lint-aur.py` | PASS 192 / WARN 6 / FAIL 0（WARN 均为发布前待办） |

最终产物 sha256 见 `dist/SHA256SUMS`。复验日志：`.build/verify/boot2-*.log`。

宿主侧另有：`cargo fmt --check` 通过、`cargo clippy --all-targets -D warnings` 0 告警、
`cargo build --locked --release` 通过（见 REVIEW.md 第 2 节）。

## 6. 发布前必须完成（release checklist）

1. **补 LICENSE**（作者决定，建议 `MIT OR Apache-2.0`）并加到 `Cargo.toml`；同步
   `debian/copyright`、AUR `license=`、`/usr/share/licenses/` 安装。
2. **补 `Cargo.toml` 的 `rust-version = "1.89"`**（实际 MSRV 是 1.89：`std::fs::File::lock`
   1.89 稳定、`time 0.3.55` 要求 1.88；`debian/control` 已按 1.89 写；Debian 13 的 rustc 1.85.1
   无法构建，需要 sid/backport/rustup）。
3. **把 `contrib/`、`man/`、`debian/`、`packaging/`、`docs/REVIEW.md` 提交入库**，打 `v0.1.0`
   annotated tag 并 push；tag 必须包含上述文件（AUR `package()` 从 tarball 内安装它们）。
4. **AUR**：tag 后 `updpkgsums` 替换 `sha256sums=('SKIP')`，`makepkg --printsrcinfo > .SRCINFO`，
   把 `Maintainer: TODO` 换成真实维护者，再提交两个 AUR 仓库。
5. **Debian**：源包当前需要联网 `cargo fetch`（不符合 Debian archive buildd 无网络要求）。
   若要进 Debian，需 vendoring/`dh-cargo`；否则只作为第三方 deb 发布（当前形态即可）。
6. 在真实 Debian/Ubuntu 桌面与 Arch 桌面上各做一次安装 → 登录自启 → 连接 Linkr 设备 → 剪贴板
   双向同步 → 注销/重登录 → 升级/卸载 的端到端验收（无真机时的替代验证已在上表列出）。
7. 若要让 `linkrd install` 与发行版包彻底不冲突，建议上游增加 `root` 拒绝、
   已安装包路径检测、以及“非 `--startup` 也可以重试桌面初始化”的 `--wait-desktop` 模式
   （见 `docs/REVIEW.md` P1-2/P1-5）。

## 7. 未覆盖项（诚实声明）

- 未在真实 Radxa Linkr 硬件上验证握手/心跳/重连/USB 热插拔；
- 未在真实 GNOME/KDE/wlroots Wayland 会话与 X11 会话中验证剪贴板后端；
- 未在真实 Arch 环境执行 `makepkg`/`namcap`/`pacman -U`（仅静态规范校验 + 等价构建命令验证）；
- 未执行 `lintian`（宿主机与 qemu 访客均无 lintian；`validate-deb.py` 覆盖了 lintian 的
  结构类检查项，但不等于 lintian 通过）；
- 未在 Debian 上用 `dpkg-buildpackage` 实际构建源包（只在 macOS 上按源包规则生成等价的
  二进制 deb；`debian/rules` 语法与 helper 参数经静态核对）。
