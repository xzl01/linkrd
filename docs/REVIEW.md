# linkrd 0.1.0 代码审阅与发行打包就绪性评估

- 审阅对象：`<repo>`，git 仓库唯一提交 `22c63f0 "init proj"`（上游 <https://github.com/radxa-linkr/linkrd>，作者 hiifong <f@f.style>）
- 审阅范围：`src/` 全部 15 个文件（约 3713 行）、`tests/` 6 个测试文件（约 1679 行）与 fixtures；发行相关文件（README、Cargo.toml/Cargo.lock、Cross.toml、`.cargo/config.toml`、`contrib/`、当前工作区中由打包同事产出的 `debian/`、`man/`、`packaging/`，仅作就绪性参照，不修改）
- 审阅环境：macOS arm64（宿主机），workspace-write 文件沙箱；本地 rustup 工具链 `rustc 1.99.1 (2026-10-13)`、`cargo 1.99.1`，`CARGO_HOME=/tmp/linkrd-cargo-home`；无 docker/dpkg/lintian/makepkg
- 结论摘要：**代码实现质量整体较好，但当前不能作为"可发布/可打包"的 0.1.0**。核心阻断项是 **缺少 LICENSE（法律上不可分发）**、**缺少发布 tag 且打包资产未入库**、**实际 MSRV 为 1.89（仓库/打包文档都写 1.85，Debian 13 的 rustc 1.85.1 无法构建）**；此外发行包形态与上游 `linkrd install --autostart` 自管理安装存在结构性冲突，需要明确取舍。

---

## 1. 项目概览

### 1.1 定位

linkrd 是 Radxa Linkr 硬件的 macOS/Linux/Windows 桌面用户会话代理：通过 USB CDC-ACM 串口与被控电脑的桌面剪贴板交换**纯文本**，协议为 Protobuf 信封 + `ACMP` 帧 + CRC32C，兼容既有 Linkr 协议；浏览器 WebSocket 由 Linkr 设备侧处理，程序只占用串口。

- 只同步 UTF-8 纯文本（含空文本、多行）；图片/文件/分块传输未实现（README.md:7-8）。
- 保留能力：剪贴板变化监听、周期刷新、去重与防回环、写入确认、心跳、USB 断线重连（README.md:9-10）。
- 运行形态：前台进程（`linkrd`）、后台/登录自启动（`linkrd install`/`--startup`）、单实例管控（`start/stop/pid/status`）、自安装/自卸载。
- 构建事实：`edition = "2024"`；`Cargo.lock` 已提交（195 个 package）；`Cross.toml` 声明 Linux musl x86_64/aarch64 与 Windows GNU 三个 target；Protobuf 生成代码已入库，无 `build.rs`、无 protoc 依赖（README.md:72-78）。

### 1.2 模块架构

| 模块 | 文件 | 职责 | 关键接口/类型 |
|---|---|---|---|
| 入口/CLI | `src/main.rs` | clap 参数、子命令分发、串口选择、重连循环、桌面初始化重试、Ctrl-C/SIGTERM 处理 | `Args`、`manage()`、`run()`、`initialize_desktop()`、`select_usb_port()` |
| 协议模型 | `src/protocol.rs`、`src/protocol/linkrd.protocol.v1.rs` | 常量（帧上限、MIME、能力名）、prost 生成的消息/枚举 | `Envelope`、`Hello`、`ClipboardSetRequest/Response`、`ClipboardChanged`、`Ping/Pong` |
| 帧层 | `src/framing.rs` | `ACMP` 8 字节头（magic + BE u32 长度）+ payload + BE CRC32C；流式读取、超时、长度/CRC/解码校验 | `encode_frame()`、`FrameReader::poll()`、`FrameError` |
| 传输/会话循环 | `src/transport.rs` | 读写双线程、有界队列、握手硬超时、心跳、周期刷新、优雅停机、写超时/背压 | `run_connection()`、`ConnectionOptions`、`write_with_deadline()` |
| 协议状态机 | `src/session.rs` | Hello 协商、消息 ID 单调性、重复写入缓存、写校验（MIME/UTF-8/NUL/大小）、revision、去重防回环 | `Session::receive/observe/apply/publish` |
| 剪贴板 | `src/clipboard.rs` | `clipboard-rs` 封装（Wayland/X11/macOS/Windows）、变化监听线程、纯文本读/写 | `SystemClipboard`、`ClipboardMonitor`、`ClipboardBackend` |
| 串口 | `src/serial.rs` | 打开 TTY、8N1 无流控、250ms 超时、Unix 非阻塞、DTR、关闭前清空输出 | `open()`、`close()` |
| 日志 | `src/logging.rs`、`src/logging/rolling.rs` | stderr + 非阻塞滚动文件；10MiB×7、UTC 跨日轮转；避免记录剪贴板内容 | `init()`、`SizeLimitedWriter`、`record_incoming/outgoing()` |
| 安装/运行时 | `src/install.rs`、`src/install/path.rs`、`src/install/startup.rs` | 自安装/自卸载（sha256 校验、临时文件原子替换、self-replace）、单实例锁与 runtime 控制文件、PATH/shell profile 改写、XDG autostart / launchd / HKCU Run | `RuntimeGuard`、`RuntimeStatus`、`lock()`、`path::plan/enable/disable`、`startup::enable/disable` |

结构上 `src/lib.rs` 暴露全部模块，`src/main.rs` 是唯一 bin；没有 feature gate 的“库/二进制”分离，测试可以同时使用 `linkrd::*` 与 `CARGO_BIN_EXE_linkrd`。

### 1.3 数据流

```
                      ┌─────────────────────────────┐
  桌面剪贴板变化 ─────►│ ClipboardMonitor 线程        │ dirty=true
                      └──────────────┬──────────────┘
                                     ▼
  ┌──────────────┐   text    ┌─────────────┐  ClipboardChanged   ┌───────────────┐
  │ SystemClipboard│◄────────│  Session    │────────────────────►│ outgoing 队列(8)│
  │ (clipboard-rs)│  write   │  状态机     │◄──── SetRequest ────│ (写线程)       │
  └──────────────┘          └──────▲──────┘                     └───────┬───────┘
                                   │ FrameReader ── incoming 队列(8)     │ ACMP 帧
                                   │ (读线程)                            ▼
                             serialport /dev/ttyACM*  ◄──── USB CDC-ACM ──── Linkr 设备
```

- 读线程：`FrameReader`（握手期上限 4KiB，ready 后 2MiB）→ 解码 `Envelope` → `incoming` 队列；任何 CRC/长度/解码/超时错误结束会话。
- 主会话循环：`recv_timeout(20ms)` 取消息 → 更新状态机 → 把要发送的帧用 `try_send` 交给写线程（队列满即失败并触发重连）；每 20ms 检查 shutdown/失败通道/握手超时/心跳；ready 后每 20ms 视 dirty 或每 1s 做一次剪贴板快照。
- 写线程：`recv_timeout(20ms)` 取帧 → `write_with_deadline()`（每 16KiB 一段、5s 帧写超时、可被 shutdown 打断）。
- 安装/运行时：管理命令在 `~/.config/linkrd`（macOS 为 `~/Library/Application Support/linkrd`）用 `management.lock` 串行化；运行进程用 `runtime.lock`（flock）+ `runtime.pid`/`runtime.control.json`（pid + UUID）+ `runtime.stop`（UUID 内容）实现单实例和本地优雅停止；`runtime-state.lock` 保护这些文件的一致性。
- 日志：`tracing` 全局订阅者；stderr 层带终端颜色（`NO_COLOR`/`TERM=dumb` 关闭），文件层经 `tracing-appender` 非阻塞（1024 行缓冲、lossy）写入 `SizeLimitedWriter`。

### 1.4 协议状态机要点（对照代码）

1. **握手**：启动即发 `Hello{protocol_major=1, session_id=uuid, message_id=1, capabilities=[watch,write], max_frame=2MiB, max_clipboard=1MiB}`；未 ready 时每 1s 重发；收到对端 `Hello` 后校验（major=1、session_id 16 字节非全零且 ≠ 自己、max_frame∈[1024,2MiB]、max_clipboard∈[1,1MiB]、必须含 `watch`），计算 `max_text=min(peer.max_clipboard, peer.max_frame-512)`，随即发送自己的 `Hello` 再发布初始快照（revision=1）。5s 未完成握手 → 会话错误 → 重连。
2. **业务消息**：对端消息 ID 必须严格递增（`message_id <= highest_received` 视为重放）；`session_id` 必须等于对端；`reply_to` 仅允许出现在 `Pong`；`ClipboardSetRequest` 需通过 MIME/大小/UTF-8/NUL 校验且对端声明了 `write` 能力，成功后先发 `ClipboardChanged{caused_by=请求引用, revision+1}` 再发 `ClipboardSetResponse{Applied{revision}}`；失败只发 Error 响应，不发布状态。写入结果缓存最近一次（含请求内容），用于“重复请求幂等返回”。
3. **心跳**：ready 后每 5s 发 `Ping`（一次只允许一个未决 ping），`Pong` 必须与本端 ping 的 `MessageRef` 完整匹配；15s 未决 → 判死重连。收到对端 `Ping` 回 `Pong{reply_to=对端引用}`。
4. **去重/防回环**：`Session::observe()` 读取剪贴板并构造 `State`（Content / Unavailable{NoText|TooLarge|Unsupported|ReadFailed}），与 `last_state` 相同则不发送；远端写入成功后主动把新状态写入 `last_state` 并带 `caused_by`，因此随后 watcher 触发的本地快照与缓存相同而不会回弹。
5. **重连**：`main::run()` 每次循环重新枚举串口、打开、DTR、清空缓冲、创建新 `Session`（revision 从 1 重来、重新发快照），失败/断开后退避 500ms→1s→2s→4s→(上限)5s；连接持续 ≥10s 后重置退避。
6. **帧约束**：`MAX_FRAME_BYTES=2MiB`、`MAX_CLIPBOARD_BYTES=1MiB`、`HELLO_MAX_BYTES=4096`、`MIN_FRAME_BYTES=1024`、`CONTENT_OVERHEAD=512`（`src/protocol.rs:7-11`）；`encode_frame` 拒绝空 payload 和超限；CRC 覆盖 8 字节头 + payload。

---

## 2. 验证结果

> 说明：任务要求在 `source .build/env.sh` 后运行 cargo。实际执行发现该本地 rustup 工具链（`stable-aarch64-apple-darwin`，1.99.1）是 **minimal profile，未安装 rustfmt/clippy 组件**，因此 `cargo fmt`/`cargo clippy` 在 env.sh 环境下直接报组件缺失。为得到代码层面的真实结论，额外使用宿主机 `~/.rustup` 的 stable（1.99.0，含 rustfmt 1.10.0-stable 与 clippy 0.1.99）执行了等价命令，并保留 env.sh 原始输出作为环境限制证据。所有针对源码的结论均基于实际编译/运行，不是推测。

### 2.1 工具链

```
$ source .build/env.sh && rustc --version && cargo --version
rustc 1.99.1 (58cf8f9c3 2026-10-13)
cargo 1.99.1 (5f94df478 2026-08-27)

# 宿主机 fallback 工具链（仅用于 fmt/clippy）
rustc 1.99.0 (b940084d7 2026-09-28)
cargo 1.99.0 (5f94df478 2026-08-27)
rustfmt 1.10.0-stable (b940084d7e 2026-09-28)
```

### 2.2 命令与结果

| # | 命令 | 环境 | 结果 | 说明 |
|---|---|---|---|---|
| 1 | `cargo fmt --check` | env.sh（1.99.1） | **失败（环境）**：`error: 'cargo-fmt' is not installed for the toolchain 'stable-aarch64-apple-darwin'.` | 组件缺失，非代码格式问题 |
| 2 | `cargo fmt --check` | 宿主机 1.99.0 + rustfmt 1.10.0 | **通过**，exit 0，无输出 | 等价格式检查通过 |
| 3 | `cargo clippy --locked --all-targets -- -D warnings` | env.sh（1.99.1） | **失败（环境）**：`'cargo-clippy' is not installed ...` | 组件缺失 |
| 4 | 同上 | 宿主机 1.99.0 + clippy 0.1.99 | **通过**：`Finished dev profile ... in 55.46s`，exit 0 | 0 warning / 0 error；未发现既有 clippy 告警 |
| 5 | `cargo test --locked` | env.sh（1.99.1） | **退出码 101**：lib 目标 15 个测试中 1 个失败，其余目标未继续执行 | 默认 `cargo test` 在首个失败 target 后停止 |
| 6 | `cargo test --locked --no-fail-fast` | env.sh（1.99.1） | **48 个测试执行，47 通过 / 1 失败**；`tests/install.rs` 14 个测试被整体跳过（Linux-only） | 明细见 2.3 |
| 7 | `cargo build --locked --release` | env.sh（1.99.1） | **通过**：`Finished release profile ... in 1m 06s` | `target/release/linkrd` = **2,508,192 字节（约 2.39 MiB）**，`file` 显示 Mach-O 64-bit arm64 |
| 8 | `cargo tree --locked --target aarch64-unknown-linux-musl -e features` | 宿主机 cargo | 用于静态链接/依赖分析（见 5.5） | 仅 feature 解析，未实际交叉编译 |

唯一失败的单元测试：

```
---- serial::tests::shutdown_joins_serial_workers_with_a_full_real_tty_buffer stdout ----
thread 'serial::tests::...' panicked at src/serial.rs:99:60:
called `Result::unwrap()` on an `Err` value: Error { kind: Unknown, description: "Operation not permitted" }
```

失败点是 `serialport::TTYPort::pair()`（`src/serial.rs:99`）。该测试用 `#[cfg(all(test, unix))]` 门控，因此 macOS 也会运行，但它依赖 PTY 分配；本环境里 PTY 不可用（用 Python 复核：`pty.openpty()` 报 `out of pty devices`）。因此：

- 这是**本机沙箱/环境限制**，不能据此断言 macOS 上必然失败；但在 macOS 开发机/CI 上该测试是否稳定没有保证。
- 它同时暴露了测试门控问题：`tests/install.rs` 明确 `#![cfg(target_os = "linux")]`，而 `src/serial.rs:57` 的测试模块只门控 `unix`（用例在 `:97-154`），其行为（填满真实 TTY 缓冲、测关闭时 join）本质上是 Linux 语义。

### 2.3 测试执行明细

| 测试目标 | 用例数 | 结果 | 说明 |
|---|---:|---|---|
| `src/lib.rs`（单元测试） | 15 | 14 通过 / **1 失败** | rolling 7、logging 1、install 2、install/path 1、install/startup 1、serial 1（失败）、transport 2 |
| `src/main.rs`（单元测试） | 5 | 5 通过 | CLI 冲突、`saved_args` 规范化、桌面重试取消、VID/PID/串口选择 |
| `tests/framing.rs` | 6 | 6 通过 | golden ping 向量、任意分片、CRC/长度/magic、deadline/EOF、部分写 |
| `tests/protocol.rs` | 4 | 4 通过 | Go 生成 fixture 的双向文本/确认、能力协商、被移除的 transfer 消息不能写剪贴板 |
| `tests/session.rs` | 11 | 11 通过 | 初始快照/去重、远端写确认与回显抑制、非法文本、写失败、旧 ID 重放、limits、心跳、新会话 revision=1 |
| `tests/transport.rs` | 5 | 5 通过 | TCP 字节流双工、分片、丢弃早期 Hello 后重发、周期刷新补偿、心跳超时、缺失/损坏 Hello |
| `tests/logging.rs` | 2 | 2 通过 | 文件日志落盘/退出 flush/`--no-file-log`、坏日志目录回退 stderr |
| `tests/install.rs` | **14** | **未执行（跳过）** | 文件首行 `#![cfg(target_os = "linux")]`，macOS 上整文件 0 test |
| Doc-tests | 0 | — | 无文档测试 |
| **合计** | **48 执行 + 14 跳过** | **47 通过 / 1 失败** | 首次失败会遮蔽后续 target，故必须加 `--no-fail-fast` 才有完整信号 |

### 2.4 额外的本机手工验证（补充 `tests/install.rs` 在 macOS 不可执行的问题）

在 `HOME=/tmp/linkrd-review/fakehome`、`XDG_*` 指向该目录的隔离环境中，用 release 二进制做了 macOS 冒烟（不触碰真实用户目录）：

| 动作 | 结果 | 证据 |
|---|---|---|
| `install --no-start --autostart --dir <fake>/bin -- --log-dir ...` | 成功；生成 `installation.json`、`~/Library/LaunchAgents/linkrd.plist`、`.profile/.bashrc/.zprofile/.zshrc/.config/fish/conf.d/linkrd.fish` | 输出 `installed: ...`、`login autostart registered`、`Process not started (--no-start)`；manifest 中 `sha256` 与 `startup_args` 正确 |
| `start` | 成功；写入 `runtime.lock/runtime.pid/runtime.control.json`，日志记录 `clipboard backend and watcher initialized` 与 `ACM unavailable; reconnecting ... retry_ms=500/1000/2000` | `status` 输出 `running: pid=90882`，`pid` 输出 PID |
| `stop --timeout 10` | 成功，**0.125s** 结束；日志出现 `stop requested by local command`、`linkrd stopped` | `status` → `stopped`；进程已消失；runtime.pid 被清理 |
| `uninstall` | 成功；二进制、LaunchAgent、manifest、PATH 块均被移除；日志保留 | 剩余文件只有空 dotfiles 与 `management.lock/runtime.lock/runtime-state.lock`；`autostart status` → `not installed for this user` |
| 模拟发行版场景：把二进制放到 `<fake>/bin/linkrd`（相当于 `/usr/bin/linkrd`），无安装记录，执行 `--startup` | **失败 exit 1**：`linkrd login startup failed error=cannot load startup configuration: linkrd is not installed for this user; run 'linkrd install' first` | 证实打包场景下 `--startup` 不可用（见 P1-2） |
| Linux musl 依赖图 | `cargo tree` 交叉目标 feature 解析成功；未实际编译 | 见 5.5 |

作为对照，`openpty` 在本机也不可用（`pty.openpty()` → `out of pty devices`），与 serial 测试失败一致。

### 2.5 本机未能验证的部分（明确声明）

1. **Linux 集成测试**：`tests/install.rs` 全部 14 个用例在 macOS 被 cfg 跳过；本机没有 Linux、没有 dpkg，无法执行。这些用例覆盖安装/重装/uninstall、PATH 脚本、systemd/XDG autostart、进程锁、后台启动、停止超时等关键路径，**结论只能来自代码审阅 + macOS 冒烟，不等于 Linux 全绿**。
2. **交叉编译/musl 静态链接**：本机没有 docker/cross 运行环境；README 声明的 `cross build/test` 未在本审阅中执行。musl 静态可行性的结论基于 feature 解析与依赖源码（第 5.5 节），仍需 Lead 的交叉构建实测（`file`/`ldd`/目标机运行）。
3. **真实硬件与协议互通**：没有 Radxa Linkr 设备，无法验证握手时序、DTR/清缓冲对设备的影响、心跳、重连、真实 USB VID/PID 枚举。
4. **systemd/udev/logind**：没有 Linux 环境，`contrib/systemd/linkrd.service`、`contrib/udev/70-linkrd.rules` 只做了静态审阅。
5. **Windows**：未构建、未运行；`cfg(windows)` 分支（注册表 PATH、HKCU Run、`FreeConsole`、self-replace）只做静态审阅。
6. **桌面后端可用性**：未在 GNOME/KDE/wlroots 的真实 Wayland 会话与 X11 会话中测试 `clipboard-rs` 行为。
7. **deb/AUR 构建**：无 dpkg/makepkg/lintian；只能审阅 `debian/`、`packaging/` 文本并给出就绪性判断。

---

## 3. 问题清单

> 分级：P0 = 发布/分发的硬阻断（法律或产物无法产出）；P1 = 发布前应修（构建/打包/测试信号实质性受损）；P2 = 应在 0.1.x 修复的健壮性/体验/可维护性问题；P3 = 改进项。每条给出 `文件:行号`、现象、风险、建议。

### 3.1 P0（发布阻断）

#### P0-1 仓库无 LICENSE，Cargo.toml 无 license 字段 —— 法律上不可分发

- 证据：`git ls-files` 中没有任何 `LICENSE*`/`COPYING*`；`Cargo.toml:1-6` 只有 name/version/edition/authors/description，无 `license`；README 也没有许可证段落；`debian/copyright:8-17` 因此只能写 `License: unknown` 并注明“上传 Debian archive 前必须解决”；`packaging/aur/linkrd/PKGBUILD:27` 与 `linkrd-git/PKGBUILD:22` 都是 `license=('unknown')`。
- 风险：无许可证默认是“保留所有权利”，任何人都没有复制/修改/再分发的授权。Debian 不能收录；AUR 即便技术上能提交，也不应分发无许可证作品；下游发行版、公司内分发同样有法律风险。这是本项目的第一个硬阻断。
- 建议：
  1. 由版权人 hiifong 明确选择许可证（Rust 生态常见 `MIT OR Apache-2.0`，与依赖许可兼容且便于打包；也可 GPL-2.0-or-later 等，但需作者本人确认，**审阅者不能替作者决定**）。
  2. 添加 `LICENSE-MIT`/`LICENSE-APACHE`（或单文件 `LICENSE`）、`Cargo.toml` 增加 `license = "MIT OR Apache-2.0"`，README 增加 License 段。
  3. 加 `THIRD-PARTY-NOTICES`（或 `cargo deny`/`cargo about` 生成）覆盖依赖许可；`debian/copyright` 更新为真实 License 与版权行；AUR `license=('MIT' 'Apache-2.0')` 并在 `package()` 安装到 `/usr/share/licenses/$pkgname/`。
  4. 若作者拒绝选许可证，则项目只能作为私有/内部工具，不应打 deb/AUR 包。

#### P0-2 没有发布 tag，打包资产未入库，AUR 稳定包当前不可构建

- 证据：`git log --oneline` 只有 `22c63f0 init proj`；`git status --short` 显示 `contrib/`、`debian/`、`man/`、`packaging/` 全部是 **untracked**（`??`）；`packaging/aur/linkrd/PKGBUILD:9-10` 自述 `v0.1.0.tar.gz` 返回 404、`sha256sums=('SKIP')`（第 36 行）；`source=.../archive/v$pkgver.tar.gz`（第 34 行）在没有 tag 时必然失败；`packaging/aur/linkrd/PKGBUILD` 与 `-git` 包的 `package()` 直接从源码树安装 `man/linkrd.1`、`contrib/udev/*`、`contrib/systemd/*`（`packaging/aur/linkrd/PKGBUILD:package()` 段），这些文件尚未入库，tag 里也不会有。
- 风险：`linkrd` 稳定版 AUR 包无法下载源码；Debian 的 orig tarball 若从 tag 生成会缺少 man page/udev/systemd 规则（`debian/README.debian` 与 `debian/control` 承诺安装这些文件）；即便本机能把工作区打成包，上游可复现性也不成立。
- 建议（发布流程必须一次性完成）：
  1. 把 `contrib/`、`man/`、`debian/`（或至少 debian 所需的 README.debian/rules 等）、`packaging/` 以及 LICENSE 一起 commit；
  2. 打 annotated tag `v0.1.0` 并 push；
  3. 在 tag 生成后更新 AUR `sha256sums`（`updpkgsums`），重生成 `.SRCINFO`；Debian 用 `gbp`/`uscan` 从 tag 取 orig tarball；
  4. 增加一个 release CI job（tag 触发）产出 tarball + `.deb` + AUR 校验和，避免手工不一致。

### 3.2 P1（发布前应修）

#### P1-1 实际 MSRV 是 Rust 1.89，仓库与打包文档都写 1.85；Debian 13 无法构建

- 证据链：
  - `Cargo.toml:4` 仅 `edition = "2024"`，**没有 `rust-version`**（`Cargo.toml:1-6`）。
  - 代码使用 `File::try_lock()`（`src/install.rs:61`）、`File::lock()`（`src/install.rs:79`），并 `match fs::TryLockError`（`src/install.rs:206-211`）。std 文件锁 API 在 **Rust 1.89.0** 才 stable（`fs.rs`：`#[stable(feature = "file_lock", since = "1.89.0")]`，见 <https://github.com/rust-lang/rust/blob/1.89.0/library/std/src/fs.rs>）。
  - 代码还使用 `if let ... && ...` let-chain（`src/clipboard.rs:84-87`），该语法在 Rust 1.88 / edition 2024 才稳定，进一步抬高下限。
  - 依赖侧：`Cargo.lock:1273-1275` 锁定 `tracing-appender 0.2.5`，它依赖 `time ^0.3.2`，解析到 `Cargo.lock:1232-1234` 的 `time 0.3.55`，其 `Cargo.toml` 声明 `rust-version = "1.88.0"`（还经 `wayland-protocols 0.32.13` 要求 1.86）。
  - 打包文档错误：`debian/control:6-7` `cargo (>= 1.85), rustc (>= 1.85)`；`debian/README.source:39` 与 `packaging/aur/README.md:51` 同样写 1.85。
  - Debian 13 (trixie) 的 rustc 是 **1.85.1**（<https://packages.debian.org/trixie/rustc>），不满足 1.89；本书面事实意味着 `dpkg-buildpackage` 在 stable 上会在编译 std API/依赖 MSRV 处失败。
- 风险：打包构建失败或报错难懂；发行版维护者按错误的 1.85 选工具链；没有 `rust-version` 时 cargo 无法给出“需要 1.89”的明确错误，也不能在 resolver 3 下做 MSRV-aware 选版。
- 建议：
  ```toml
  # Cargo.toml:1-6
  [package]
  name = "linkrd"
  version = "0.1.0"
  edition = "2024"
  rust-version = "1.89"
  ...
  ```
  并同步修正 `debian/control`、`debian/README.source`、`packaging/aur/README.md`、README 的构建说明；在 CI 用 1.89 做一次 `cargo check --locked` 固化 MSRV。若坚持支持 Debian 13，则需要把 `File` 锁换成 `rustix::fs::flock`（已有 rustix 依赖）或 `fs2`，并把 `time` 降到 1.85 可编译的版本/固定 `time = "=0.3.36"` 之类，但这会牺牲依赖升级空间——这是 Lead/维护者需要决策的取舍。

#### P1-2 发行包形态与 `linkrd install`/`--startup` 自管理安装冲突（打包核心问题）

这是上游设计（单文件自安装代理）与发行版包管理（`/usr/bin` + systemd user unit + 包升级）之间的结构性冲突，涉及三层：

1. **二进制位置/升级路径冲突**：`linkrd install` 默认把**当前可执行文件复制**到 `~/.local/bin/linkrd`（`src/install.rs:40-51`、`375-455`），再以 `--startup` 后台启动（`src/install.rs:539-577`）。它还会改写 shell profile（`src/install/path.rs:112-176`）并注册 XDG autostart（`src/install/startup.rs:111-121`）。发行版包必须放 `/usr/bin/linkrd`，由包管理器升级；两者同时存在会出现：用户 `~/.local/bin/linkrd` 旧副本劫持 PATH、包升级后用户仍跑旧二进制、两套 autostart 竞争同一个单实例锁。
2. **`--startup` 在包安装形态下不可用**：`--startup` 会调用 `install::startup_args()`（`src/install.rs:531-537`），它要求 `installation.json` 存在且 `current_exe == installed.executable`，否则直接报错退出。本机冒烟已证明：把二进制放到模拟 `/usr/bin` 的位置、无安装记录时执行 `--startup`，输出 `cannot load startup configuration: linkrd is not installed for this user`，exit 1（第 2.4 节表格）。因此发行版单元的 `ExecStart=/usr/bin/linkrd --startup` 不可行。
3. **桌面初始化重试只挂在 `--startup` 上**：`main::run()` 用 `initialize_desktop(args.startup, ...)`（`src/main.rs:349`），而该函数只有在 `retry=true` 时才按 1s→30s 退避重试（`src/main.rs:409-431`）。包形态的 systemd unit 用 `ExecStart=/usr/bin/linkrd`（`contrib/systemd/linkrd.service:17`），此时 `retry=false`，一旦服务在图形会话环境变量（`WAYLAND_DISPLAY`/`DISPLAY`/`DBUS_SESSION_BUS_ADDRESS`）就绪之前启动，进程立即退出；`Restart=on-failure`/`RestartSec=5s`（unit:18-19）会每隔 5 s 重启，实际相当于“重启即重试”。按 systemd 默认 `StartLimitBurst=5`/`StartLimitIntervalSec=10s` 计算，5 s 间隔最多 2 次启动/10 s，**不会**触发限流进入 failed（本团队打包说明见 `packaging/README.md` §3；未在真实 systemd 上实测）；代价是首次登录会先失败一次并留下 WARN 日志、且没有 `--startup` 那种指数退避。要彻底消除这一缺口，需要**上游提供非自安装启动模式**。
- 风险：发行版文档必须明令禁止 `linkrd install --autostart`，否则用户会制造不受包管理器管理的副本和重复自启动；若不禁用/规避，用户会看到“包升级后行为不变”“systemd 服务随机失败”“两个实例互斥报错”。
- 建议：
  - **上游（推荐）**：增加与自安装解耦的守护模式，例如 `--wait-desktop`（仅启用 `initialize_desktop` 的重试，不要求 installation.json），systemd unit 改 `ExecStart=/usr/bin/linkrd --wait-desktop --no-file-log`；或在包形态下让 `--startup` 在缺少 manifest 时退化为“使用 CLI/默认参数 + 重试”。同时让 `linkrd install` 在检测到系统包安装路径（如 `/usr/bin/linkrd` 存在 owned by package）时拒绝执行并提示使用 systemctl。
  - **打包侧（不改变上游也可行）**：unit 用普通 `ExecStart=/usr/bin/linkrd`，但设置 `Restart=always`、`RestartSec=10s`、`StartLimitIntervalSec=0`，并确保 `After=graphical-session.target` 且会话环境已导入（GNOME/KDE 的 systemd 用户实例通常会 import environment，但 Ubuntu/GNOME 以外不保证；需要在目标桌面实测）。同时 unit 加 `--no-file-log`（journald 已收 stderr）避免双写日志。
  - 文档明确“发行版包用户不要运行 `linkrd install` / `linkrd autostart enable`；`linkrd status/pid/stop` 可用，`linkrd start` 因缺少 installation.json 不可用”。当前 `debian/control` 已写了类似警告，但上游 README 没有。

#### P1-3 Debian 构建需要联网抓 crates，不符合 archive 构建环境

- 证据：`debian/rules:18` `cargo fetch --locked`；`debian/README.source:39-44` 也说明需要先联网 fetch（或预置 CARGO_HOME/vendor）。`debian/control:6-7` 的 Build-Depends 没有 vendor 方案。
- 风险：Debian buildd/sbuild 默认无网络；`cargo fetch` 会让构建失败，或产生不可复现构建。即使本机打 deb 成功，也不满足 archive 上传要求。
- 建议：在 `debian/` 内 vendoring（`cargo vendor vendor/` + `debian/cargo-checksum.json`/`dh-cargo` 或 `debian/rules` 指定 `--offline` + `CARGO_HOME=debian/cargo-home` 预置），或改用 `dh-cargo`；同时把 `CARGO_NET_OFFLINE=true` 固化。若只面向内部/自托管二进制仓库，则应明确声明不追求 Debian archive 合规。

#### P1-4 macOS 上 `cargo test` 非全绿（serial PTY 测试）

- 证据：`src/serial.rs:57` 的 `#[cfg(all(test, unix))]` 与 `:97-154` 的测试体；第 99 行 `serialport::TTYPort::pair().unwrap()` 在本机 panic：`Operation not permitted`；本机 PTY 资源不可用（Python `pty.openpty()` → `out of pty devices`）。`tests/install.rs:1` 已正确用 `#![cfg(target_os = "linux")]` 整体跳过。
- 风险：macOS 开发/CI 上 `cargo test` 失败，可能掩盖真正的回归；该测试名也表明它验证的是 Linux TTY 关闭语义，却对 macOS 生效。
- 建议：
  ```rust
  #[cfg(all(test, target_os = "linux"))]   // src/serial.rs:57（替换原 unix 门控）
  mod tests { ... }
  ```
  或保留 unix 门控但在 `TTYPort::pair()` 失败时 `eprintln!` 并返回（skip），并在 CI 加 Linux job 保证该测试真跑。

#### P1-5 `linkrd install` 与包管理器的 PATH/自启动副作用需要上游层面收敛

- 证据：`src/install/path.rs:112-176` 的 `plan()` 对固定 5 个位置（`.profile`、`.bashrc`、`$ZDOTDIR/.zprofile`、`.zshrc`、fish conf.d，外加存在的 `.bash_profile`/`.bash_login`）**无条件**登记 block（第 128-141 行），不检查目标目录是否已在 `PATH`；`enable()`（第 166-176 行）随后逐个追加。macOS 冒烟中，一个**全新的 HOME** 被创建了 5 个 dotfile；`uninstall` 后这些文件保留为空文件（非删除）。此外 `install` 没有检查 euid，README 只“建议”不要 sudo（README.md:52-55），以 root 运行会把文件装到 `/root` 且改 root 的 profile。
- 风险：对用户 home 的侵入式修改（即使用户从不使用 bash/fish）；`~/.local/bin` 通常已在 PATH，仍然追加无意义 block；发行版用户误用会污染 dotfiles；以 root 运行属于误用但当前不拦截。
- 建议：
  1. `plan()` 先检查当前 `PATH`（以及配置文件中已含的路径），若已包含安装目录，则跳过写 profile；至少只修改“存在且匹配已检测到的登录 shell”的文件，不主动创建不存在的 dotfile（fish conf.d 可作为例外并注明）。
  2. 增加 `--no-path`（只装二进制/不改 profile）与 `--force`（覆盖非受管文件）等显式选项；README/help 说明各文件会被修改。
  3. 在 `install()` 入口处 `ensure!(rustix::process::geteuid().as_raw() != 0, "do not run linkrd install as root; use the distribution package")`（保留 `--dir` 的受控例外需显式确认）；tests 中已考虑 root（`tests/install.rs:293-295`），说明作者也意识到 root 场景。
  4. 发行版文档明确“不要运行 linkrd install”。

### 3.3 P2（健壮性/体验/可维护性）

#### P2-1 重复的非 Hello 消息会直接终止会话（含重复 Ping）

- 证据：`src/session.rs:160-176`。`message_id <= highest_received` 时，只有 `ClipboardSetRequest` 会走幂等缓存；其它 payload 一律 `Err(SessionError::Invalid("replayed message ID"))`。重复 `Ping`/`Pong`（例如设备重传）会判死并触发重连。
- 风险：串口本身可靠，但设备端重传、固件 bug、或未来 ACMP 重传机制都会让会话反复重连；重连代价是丢快照、重新握手。
- 建议：对 `Ping` 重复消息幂等处理（丢弃或重新回 Pong，不关闭会话）；对未知/重复 payload 记录 debug 日志后忽略而不是破坏会话；真正的协议错误（session_id/version/CRC）再终止。

#### P2-2 `ClearBuffer::All` 紧跟在 DTR 之后，可能丢掉设备启动时发送的 Hello（硬件相关）

- 证据：`src/main.rs:369-377`：打开串口 → `write_data_terminal_ready(true)` → `clear(ClearBuffer::All)` → clone reader → 进入 `run_connection` 立即发本端 Hello。`ClearBuffer::All` 同时丢弃收/发缓冲里的“陈旧数据”，但如果 Linkr 设备在检测到 DTR/端口打开后先发送 Hello，且该 Hello 已进入驱动缓冲，就会被清掉。
- 风险：若设备只在连接时发一次 Hello，而本端周期 Hello（`src/transport.rs:175-180`）不能促使设备重发，则握手会 5s 超时（`src/transport.rs:142-144`）→ 重连 → 再次清缓冲，形成稳定失败循环。现有测试只覆盖“本端 Hello 被对端丢弃后重发”（`tests/transport.rs:88-145`），没有覆盖“对端 Hello 被本端清掉”。
- 建议：硬件上验证；可改为只清输出缓冲/只丢弃本端未完成的帧、或在 DTR 前清输入、或握手失败后不清输入直接重试；至少加一行注释说明该命令是为清上会话残留帧。

#### P2-3 日志写在 XDG data 目录，且与 journald 双写；轮转按 UTC 跨日

- 证据：`src/logging.rs:36-41`（`data_local_dir()/linkrd/logs`）、`src/logging.rs:14-15`（10MiB、7 个文件）、`src/logging/rolling.rs:36-38,92`（`Utc::now().date_naive()` 判断跨日）。systemd/XDG autostart 运行时 stderr 已进 journald，文件日志是第二份。
- 风险：日志属于状态数据，放 `XDG_STATE_HOME`（`~/.local/state`）更符合 XDG 语义；UTC 日界在中国时区对应早上 8 点，用户会觉得“日志莫名其妙轮转”；journald + 文件双写增加 I/O。
- 建议：日志目录改用 `dirs::state_dir()`（不存在则回退 data）；systemd unit 加 `--no-file-log`；或明确文档说明 UTC 轮转；包维护者不应依赖日志轮转做 logrotate（文件是自管理的 10MiB×7）。

#### P2-4 日志目录回退到共享临时目录

- 证据：`src/logging.rs:37-39`：`dirs::data_local_dir().unwrap_or_else(std::env::temp_dir).join("linkrd").join("logs")`。
- 风险：当 `HOME`/`XDG_DATA_HOME` 不可用时，日志落到 `/tmp/linkrd/logs`（可预测路径）。多用户机器上其他用户可预先创建该路径/符号链接，影响日志写入（本地提权面不大，但属于可避免的隐患）。
- 建议：无法确定用户数据目录时只写 stderr（`--no-file-log` 行为），不要用可预测的临时目录。

#### P2-5 无 CI、无 CHANGELOG、上游无 man page；systemd unit 的 `Documentation=man:linkrd(1)` 目前悬空

- 证据：仓库无 `.github/`/`.gitlab-ci.yml`；无 `CHANGELOG*`；`git ls-files` 无 `man/`；`contrib/systemd/linkrd.service:11` 引用 `man:linkrd(1)`；`man/linkrd.1` 是本次打包工作区新增、尚未入库。
- 风险：没有自动化质量门禁，MSRV/依赖/跨平台回归只能靠人；man page 缺失导致 `man linkrd` 失败、Debian lintian 可能报 `binary-without-manpage`（取决于包是否安装 man page）；用户无法了解子命令细节。
- 建议：CI 至少矩阵：Linux（fmt/clippy/test/build + 交叉 musl）、macOS（fmt/clippy/test，serial 测试按 P1-4 门控）、Windows（build，可选 test）；加 `cargo-deny`（license/advisory/ban）与 `cargo-audit`；提供 `CHANGELOG.md`（Keep a Changelog 格式）；把 `man/linkrd.1` 纳入上游。

#### P2-6 安装/升级流程文档不完整：`install` 需要先 stop，且不能改安装目录

- 证据：`src/main.rs:208-232`（`Install` 子命令在 `manage` 中先 `install::lock(directory,"runtime.lock")`，运行中会失败）；`src/install.rs:391-396`（已有安装时 `ensure!(previous.executable == executable, "uninstall before changing the installation directory")`）。
- 风险：用户按 README 直觉“再跑一次 install 升级”会得到 `linkrd is already running`；改目录必须先 `uninstall`（而 uninstall 又要求 stop）。README.md:40-41 只写了 uninstall 前 stop，没有写升级。
- 建议：README 增加“升级自安装副本：`linkrd stop && linkrd install [-- ...]`；换目录：stop → uninstall → install --dir”；错误信息里给出下一步命令；或实现 `linkrd update <new-binary>` 的一等命令。

#### P2-7 出站队列满会终止会话（设备慢/大帧时）

- 证据：`src/transport.rs:19`（`QUEUE_CAPACITY=8`）、`55-56`（两个 sync_channel）、`117-122`（`try_send` 失败直接 `anyhow!("outgoing queue full or closed; reconnect to refresh state")`）。一帧最大 2MiB，队列最坏 16MiB + incoming 16MiB。
- 风险：USB 串口在设备端处理慢、或 `ClipboardChanged` 大文本 + 心跳/响应叠加时，可能触发“队列满 → 断开重连”，表现为剪贴板同步抖动；重连不会丢数据（重新发快照），但频繁抖动不友好。
- 建议：对心跳/响应等小控制帧保留专用通道或优先队列；队列满时等待可写（带超时）而不是立即失败；或增大队列并记录 metrics；至少在日志里说明重连原因。

#### P2-8 每 20ms 的 tick 与每秒剪贴板读取对功耗不友好

- 证据：`src/transport.rs:20`（`TICK=20ms`）、主循环 `recv_timeout(TICK)`（`145`）、`src/transport.rs:187-199`（心跳/刷新）；`src/main.rs:398-402`（重连等待 50ms 轮询）；`src/install.rs:134`（stop 文件每 50ms 读）。即使空闲，会话循环每秒唤醒 50 次，并在 ready 后每 1s 读取一次剪贴板（`session.observe`，X11/Wayland 往返）。
- 风险：桌面代理长期常驻，空闲 CPU 唤醒和剪贴板读取（部分后端每次读会创建/销毁数据源）会耗电、增加日志/DBus 噪声；在 Wayland 上频繁读取剪贴板可能干扰选择所有权。
- 建议：空闲时用 `recv_timeout` 更长（200ms~1s）或条件变量唤醒；仅当 `dirty` 或心跳/超时到期时唤醒；周期刷新可放宽到 2~5s（协议兼容性允许的话）；stop 监控可用 inotify（Linux）/`poll` 替代 50ms 轮询。

#### P2-9 Windows/macOS 代码有实现但未文档化/未验证

- 证据：`Cargo.toml` 有 `cfg(windows)` 与 `cfg(target_os="macos")` 依赖；`src/install/startup.rs` 实现 LaunchAgents、HKCU Run；README 只提 Linux/Windows，完全没提 macOS；也没有 Windows/macOS CI 或测试。`tests/install.rs` 仅 Linux。
- 风险：平台代码漂移，用户按 README 在 macOS 上使用会撞上未测试路径（本次 macOS 冒烟覆盖了 install/start/stop/uninstall，但 PATH/LaunchAgent 细节与交互式 TTY 未覆盖）；Windows 的注册表/`FreeConsole` 逻辑零验证。
- 建议：明确支持矩阵（Linux 优先，Windows 实验性，macOS 实验性）；为 macOS/Windows 增加 CI build + 最小 CLI 测试；README 增加对应平台说明与限制。

### 3.4 P3（改进项）

1. **重复 Ping 之外的严格重放策略**（`src/session.rs:160-176`）：旧写入请求若已不在 `last_write` 缓存中，会回 `InvalidRequest`；若对端把它当失败重试，会反复得到错误。可考虑保留最近 N 个请求的应答，或对“ID 低于最高但内容是唯一最近快照”的情况容忍。影响：低（串口可靠）。
2. **`last_write` 仅缓存最后一次**（`src/session.rs:27-31,255-263`）：大请求（>1MiB+512）不缓存，重复即报错。低风险，建议文档化或保留小环形缓存。
3. **`stop`/`status` 的 PID 竞态**（`src/install.rs:196-219`）：`try_lock` 得到 WouldBlock 后进程可能立即退出，随后读到的 `runtime.pid` 已是陈旧值，`status` 会短暂显示 running。可在读取 pid 后再次 `try_lock` 确认；影响很小。
4. **uninstall 残留文件**：`uninstall` 只删二进制/manifest/autostart，保留 `management.lock`、`runtime.lock`、`runtime-state.lock` 以及安装时创建的**空 dotfiles**（macOS 冒烟可见）。可在 uninstall 时清理本工具创建的锁文件，或在文档说明；dotfiles 不宜自动删除（用户可能已添加内容），但可以在安装时避免创建空文件（见 P1-5）。
5. **`linkrd install` 的“目录已在 PATH”仍打印 export 提示**（`src/install/path.rs:210-229`）：用户会在已生效的环境里再次 export；建议先检测 PATH 并只在必要时提示。
6. **日志记录超大单行会被拆分到多个轮转文件**（`src/logging/rolling.rs:51-68`）：`write_inner` 在单条记录 >10MiB 时先写满当前文件再在下一次 `write` 里轮转，记录被拆开。极端情况才会发生；可在注释/实现中处理或忽略。
7. **`--log-dir` 相对路径按安装时 CWD 解析**：`src/main.rs:140-149` 用 `std::path::absolute`，macOS 冒烟中 `--log-dir "custom logs"` 被保存为 `<repo>/custom logs`（当时 CWD）。建议在 help/README 说明相对路径相对当前目录，或改为相对 `$HOME`；同时安装后把路径写进输出。
8. **配置目录内文件被当作可信输入**：`Installation`/`path::Configuration` 从 `~/.config/linkrd/installation.json` 读取（`src/install.rs:287-306`、`path.rs:112-164`），`uninstall/disable` 会按记录路径修改文件。同用户攻击者本就能改这些文件，不构成提权；但建议校验 profiles 都在 `$HOME` 下，防止损坏的 manifest 造成意外写入。低优先级。
9. **生成协议代码无来源/版本注释**：`src/protocol/linkrd.protocol.v1.rs` 是 `include!` 的生成文件（`src/protocol.rs:1`），README 说明仓库不存 `.proto`（README.md:77-78）。建议在文件头加“generated from <proto> with prost-build x.y, do not edit”，并在 `docs/` 保存协议定义/生成命令，便于审计和再生成；不影响当前构建（无 protoc 依赖）。
10. **`management.lock` 语义在错误信息中混同**：`src/install.rs:61-67` 对 `runtime.lock` 的冲突统一提示“already running (or an installation is in progress)”，用户无法区分；可细分提示。
11. **`--baud` 对 CDC-ACM 无实际意义**（`src/main.rs:58-60` 已注释说明），保留兼容；无需改。
12. **`clear` 输出缓冲在关闭时可能失败**（`src/serial.rs:5-21` 仅 warn 后继续关闭）：正确，只是驱动可能等 pending 输出；已有测试覆盖满缓冲关闭。

---

## 4. 测试覆盖评估与缺口

### 4.1 覆盖较好的部分（有针对性、断言细）

- **帧层**（`tests/framing.rs`）：golden 向量、任意 split 边界 + TimedOut/Interrupted 交错、CRC/长度/magic/解码错误、deadline 与 EOF 区分、部分写不重复字节——覆盖度好。
- **协议状态机**（`tests/session.rs` + `tests/protocol.rs`）：初始快照/revision、去重、远端写确认顺序（Changed 先于 Response）、重复请求幂等、旧/复用 ID 拒绝、非法 MIME/UTF-8/NUL/TooLarge、未协商写能力、心跳 Pong 关联、新会话 revision 重置；并且用 Go 生成的 fixture 校验双向文本帧与确认帧，是**协议互通性最有力的证据**。
- **传输层**（`tests/transport.rs`）：用 TCP 流模拟字节流，覆盖分片、首帧丢弃、双工、去重、周期刷新补偿、心跳超时、缺失/损坏 Hello；对无硬件环境很实用。
- **安装/运行时**（`tests/install.rs`，仅 Linux）：覆盖首次安装/重装/自卸载、PATH 脚本幂等、符号链接与权限保留、部分写入失败后可重试、autostart 冲突、被外部修改的二进制拒绝覆盖/删除、`runtime.lock` 阻止更新/卸载、后台进程启动与 PID/status、强制 kill 后 status 回落到 stopped、stop/start 循环、超时参数。设计得非常细，遗憾是本机不能执行。
- **日志**：文件落盘/退出 flush/禁用文件日志/坏目录回退、轮转边界（10MiB、跨日、oversize 记录、轮转失败停止写入、非 Unicode 目录不 panic）、协议日志不泄露剪贴板内容（`src/logging.rs:178-228` 断言）。

### 4.2 缺口（按重要性）

1. **Linux 集成测试在 macOS 上完全跳过**：`tests/install.rs` 14 个用例没有本机证据；本次只能用 macOS 冒烟覆盖其中一部分（install/no-start、autostart、start/stop/uninstall、PATH 清理），**未覆盖**：PATH 符号链接/权限保留、部分失败重试、`.bash_profile`、fish、现有 PATH 冲突、同时运行的锁竞争、`--startup` 损坏 manifest 的日志路径、后台进程崩溃检测、`stop` 超时保留请求等。发布前必须在 Linux 上跑一遍。
2. **没有真实/模拟串口层测试**：`tests/transport.rs` 用 TCP，`src/serial.rs` 只有“满 TTY 缓冲时关闭”的测试；`serial::open` 的 termios 设置、DTR、`ClearBuffer`、非阻塞/超时行为没有单元测试，也无硬件 E2E。P2-2（清缓冲丢 Hello）属于此类风险。
3. **没有重连循环测试**：`main::run()` 的 500ms→5s 退避、连接维持 ≥10s 重置、shutdown 中断重试、`initialize_desktop` 与重连的交互只有 `initialize_desktop` 的单元测试，没有整体循环测试（可用可注入的 port opener/时钟抽象来测）。
4. **没有剪贴板后端测试**：`SystemClipboard`/`ClipboardMonitor` 只靠真实桌面环境；`with_platform_pool`（macOS autorelease pool）没有测试；watcher 线程 panic/退出后的行为（`src/main.rs:359-364` 只 warn 一次）没有测试。Wayland 各 compositor 的 data-control 协议支持差异（ext-data-control vs wlr-data-control）完全未验证。
5. **心跳/错误帧互通 fixture 缺失**：fixtures 只覆盖 Hello/text/Changed/Applied（Go 生成），Ping/Pong/Error 的 golden 向量来自自身编码（`tests/framing.rs:22-32` 的 hex 无法证明与 Go 实现一致）；建议补 Go 生成的 Ping/Pong/Error fixture（尤其 `ErrorCode` 数值、`reply_to` 编码）。
6. **无 fuzz/属性测试**：`FrameReader` 是最该 fuzz 的入口（长度、CRC、分片、超时）；可用 `cargo-fuzz`/`proptest` 做“任意字节流不 panic、不越界、不无限等待”。
7. **无覆盖率统计/门禁**：没有 `cargo llvm-cov`/codecov；安装模块的 Windows/macOS 分支、`path.rs` 的 Windows 注册表逻辑、`startup.rs` 的 Windows Run 键完全无测试。
8. **真实桌面/显示服务器矩阵缺失**：X11、Wayland（GNOME/Mutter、KDE/KWin、wlroots）、无会话的 systemd 启动都未测；这与 P2-9 和 P1-2 相关。
9. **安装器在“PATH 已包含目标目录”场景无测试**：`path::plan` 不检查 PATH，也没有相应用例（可加：预设 PATH 含 `~/.local/bin` 时应不写 profile 或不改动）。
10. **`--startup` 在无安装记录场景无测试**：本次手工验证发现它直接失败，这正是发行版 unit 不能直接使用 `--startup` 的原因；建议加 CLI 集成测试明确该契约（要么预期失败并给清晰提示，要么未来改成可用）。

### 4.3 覆盖度定性结论

| 模块 | 覆盖度 | 评价 |
|---|---|---|
| framing | 高 | 边界/错误/分片/超时齐全，建议补 fuzz |
| protocol/session | 高 | 状态机与互通 fixture 好；缺 Ping/Pong/Error Go fixture、重复 Ping 场景 |
| transport | 中高 | TCP 模拟充分；缺真实 serial、重连循环、队列满场景 |
| clipboard | 低 | 依赖真实桌面，无单元测试；watcher 生命周期弱覆盖 |
| serial | 低（仅关闭语义） | 打开/termios/DTR/清缓冲无测试，且 macOS 测试门控错误 |
| install/runtime | 中高（但本机未执行） | 用例设计细；需 Linux 实跑 + 覆盖 PATH 已在环境、root 拒绝、升级流程 |
| logging | 高 | 轮转/回退/隐私断言好 |
| 打包资产 | 无测试 | deb/AUR 只能靠 lint 工具（本机无） |

---

## 5. 发行版打包就绪性（供仓库维护者与打包者）

### 5.1 结论：当前不适合发布，先解决 5 个上游阻断项

| # | 阻断项 | 当前状态 | 影响 | 必须由谁决定/修改 |
|---|---|---|---|---|
| B1 | LICENSE + Cargo license | 缺失（P0-1） | 法律上不可分发；Debian copyright 只能写 unknown | 版权人 hiifong |
| B2 | 发布 tag + 打包资产入库 | 只有 `init proj`；`contrib/debian/man/packaging` 未跟踪（P0-2） | AUR stable 源码 404、sha256 SKIP；Debian orig 缺 man/udev/unit | 上游维护者 |
| B3 | MSRV/`rust-version` | 实际 1.89，文档写 1.85（P1-1） | Debian 13 无法构建；打包选择工具链出错 | 上游 + 打包者 |
| B4 | 包形态 vs `install/--startup` | unit 不能 `--startup`；无桌面重试模式（P1-2） | systemd 服务在会话环境未就绪时会失败 | 上游（推荐加 flag）+ 打包者 |
| B5 | Debian 离线构建 | `cargo fetch` 需要网络（P1-3） | 不能进 Debian archive | 打包者 |

### 5.2 推荐的上游元数据（一次性改好）

当前 `Cargo.toml:1-6`：

```toml
[package]
name = "linkrd"
version = "0.1.0"
edition = "2024"
authors = ["hiifong <f@f.style>"]
description = "USB ACM text clipboard synchronization agent for Linkr"
```

建议（`license` 值以作者确认为准）：

```toml
[package]
name = "linkrd"
version = "0.1.0"
edition = "2024"
rust-version = "1.89"                     # 实际下限：File::lock + MSRV 1.88 的依赖
authors = ["hiifong <f@f.style>"]
description = "USB ACM text clipboard synchronization agent for Linkr"
license = "MIT OR Apache-2.0"             # 需作者书面确认
repository = "https://github.com/radxa-linkr/linkrd"
homepage = "https://github.com/radxa-linkr/linkrd"
readme = "README.md"
keywords = ["clipboard", "usb", "cdc-acm", "linkr", "wayland"]
categories = ["command-line-utilities"]
```

同时补 `LICENSE-MIT`/`LICENSE-APACHE`、`CHANGELOG.md`、`debian/copyright` 的真实 License 段、AUR `license` 与 `/usr/share/licenses/`。`Cargo.lock` 已提交（好），但注意 lock 中的 `time 0.3.55` 要求 1.88；如果将来为了支持更老工具链要降级，需要显式 pin 并回归。

### 5.3 两种部署模型的对照（发行版必须选"包管理"模型）

| 维度 | 上游自安装（README 当前推荐） | 发行版包（deb/AUR 应走） |
|---|---|---|
| 二进制 | `~/.local/bin/linkrd`（`install.rs:40-51`） | `/usr/bin/linkrd` |
| 升级 | 用户手动 `linkrd stop && linkrd install`；无网络更新 | `apt upgrade`/`pacman -Syu` |
| 登录启动 | XDG autostart / LaunchAgent / HKCU Run（`startup.rs`） | systemd **user** unit（`contrib/systemd/linkrd.service`） |
| 单实例 | `~/.config/linkrd/runtime.lock`（flock） | 同一个锁（兼容），但不要同时启用 autostart |
| PATH 修改 | 改 5~6 个 profile（`path.rs`） | **不修改用户 dotfiles** |
| udev 权限 | 用户自行处理 dialout/uucp | `/usr/lib/udev/rules.d/70-linkrd.rules`（uaccess ACL） |
| 日志 | `~/.local/share/linkrd/logs` 自轮转 10MiB×7 | journald（stderr）+ 可选文件日志；建议 `--no-file-log` |
| 用户命令 | `linkrd start/stop/status/autostart/uninstall` | `systemctl --user ...` + `linkrd status/pid/stop`（`start/autostart/uninstall` 因无 manifest 不适用） |

**硬性建议**：
- 包中 **不要** 安装 XDG autostart/LaunchAgent 条目；只装 systemd user unit 且 **`--no-enable`**（当前 `debian/rules` 的 `dh_installsystemduser --no-enable` 方向正确；`packaging/aur/*/linkrd.install` 只打印提示、不 enable，方向正确）。
- 包文档必须显著警告“不要运行 `linkrd install --autostart`”（当前 `debian/control` 已写；上游 README 没有，需补）。
- 若上游实现 P1-2 的 `--wait-desktop`，unit 改 `ExecStart=/usr/bin/linkrd --wait-desktop --no-file-log`；否则 unit 需要 `Restart=always`/`StartLimitIntervalSec=0` 并在目标桌面实测环境变量导入。
- `linkrd status/pid/stop` 对包形态可用（runtime 文件按 XDG config 目录定位），这可以作为包的辅助 CLI；但 `linkrd start` 会因 `require_installation()` 报“not installed”（`install.rs:274-281`），需要在文档说明，或上游在包路径下放宽（检测 `/usr/bin/linkrd`）。

### 5.4 udev / systemd / 日志 / 路径（包内文件）

- **udev**（`contrib/udev/70-linkrd.rules:7`）：`SUBSYSTEM=="tty", ATTRS{idVendor}=="1d6b", ATTRS{idProduct}=="0104", TAG+="uaccess", GROUP="dialout", MODE="0660"`。
  - `uaccess` 是正确方向（登录 seat 用户获得 ACL，无需入 dialout 组）。
  - 建议追加 `ENV{ID_MM_DEVICE_IGNORE}="1"`（避免 ModemManager 把 CDC-ACM 当调制解调器探测、发送 AT 命令干扰协议）；如果设备同时暴露多个接口，可加 `ENV{ID_MM_PORT_IGNORE}="1"`。
  - 包路径 `/usr/lib/udev/rules.d/`（debhelper `dh_installudev --priority=70` 会放对位置；AUR 已用该路径）。
- **systemd user unit**（`contrib/systemd/linkrd.service`）：`After=graphical-session.target`、`PartOf=` 正确；`WantedBy=graphical-session.target`、不 enable，正确。现有加固 `NoNewPrivileges`/`RestrictSUIDSGID`/`LockPersonality` 合理；可考虑 `UMask=0077`（日志 600/700）、`SystemCallFilter=@system-service` 等可选加固（需验证剪贴板 socket/serial 不被误杀）。
- **日志**：默认 `~/.local/share/linkrd/logs/linkrd.log`（`logging.rs:36-41`），10MiB×7、按 UTC 日轮转（`rolling.rs:36-38,92`）。包形态建议 unit 传 `--no-file-log`（journald 已收 stderr），或保留文件日志但说明不归 logrotate 管；不要把日志放进 `/var/log`（用户级代理没有权限且不应）。
- **状态/配置路径**：`XDG_CONFIG_HOME/linkrd/{installation.json,runtime.lock,runtime.pid,runtime.control.json,runtime.stop,runtime-state.lock,management.lock}`；macOS 对应 `~/Library/Application Support/linkrd`。这些是用户态文件，包卸载不应删除（可能还有日志/用户配置），由 `linkrd uninstall` 管理；包 postrm 只清理 `/usr/bin`、unit、udev、man 等系统文件。
- **man page**：`man/linkrd.1`（本次新增，未入库）需要随包安装（`/usr/share/man/man1/linkrd.1.gz`），否则 unit 的 `Documentation=man:linkrd(1)` 悬空。当前 `debian/linkrd.manpages`、AUR `package()` 已引用它；务必 commit。

### 5.5 musl 静态链接可行性（基于实际 feature 解析，未实机验证）

结论：**可行，且不需要 libwayland / libX11 / libudev**。证据：

1. `clipboard-rs 0.3.5` 在 Linux 上使用 `x11rb::rust_connection::RustConnection`（纯 Rust X11 协议实现，见 registry 源 `clipboard-rs-0.3.5/src/platform/x11.rs:29,805`），不链接 libX11/libxcb；`x11rb 0.13.2` 只启用 `default`/`render`/`shape`/`xfixes`，未启用 `allow-unsafe-code` 或 `dl-libxcb`（Linux 依赖树中没有 `libloading`；`once_cell` 是 x11rb 的普通纯 Rust 依赖，不代表 dlopen）。
2. Wayland 后端：`cargo tree --target aarch64-unknown-linux-musl -e features` 显示只启用 `wayland-backend feature "default"` 与 `wayland-sys feature "default"`，**没有 `client_system` / `server_system` / `dlopen`**。wayland-backend 文档说明：`rs` 后端“always available”，`sys` 后端才由 `client_system`/`server_system` 控制，`dlopen` 才需要运行时加载系统库（<https://docs.rs/wayland-backend/0.3.17/wayland_backend/>）。因此运行时不需要 libwayland，也不会 dlopen 它。
3. `serialport 4.10.1` 使用 `default-features = false`（`Cargo.toml` 依赖声明），Linux 上不启用 `libudev`；即使启用，其 Cargo.toml 也只在 `not(target_env = "musl")` 下依赖 libudev。musl 下 `available_ports()` 走 sysfs 回退实现（读 `/sys/class/tty`、USB `idVendor`/`idProduct`/`serial`），因此**默认 VID/PID 选择在 musl 静态二进制中仍可用**（这消解了“无 udev 就无法枚举 USB 信息”的担心）。
4. 依赖树中没有需要链接系统 C 库的 `-sys` 包（Linux musl 树里只有 `linux-raw-sys`、`wayland-sys` 等纯定义/系统调用封装，且 wayland-sys 无 client/server/dlopen feature）。`objc2`/`windows-*` 只在 macOS/Windows target 出现。
5. `.cargo/config.toml` 对 musl 设置 `-C target-feature=+crt-static`，`Cross.toml` 使用 musl cross 镜像；`cross build --locked --release --target x86_64-unknown-linux-musl` 应得到静态二进制。

仍需 Lead 实机验证（本机无 docker）：在 Linux 上用 `file target/.../linkrd`（应显示 statically linked）、`ldd`（不应列出 libwayland/libX11/libudev）、并在目标机（Radxa/Linkr 使用场景）确认 clipboard Wayland/X11 与串口枚举正常。注意静态 musl 二进制不依赖 glibc 版本，适合“下载即用”的发布形态；但这与 Debian 包（应该动态链接、由 archive 构建）是两条不同分发路径，不要混用。

### 5.6 Debian 特有问题（针对当前 `debian/` 工作区）

1. **License**：`debian/copyright:8-17` 现为 `License: unknown`，debian-archive 上传会被直接拒绝；必须等上游 LICENSE（P0-1）。
2. **MSRV**：`debian/control:6-7` 写 `cargo/rustc >= 1.85`，实际 1.89（P1-1）。Debian 13 的 1.85.1 无法构建；`sid`/backports 才有机会。需要 Lead 决策：面向 trixie 用 backported rustc/自带工具链？还是只发 sid？还是放弃源码包、仅发布上游静态 musl 二进制的 deb（不符合 Debian 政策但可作第三方仓库）？
3. **离线构建**：`rules:18` 的 `cargo fetch` 需要联网（P1-3）；必须 vendor。
4. **测试**：`rules:28` 在 `dh_auto_test` 跑完整 `cargo test --release`，会执行 `tests/install.rs`（临时 HOME、启动后台进程、`/bin/sh`、文件锁）。在 buildd 上虽然大多可行，但属于“测试会启动子进程/依赖 shell”的脆弱做法；建议 CI 跑全量，包构建用 `DEB_BUILD_OPTIONS=nocheck` 或只跑 `--lib`，并在 README.source 说明。
5. **README 命名**：`debian/README.debian` 是自定义小写；Debian 惯例/自动安装是 `debian/README.Debian`（大写 D），`dh_installdocs` 会自动装。建议改名（当前 `rules` 的手动 install 可以删掉），避免 lintian 风格问题。
6. **资产完整性**：`debian/linkrd.udev`、`debian/linkrd.user.service` 是对 `../contrib/` 的符号链接（`ls -l` 可见），这要求源码包同时包含这两个上游文件；当前它们未入库（P0-2）。`debian/linkrd.manpages` 指向 `man/linkrd.1` 同理。
7. **Homepage/Vcs 字段**：`debian/control` 已写 GitHub 地址，OK；`debian/watch` 用 GitHub tags 模式，等第一个 tag 后才有效。
8. **包内容承诺**：`debian/control` 描述承诺安装 `/usr/bin/linkrd`、udev rule、systemd user unit；当前 `debian/linkrd.install` 只装二进制，udev/unit 依赖 debhelper 覆盖与符号链接，务必实际 `dpkg-buildpackage -b` + `lintian` 验证（本机无 dpkg/lintian，未做）。

### 5.7 AUR 特有问题（针对当前 `packaging/aur/`）

1. **稳定包源码 404 / sha256 SKIP**：`linkrd/PKGBUILD:9-10,34,36`；必须等 `v0.1.0` tag 后用真实 tarball + `updpkgsums`，否则 `makepkg` 直接失败（`SKIP` 只是占位，不是可分发状态）。在此之前只应发布 `linkrd-git`。
2. **license=unknown**：`linkrd/PKGBUILD:27`、`linkrd-git/PKGBUILD:22`；同上，需上游 LICENSE；随后 `license=('MIT' 'Apache-2.0')` 并在 `package()` 安装许可证文件到 `/usr/share/licenses/$pkgname/`。
3. **package() 依赖未入库文件**：`man/linkrd.1`、`contrib/udev/70-linkrd.rules`、`contrib/systemd/linkrd.service` 必须出现在 tag/tree 里（当前 untracked）。AUR 的 `source` 是 GitHub archive，不包含未提交文件。
4. **check() 跑完整测试**：`linkrd/PKGBUILD` 的 `check()` 执行 `cargo test --release --locked --frozen`；在 Arch 上大概率可过（pty、/bin/sh 都有），但会启动后台进程和临时 HOME；若不稳定，文档已建议 `makepkg -C`/`--nocheck`。建议在 CI 里用 Arch 容器跑一次 `makepkg`。
5. **依赖声明**：`depends=('gcc-libs')` 对 glibc 动态 Rust 二进制是正确的（libgcc_s）；Wayland/X11 纯 Rust，不应加 `libwayland`/`libx11`（AUR 注释也这么写，正确）。若未来改为 musl 静态，则依赖列表需重新评估（大概率无 glibc/gcc-libs 依赖）。
6. **systemd/udev 安装**：路径 `/usr/lib/systemd/user`、`/usr/lib/udev/rules.d` 正确；`linkrd.install` 的 `udevadm control --reload-rules` 与 `trigger` 合理；不自动 enable 正确。
7. **-git 包**：`provides=('linkrd')`、`conflicts=('linkrd')` 正确；`pkgver()` 在上游有 tag 后会自动切换；`pkgver=0.1.0.r1.g22c63f0` 只是占位，提交前应 `makepkg -o` 重算并重生成 `.SRCINFO`。
8. **AUR 提交前**：把 `Maintainer: TODO <todo@example.invalid>` 换成真实维护者；`license=unknown` 会被 AUR 评论指出；补 `README`（当前 `packaging/aur/README.md` 是打包说明，不是包内文件，OK）。

### 5.8 Windows（不在 deb/AUR 范围，但影响发布口径）

`Cross.toml` 声明 `x86_64-pc-windows-gnu`；README 宣称 Windows 支持；但本审阅无法构建/运行 Windows。Windows 路径（注册表 PATH、HKCU Run、`FreeConsole`、`CreateProcess` detached）仅在代码层面看起来自洽（`startup.rs:147-204`、`install/path.rs:231-409`、`main.rs:268-272`）。建议至少补一次 `cross build --target x86_64-pc-windows-gnu` 到 CI，并在 README 标注“experimental”。

---

## 6. 优先级建议

### 发布前必须修（Must-fix，0.1.0 tag / 首个包之前）

1. **P0-1 LICENSE + Cargo license + 依赖许可清单**（作者决策，不能替作者选）。
2. **P0-2 打包资产入库 + `v0.1.0` tag + AUR sha256 + Debian orig tarball 一致性**。
3. **P1-1 `rust-version = "1.89"` + 全部打包文档的 1.85→1.89 修正 + CI 用 1.89 编译校验**。
4. **P1-2 包形态启动策略**：上游加与自安装解耦的 `--wait-desktop`（推荐）或包侧 unit 使用 `Restart=always` + `StartLimitIntervalSec=0` 并在 GNOME/KDE 实测；包文档禁止 `linkrd install`。
5. **P1-3 Debian vendoring / 离线构建**（若目标是 Debian archive）。
6. **P1-4 serial 测试按 `target_os = "linux"` 门控**，保证 macOS 上 `cargo test` 可全绿；在 Linux 实跑 `tests/install.rs` 14 个用例。
7. **P1-5 至少加 root 拒绝与“PATH 已含目录则不写 profile”**，并在 README 写清 install 会修改哪些文件。

### 0.1.x 应修（Should-fix）

- P2-1 重复 Ping 幂等；P2-2 清缓冲/DTR 时序按硬件实测调整；P2-7 出站队列满的处理。
- P2-3/P2-4 日志目录（XDG state、避免 /tmp 回退、`--no-file-log` in unit）。
- P2-5 CI（Linux/macOS matrix、cargo-deny/audit、跨平台 build）与 CHANGELOG/man page 入库。
- P2-6 升级流程文档/命令；P2-8 空闲 CPU/剪贴板轮询优化；P2-9 明确支持矩阵。
- 补 Ping/Pong/Error 的 Go fixtures、`FrameReader` fuzz、重连循环测试、覆盖 `--startup` 无 manifest、PATH 已含目录、root 场景。

### Nice-to-have

- 安装模块 Windows/macOS 分支测试与 CI；`--no-path`/`--force` 等安装选项；日志轮转的本地时区/超大行处理；`Installation` 记录的 profile 路径校验；协议 `.proto` 与生成指令留档；包升级时检测“旧的 `~/.local/bin` 自安装副本”并提示迁移。

### 需要 Lead 决策的事项（本次审阅无法替决策）

1. **许可证选择**：必须由版权人确认（MIT OR Apache-2.0 是最省事的 Rust 生态默认，但不能替作者拍板）。
2. **Debian 构建策略**：trixie 的 rustc 1.85.1 无法满足 1.89——是 (a) 只面向 sid/未来版本，(b) 从 backports/sid 取 rustc 构建 trixie 源包，(c) 不做 Debian archive，只发布上游静态 musl 二进制的第三方 deb，(d) 上游降低 MSRV（改文件锁实现/降 time 依赖）？这直接影响 `Cargo.toml` 是否要改代码。
3. **是否允许修改上游源码**以加 `--wait-desktop`/root 拒绝/PATH 检测，还是本次只交付文档与包、把这些列为 upstream TODO（本任务禁止我修改 src/，需要 Lead 决定是否另开任务）。
4. **包发布顺序**：AUR 是先只发 `linkrd-git`（可立即做，依赖 HEAD），还是等 `v0.1.0` tag 后同时发稳定包？Debian 是否等 LICENSE/rustc 问题解决再构建？
5. **`contrib/`、`man/`、`debian/`、`packaging/` 是否要合入上游仓库的一次提交**（AUR/deb 的可复现性要求；如果只作为本次交付物放在工作区，下游无法从上游 tag 构建）。

---

## 7. 附录：关键证据与命令输出摘录

### 7.1 环境/构建

```
rustc 1.99.1 (58cf8f9c3 2026-10-13) / cargo 1.99.1 (5f94df478 2026-08-27)   # env.sh
rustc 1.99.0 / clippy 0.1.99 / rustfmt 1.10.0-stable                          # 宿主机 fallback

$ cargo fmt --check            # env.sh
error: 'cargo-fmt' is not installed for the toolchain 'stable-aarch64-apple-darwin'.

$ cargo clippy --locked --all-targets -- -D warnings   # 宿主机 1.99.0
Finished `dev` profile [unoptimized + debuginfo] target(s) in 55.46s   (exit 0, 0 warning)

$ cargo build --locked --release
Finished `release` profile [optimized] target(s) in 1m 06s
-rwxr-xr-x  2508192  target/release/linkrd
target/release/linkrd: Mach-O 64-bit executable arm64
```

### 7.2 测试结果（`cargo test --locked --no-fail-fast`）

```
src/lib.rs          15 tests: 14 passed, 1 failed (serial::... TTYPort::pair EPERM)
src/main.rs          5 tests: 5 passed
tests/framing.rs     6 tests: 6 passed
tests/install.rs     0 tests (cfg(target_os = "linux"))
tests/logging.rs     2 tests: 2 passed
tests/protocol.rs    4 tests: 4 passed
tests/session.rs    11 tests: 11 passed
tests/transport.rs   5 tests: 5 passed
doc-tests            0 tests
合计 48 执行 / 47 通过 / 1 失败，14 个 install 用例被 cfg 跳过
```

失败原文：

```
thread 'serial::tests::shutdown_joins_serial_workers_with_a_full_real_tty_buffer' panicked at src/serial.rs:99:60:
called `Result::unwrap()` on an `Err` value: Error { kind: Unknown, description: "Operation not permitted" }
```

本机 PTY 复核：`python3 -c "import pty; pty.openpty()"` → `openpty FAILED out of pty devices`。

### 7.3 macOS 冒烟（隔离 HOME）

```
$ linkrd install --no-start --autostart --dir /tmp/linkrd-review/fakehome/bin -- --log-dir /tmp/linkrd-review/fakehome/logs --log-level debug
installed: /private/tmp/linkrd-review/fakehome/bin/linkrd
Command PATH configured for this user; open a new terminal to run linkrd.
login autostart registered; takes effect on next login
Process not started (--no-start).

$ linkrd start
background process launched: pid=90882; uses saved arguments; check logs for desktop/ACM readiness

$ linkrd status
running: pid=90882

$ linkrd stop --timeout 10
stopped: pid=90882            # 实测总耗时 0.125s

$ linkrd uninstall
uninstalled /private/tmp/linkrd-review/fakehome/bin/linkrd; logs retained
```

启动日志（`/tmp/.../logs/linkrd.log`）：

```
INFO linkrd::logging: file logging enabled (daily and size rotation) directory=... max_file_bytes=10485760 max_files=7
INFO linkrd: linkrd starting version="0.1.0" os="macos"
INFO linkrd: clipboard backend and watcher initialized
WARN linkrd: ACM unavailable; reconnecting error=no matching USB serial port found retry_ms=500
WARN linkrd: ACM unavailable; reconnecting error=no matching USB serial port found retry_ms=1000
WARN linkrd: ACM unavailable; reconnecting error=no matching USB serial port found retry_ms=2000
INFO linkrd::install: stop requested by local command
INFO linkrd: shutdown requested; stopping clipboard watcher
INFO linkrd: linkrd stopped
```

模拟包安装（无 installation.json）执行 `--startup`：

```
ERROR linkrd: linkrd login startup failed error=cannot load startup configuration:
  linkrd is not installed for this user; run `linkrd install` first
exit 1
```

### 7.4 MSRV 证据

```
# 代码使用（src/install.rs）
line 61:  file.try_lock()
line 79:  file.lock()
line 206-211: match file.try_lock() { Ok(()) => ..., Err(fs::TryLockError::WouldBlock) => ... }
# Rust 1.89.0 才 stable：
#   https://github.com/rust-lang/rust/blob/1.89.0/library/std/src/fs.rs
#   #[stable(feature = "file_lock", since = "1.89.0")] pub fn try_lock(...)

# 依赖侧：
Cargo.lock:1273-1275  tracing-appender 0.2.5
Cargo.lock:1232-1234  time 0.3.55
# time 0.3.55 的 Cargo.toml: rust-version = "1.88.0"
# wayland-protocols 0.32.13: rust-version = "1.86"

# 发行版实际工具链：
Debian 13 (trixie) rustc: 1.85.1  (https://packages.debian.org/trixie/rustc)
```

### 7.5 musl/依赖图证据

```
$ cargo tree --locked --target aarch64-unknown-linux-musl -e features -i wayland-backend
wayland-backend v0.3.17
└── wayland-backend feature "default"
    └── wayland-client v0.31.15 ...
        ...（无 client_system / server_system / dlopen）

$ cargo tree --locked --target aarch64-unknown-linux-musl -e features -i wayland-sys
wayland-sys v0.31.11
└── wayland-sys feature "default"     （无 client/server/dlopen → 不链接 libwayland）

$ cargo tree --locked --target aarch64-unknown-linux-musl | grep -c libudev
0

# clipboard-rs 0.3.5 Linux 实现使用 x11rb RustConnection（纯 Rust）：
#   src/platform/x11.rs:29  use x11rb::rust_connection::RustConnection
#   src/platform/x11.rs:805 conn: RustConnection
# serialport 4.10.1: default-features=false；musl 下走 sysfs 枚举（/sys/class/tty），
#   因此可用 vid/pid 默认值选口。
# wayland-backend 文档：rs 后端 always available；client_system/dlopen 才启用系统库
#   https://docs.rs/wayland-backend/0.3.17/wayland_backend/
```

### 7.6 仓库/打包工作区状态

```
$ git log --oneline
22c63f0 init proj

$ git status --short
 M .gitignore
?? contrib/
?? debian/
?? man/
?? packaging/

$ git ls-files | grep -i -E 'license|copying'   # 无输出
$ sed -n '1,6p' Cargo.toml                      # 无 license/repository/homepage/rust-version
```

---

## 8. 审阅者备注

- 本次只写 `docs/REVIEW.md`（及本文件所需的临时验证脚本放 `/tmp/linkrd-review/`），未修改 `src/`、`Cargo.toml`、`Cargo.lock`、`tests/`、`contrib/`、`debian/`、`man/`、`packaging/`、`.build/`、`dist/`。工作区中 `contrib/`、`debian/`、`man/`、`packaging/` 是其他同事并行产出的打包草稿；本报告对其只做只读参照，具体文件在并行编辑中可能变化，以最终提交为准。
- 行号基于当前工作区源码（`git status` 显示 `src/` 无改动）；若上游随后修改代码，请以 `文件:行号` 重新定位。
- 已按要求避免对无法验证的交叉编译/真实硬件做结论：musl 静态部分是“依赖图 + 依赖源码”的可以论证，但仍以 Lead 的交叉构建实测为准；Debian/AUR 构建结果以打包同事在 Linux/Arch 上的实际运行为准。
