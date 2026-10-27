# linkrd Linux 真机验证报告（QEMU 访客内实测）

> 本文档是 `packaging/README.md` §5 验证矩阵的原始报告，对应最终产物（sha256 见
> `dist/SHA256SUMS`）。复现所需的脚本与完整串口日志在工作区的 `.build/verify/`
> （构建缓存，未入库），包括 `boot2-*.log`（最终产物复验）、`host-checks-final.log`。

- 验证人：linux-verifier（Agent Team 成员）
- 时间：2026-10-27（UTC+8）；第一轮验证 + **最终产物第二轮复验**（man page `.UR` 与 postinst `#DEBHELPER#` 修复后）
- **本报告对应最终产物（第二轮复验，2026-10-27 11:22 生成）：**
  - `.build/artifacts/linkrd-linux-amd64`（musl 静态，sha256 `d4bcb61a7c03adcf0bd31c9115e528ff3e53dd0428e2a408b860e0622f763b83`；两轮未变）
  - `.build/artifacts/linkrd-linux-arm64`（musl 静态，sha256 `72ac58f1a56593173dfa968170d08da13b674ebee5b95044c66ab1210d5b4c77`；两轮未变）
  - `dist/linkrd_0.1.0-1_amd64.deb`（最终版，sha256 `e2403654a0d7e3d3211f76495940e2d1ecf81b54c73ee9a22d715d707457b20a`，1,229,026 B）
  - `dist/linkrd_0.1.0-1_arm64.deb`（最终版，sha256 `f46cf2dd88b65a7575845c9f7e18f941566b9c4d3ae7279cca12d168f3f825e9`，1,179,910 B）
- 工作目录：`<repo>`；本报告与全部脚本、日志见 `.build/verify/`。
- 第一轮（修复前）日志保留为 `boot-*.log` 作对比；**最终产物复验日志为 `boot2-*.log`**，其结论覆盖本报告全部 PASS/FAIL。

**复验摘要（最终产物）：** man page `.UR` 空块缺陷已修复（`.UR`/`.UE` 之间补链接文本），真实 mandoc 1.14.6 对安装前后的 man page `-T lint` 与 `-W all` 均 **0 输出、rc=0**；postinst 已移除字面 `#DEBHELPER#` 占位符（现 541 B）；两个 static `.deb` 在 Ubuntu 24.04 arm64/amd64 原生 dpkg 1.22.6 上重新完整安装/校验/移除/清除全部 PASS，无新增缺陷。旧 sha256 产物全部作废。

复验关键退出码（最终产物，全部取自 `boot2-*.log`）：
- Alpine aarch64 lint 访客：`MANDOC_LINT_RC=0`、`MANDOC_LINT_ALL_RC=0`、`MANDOC_INSTALLED_RC=0`、`DEB_I/C/E/X_RC=0`、`INSTALL_RC=0`、`STATUS_RC=0`、`VERIFY_RC=0`、`REMOVE_RC=0`、`PURGE_RC=0`
- Ubuntu arm64：`INSTALL_RC=0`、`STATUS_RC=0`、`VERIFY_RC=0`、`MD5SUMS_RC=0`、`REMOVE_RC=0`、`PURGE_RC=0`、`AUDIT_AFTER_RC=0`；`CROSS_INSTALL_RC=1`（跨架构，预期拒绝）
- Ubuntu amd64：`INSTALL_RC=0`、`STATUS_RC=0`、`VERIFY_RC=0`、`MD5SUMS_RC=0`、`REMOVE_RC=0`、`PURGE_RC=0`、`AUDIT_AFTER_RC=0`；`CROSS_INSTALL_RC=1`（跨架构，预期拒绝）

**结论：最终产物核心交付物 PASS，第一轮发现的唯一缺陷已修复并复验通过，无已知未决缺陷。两个架构的 static `.deb` 均能被真实 dpkg（1.22.6，Ubuntu 24.04 arm64/amd64 原生架构）完整安装、md5 校验、移除与清除；`dpkg-deb` / `dpkg -i` / `dpkg --verify` / `dpkg -r` / `dpkg -P` 全部通过。x86_64 产物 smoke 与全套 42 个测试在真实 x86_64 Linux 内核上通过（产物两轮未变，结论继续有效）。**

---

## 1. PASS/FAIL 总表

| # | 验证项 | 环境 | 结果 | 证据 |
|---|--------|------|------|------|
| A1 | amd64 artifact `--version`（`linkrd 0.1.0`，rc=0） | x86_64 Alpine | **PASS** | §3.2 |
| A2 | amd64 artifact `--help`（rc=0，输出正常） | x86_64 Alpine | **PASS** | §3.2 |
| A3 | amd64 artifact `--list-ports`（rc=0） | x86_64 Alpine | **PASS** | §3.2 |
| A4 | amd64 artifact `status`（rc=0，输出 `stopped`） | x86_64 Alpine | **PASS** | §3.2 |
| A5 | x86_64-musl 全套 42 个集成测试（6/4/2/11/5/14） | x86_64 Alpine | **PASS** | §3.3 |
| A6 | aarch64 产物 smoke + 42 测试（Lead 先前已验证，本报告复用其日志） | aarch64 Alpine | **PASS**（复用） | `.build/qemu/boot-hvf.log` |
| B1 | 真实 `dpkg-deb -I/-c/-e/-f` 接受新 ar 成员名格式 | Ubuntu amd64/arm64 + Alpine aarch64 | **PASS** | §4.1、§4.2、§4.3 |
| B2 | control 字段被真实 dpkg 接受（`dpkg -s` 完整回读） | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.3 |
| B3 | `dpkg -i` arm64 `.deb`（原生架构 arm64） | Ubuntu arm64 | **PASS** | §4.2 |
| B4 | `dpkg -i` amd64 `.deb`（原生架构 amd64） | Ubuntu amd64 | **PASS** | §4.3 |
| B5 | `dpkg -L` / `dpkg -l` / `dpkg-query -W` / `dpkg --audit` | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.3 |
| B6 | `dpkg --verify` + `md5sum -c /var/lib/dpkg/info/linkrd.md5sums` 全部 OK | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.3 |
| B7 | 安装后 `/usr/bin/linkrd --version` / `--help` 可运行 | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.3 |
| B8 | udev 规则落到 `/usr/lib/udev/rules.d/70-linkrd.rules`，内容与 contrib 一致 | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.4 |
| B9 | systemd 用户单元落到 `/usr/lib/systemd/user/linkrd.service`，内容与 contrib 一致，且未被 enable | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.4 |
| B10 | man page 安装为 `/usr/share/man/man1/linkrd.1.gz`，gzip 完好、内容正确 | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.4 |
| B11 | `mandoc -T lint` / `mandoc -T lint -W all`（安装前载荷 + 安装后 `/usr/share/man/man1/linkrd.1.gz`） | Alpine aarch64（mandoc 1.14.6） | **PASS**（修复后 **0 输出、rc=0**；-W all 也 rc=0） | §4.1、§5.1 |
| B12 | `mandoc` 渲染 man page | Alpine aarch64 | **PASS**（rc=0，标题/正文正常） | §4.1 |
| B13 | `postinst` 在无 `udevadm` 环境安全跳过（安装 rc=0） | Ubuntu arm64/amd64（均无 udevadm） | **PASS** | §4.2、§4.5 |
| B14 | `dpkg -r linkrd` 干净移除 | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.3 |
| B15 | `dpkg -P linkrd` 干净清除，无归档数据库/文件残留 | Ubuntu arm64/amd64 | **PASS** | §4.2、§4.3 |
| B16 | 跨架构防护：arm64 dpkg 拒绝 amd64 `.deb` | Ubuntu arm64（及 amd64 上拒绝 arm64） | **PASS**（按预期 rc=1） | §4.4 |
| B17 | Alpine（musl）dpkg 1.22.15 也能安装 arm64 `.deb`（需先 `dpkg --add-architecture arm64`） | Alpine aarch64 | **PASS** | §4.1 |
| B18 | `.deb` 内 `/usr/bin/linkrd` 与 `.build/artifacts/` 产物逐字节一致（sha256） | 宿主静态比对 | **PASS** | §4.4、`host-checks-final.log` |
| B19 | `.deb` 载荷中 udev 规则 / systemd 单元 / man page 与仓库源文件逐字节一致 | 宿主静态比对 | **PASS** | §4.4、`host-checks-final.log` |
| B20 | 最终 postinst 无 `#DEBHELPER#` 占位符（541 B，13 行） | 访客 `dpkg-deb -e` + 宿主 control 检查 | **PASS**（三个访客日志中出现次数均为 0） | §4.4、§5.2 |

> 注：B1 的“新 ar 成员名”指 deb-packager 修复后的格式；上表全部 PASS/FAIL 结论均基于**最终产物**（sha256 见开头），由 `boot2-*.log` 复验日志支撑；第一轮 `boot-*.log` 仅用于对比修复前状态。

---

## 2. 环境信息

| 项目 | 值 |
|------|-----|
| 宿主 CPU/OS | Apple Silicon arm64，macOS 27.2（26B118） |
| QEMU | 11.1.1（`qemu-system-aarch64` / `qemu-system-x86_64`） |
| 加速方式 | aarch64 访客：`-accel hvf`（硬件加速）；x86_64 访客：`-accel tcg,thread=multi`（无 HVF，纯 TCG，实测足够快） |
| aarch64 内核 | `6.12.114-0-virt #1-Alpine SMP PREEMPT_DYNAMIC 2026-10-21 aarch64`（来自 `.build/qemu/vmlinuz-virt`） |
| x86_64 内核 | `6.12.114-0-virt #1-Alpine SMP PREEMPT_DYNAMIC 2026-10-21 x86_64`（Alpine 3.22 netboot `vmlinuz-virt`） |
| 阶段 A 访客 | Alpine 3.22 x86_64 minirootfs（musl），busybox + bash 5.2.37（自 Alpine apk 提取） |
| 阶段 B 访客 1 | Alpine 3.22 aarch64（musl），dpkg **1.22.15**（`musl-linux-arm64`）、mandoc **1.14.6**（Alpine main apk） |
| 阶段 B 访客 2 | Ubuntu base **24.04.3 LTS**（glibc，Debian 系），dpkg **1.22.6**（arm64 / amd64 原生架构） |
| 访客启动方式 | initramfs + `rdinit=/init`（无磁盘），串口控制台；`ifconfig lo 127.0.0.1 up`（阶段 A transport 测试需要） |
| 测试二进制 | x86_64-musl 集成测试：`cargo zigbuild --locked --release --target x86_64-unknown-linux-musl --tests`（rc=0） |

启动命令（aarch64 / HVF）：

```
qemu-system-aarch64 -M virt -cpu max -m 2048 -nographic -no-reboot -accel hvf \
  -kernel .build/qemu/vmlinuz-virt -initrd .build/verify/initramfs-aarch64-*.cpio.gz \
  -append "console=ttyAMA0 rdinit=/init panic=-1 loglevel=4"
```

启动命令（x86_64 / TCG）：

```
qemu-system-x86_64 -M q35 -cpu max -m 2048 -nographic -no-reboot -accel tcg,thread=multi \
  -kernel .build/verify/dl/x86_64/vmlinuz-virt \
  -initrd .build/verify/initramfs-x86_64.cpio.gz \
  -append "console=ttyS0 rdinit=/init panic=-1 loglevel=4"
```

---

## 3. 阶段 A：x86_64 实机验证（amd64 产物 + 42 测试）

日志：`.build/verify/boot-x86_64.log`（脚本 `.build/verify/build-x86_64-initramfs.py`）。
访客内 `uname -a`：`Linux (none) 6.12.114-0-virt #1-Alpine SMP PREEMPT_DYNAMIC 2026-10-21 13:44:51 x86_64 Linux`。
注：`.build/artifacts/` 两个产物在第二轮复验前后 sha256 未变（见报告开头），故本节阶段 A 结论对最终产物继续有效。

### 3.1 访客准备

- `ifconfig lo 127.0.0.1 netmask 255.0.0.0 up` 成功（transport 测试依赖 127.0.0.1 TCP）。
- `/bin/bash` 存在且可执行：`BASH_OK 5.2.37(1)-release`（install 测试的 PATH 用例需要）。
- 测试用 cargo target 绝对路径 `<repo>/target/x86_64-unknown-linux-musl/release/` 已“烘焙”进访客根，`deps/` 下 6 个集成测试可执行文件齐备。

### 3.2 amd64 产物 smoke（用 `.build/artifacts/linkrd-linux-amd64` 原始文件）

```
linkrd 0.1.0
SMOKE version exit=0
SMOKE help exit=0
<-- 帮助输出正常，列出 start/stop/pid/status/... 子命令 -->
SMOKE list-ports exit=0
/dev/ttyS2 Unknown
/dev/ttyS0 Unknown
/dev/ttyS3 Unknown
/dev/ttyS1 Unknown
SMOKE status exit=0      # 输出 "stopped"
```

四个 smoke 项退出码均为 0；`--list-ports` 在无真实 Linkr 设备时按预期列出 tty 设备并返回 0。

### 3.3 全套 42 个测试（x86_64-musl，逐二进制 `--test-threads=1`）

| 测试二进制 | 结果 | 数量 |
|---|---|---|
| `framing-e569fb6ebf82b695` | `test result: ok. 6 passed; 0 failed`，rc=0 | 6 |
| `protocol-75ee4c261058e523` | `test result: ok. 4 passed; 0 failed`，rc=0 | 4 |
| `logging-90441d50750110a1` | `test result: ok. 2 passed; 0 failed`，rc=0 | 2 |
| `session-a568745d097188cf` | `test result: ok. 11 passed; 0 failed`，rc=0 | 11 |
| `transport-cfef63a91cff4831` | `test result: ok. 5 passed; 0 failed`，rc=0 | 5 |
| `install-b0f997cc0c59c2ff` | `test result: ok. 14 passed; 0 failed`，rc=0 | 14 |
| **合计** | **42 passed; 0 failed**，6 个进程全部 rc=0 | **42** |

原始片段：

```
--- RUN framing-e569fb6ebf82b695
...
test result: ok. 6 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 0.02s
--- EXIT framing-e569fb6ebf82b695 rc=0
...
--- RUN install-b0f997cc0c59c2ff
running 14 tests
...
test result: ok. 14 passed; 0 failed; 0 ignored; 0 measured; 0 filtered out; finished in 6.07s
--- EXIT install-b0f997cc0c59c2ff rc=0
=== ALL DONE ===
```

对照：Lead 先前的 aarch64 访客日志 `.build/qemu/boot-hvf.log` 同样是 6/4/2/11/5/14 全通过（aarch64 已覆盖，故本次只补 x86_64；两个架构合计 84 个测试用例全部通过）。

---

## 4. 阶段 B：`.deb` 真实 dpkg 安装验证

### 4.1 Alpine aarch64 访客（musl dpkg 1.22.15 + mandoc 1.14.6）

日志：`.build/verify/boot2-aarch64-lint.log`（最终产物复验；脚本 `.build/verify/build-aarch64-lint.py`；第一轮日志 `boot-aarch64-lint.log` 保留作对比）。
用途：用**真实 `dpkg-deb`** 解析最终 `.deb`、用**真实 `mandoc`** 检查 man page、以及查看 dpkg 在 musl 环境下的安装/移除行为。

- `dpkg-deb -I` 接受最终 `.deb`：rc=0，control 区 1501 B = control 1309 B + md5sums 600 B + postinst 541 B。
- `dpkg-deb -c` rc=0（9 个文件 + 13 个目录，root/root，`/usr/bin/linkrd` 755，其余 644）。
- `dpkg-deb -e` rc=0，control/md5sums/postinst 原文已落日志；`dpkg-deb -x` rc=0（载荷 9 文件全部解出）。
- 载荷内 `/tmp/root/usr/bin/linkrd --version` → `linkrd 0.1.0`，rc=0。
- **`mandoc -T lint`（修复后验收点）**：
  ```
  === mandoc -T lint on packaged man page ===
  MANDOC_LINT_RC=0            # 无任何输出
  === mandoc -T lint -W all (strict) ===
  MANDOC_LINT_ALL_RC=0        # 无任何输出
  ```
  `mandoc -T lint` 与 `-W all` 均为 **0 输出、rc=0**，原先的 `WARNING: empty block: UR` 已消失（详见 §5.1）。
- `mandoc <man page>` 渲染 rc=0，`LINKRD(1) User Commands LINKRD(1)`、NAME/SYNOPSIS/DESCRIPTION 均正常。
- udev 规则与 systemd 用户单元从载荷中直接 `cat`，内容正确（与 contrib 逐字节比对见 §4.4）。
- postinst 原文 13 行、541 B，**不含 `#DEBHELPER#` 占位符**（整个访客日志中该字符串出现 0 次）。
- 安装：Alpine 的 dpkg 系统架构是 `musl-linux-arm64`（非 `arm64`），直接 `dpkg -i` 会被拒绝（这是 Alpine dpkg 的构建属性，不是包缺陷）；执行 `dpkg --add-architecture arm64` 后：
  ```
  ADD_ARCH_RC=0
  arm64
  Selecting previously unselected package linkrd:arm64.
  Unpacking linkrd:arm64 (0.1.0-1) ...
  Setting up linkrd:arm64 (0.1.0-1) ...
  INSTALL_RC=0
  ```
- `dpkg -s linkrd` → `Status: install ok installed`，rc=0；`dpkg -l` → `ii linkrd:arm64 0.1.0-1`；`dpkg -L` 列出全部 9 文件 + 目录；`dpkg --verify` 无输出 rc=0（md5sums 通过）。
- `/usr/bin/linkrd --version` → `linkrd 0.1.0`，rc=0；安装后的 `/usr/share/man/man1/linkrd.1.gz` 再跑 `mandoc -T lint` → **rc=0（无输出）**。
- `dpkg -r` rc=0；`dpkg -P` rc=0；清除后 `/usr/bin/linkrd`、man page 均不存在，`/var/lib/dpkg/info/` 无 linkrd 残留。Alpine 上有 3 条 `dpkg: warning: ... directory '/usr/share' not empty so not removed` —— 这是 dpkg 在非 Debian 基础系统上移除“它认为属于包”的公共目录时的正常提示，不影响包本身结论。

### 4.2 Ubuntu 24.04 arm64 访客（真实 Debian 系 dpkg 1.22.6，原生 arm64）

日志：`.build/verify/boot2-deb-arm64.log`（最终产物复验；脚本 `.build/verify/build-deb-guest.py arm64`；第一轮日志 `boot-deb-arm64.log` 保留作对比）。
说明：Ubuntu base 镜像自带 `/etc/dpkg/dpkg.cfg.d/excludes`（`path-exclude=/usr/share/man/*`、`/usr/share/doc/*`，仅保留 copyright/changelog）。因此初始化脚本先做一次 **stock 配置安装**（演示该环境策略的影响，非包缺陷），再删除该 excludes 做**完整安装**主验证。
最终产物：`linkrd_0.1.0-1_arm64.deb` 1,179,910 B（sha256 见开头）；`dpkg-deb -I` 显示 control 区 1501 B，其中 postinst 541 B（13 行，无 `#DEBHELPER#`）。

- 环境：`dpkg 1.22.6 (arm64)`；`udevadm` NOT FOUND；`systemctl` NOT FOUND；`mandoc` NOT FOUND（Ubuntu base 最小化，`man` 也存在但提示 unminimize）。
- **Stock 配置安装**（md5 等仍全部由 dpkg 校验）：
  ```
  INSTALL_STOCK_RC=0
  VERIFY_STOCK_RC=0
  missing     /usr/share/doc/linkrd/README.debian
  missing     /usr/share/doc/linkrd/README.md
  missing     /usr/share/doc/linkrd/README.zh-CN.md
  missing     /usr/share/man/man1/linkrd.1.gz
  ```
  即 dpkg 按镜像自带的 path-exclude 主动过滤 man/docs（`copyright`、`changelog.Debian.gz` 按 path-include 保留），属于 **Ubuntu base 镜像自身策略**，非 `.deb` 缺陷。
- **完整安装（移除 excludes 后）**：
  ```
  Selecting previously unselected package linkrd.
  Preparing to unpack .../deb/linkrd_0.1.0-1_arm64.deb ...
  Unpacking linkrd (0.1.0-1) ...
  Setting up linkrd (0.1.0-1) ...
  INSTALL_RC=0
  ```
- `dpkg -s linkrd` rc=0：`Status: install ok installed`、`Architecture: arm64`、`Version: 0.1.0-1`、`Installed-Size: 2639`、`Recommends: systemd`（无 Depends，符合 musl 静态产物预期）。
- `dpkg -L linkrd` rc=0，包含全部 9 个文件：`/usr/bin/linkrd`、`/usr/lib/udev/rules.d/70-linkrd.rules`、`/usr/lib/systemd/user/linkrd.service`、`/usr/share/doc/linkrd/{README.debian,README.md,README.zh-CN.md,changelog.Debian.gz,copyright}`、`/usr/share/man/man1/linkrd.1.gz`。
- `dpkg --verify linkrd` 无输出 rc=0（md5sums 全部匹配）；`dpkg-query -W` → `linkrd 0.1.0-1 arm64 install ok installed`；`dpkg --audit` rc=0（无异常）。
- 磁盘实测：`/usr/bin/linkrd` 2,624,648 B，`root:root`，`-rwxr-xr-x`；`/usr/lib/systemd/user/linkrd.service` 676 B；`/usr/lib/udev/rules.d/70-linkrd.rules` 456 B；man page 3,758 B（修复后 +16 B）；doc 目录 5 个文件。
- `md5sum -c /var/lib/dpkg/info/linkrd.md5sums`：9 个文件全部 `OK`，`MD5SUMS_RC=0`。
- 安装后的 `/usr/bin/linkrd --version` → `linkrd 0.1.0` rc=0；`--help` rc=0。
- man page：`gzip -t` rc=0；`gzip -dc | head` 内容正常（`.TH LINKRD 1 "2026-10-27" "linkrd 0.1.0" "User Commands"`）。
- postinst：541 B / 13 行，已无 `#DEBHELPER#` 占位符（访客内 `dpkg-deb -e` + `cat` 原文见日志）。
- udev 规则 / systemd 单元安装位置与内容正确（systemd 单元未被 enable：`dpkg -L` 只有 `/usr/lib/systemd/user/linkrd.service`，postinst 中也无 enable 逻辑，与 `debian/rules` 的 `dh_installsystemduser --no-enable` 一致）。
- **移除/清除**：
  ```
  dpkg -r linkrd   → REMOVE_RC=0；之后 dpkg -s rc=1（not installed），/usr/bin/linkrd 等已删除
  dpkg -i（重装）→ REINSTALL_RC=0
  dpkg -P linkrd   → PURGE_RC=0
  清除后：/usr/bin/linkrd、70-linkrd.rules、linkrd.service、linkrd.1.gz 均不存在；
          /var/lib/dpkg/info/ 无 linkrd 残留；dpkg --audit rc=0
  ```
- `man -l` / `man --warnings -l`：命令 rc=0，但 Ubuntu base 已被裁剪（输出 “This system has been minimized...”），**未真正渲染**；man page 的实际渲染由 Alpine 的 mandoc 验证（§4.1）。
- 跨架构防护：在 arm64 上安装 amd64 `.deb` 被 dpkg 正确拒绝：
  ```
  dpkg: error processing archive /opt/deb/linkrd_0.1.0-1_amd64.deb (--install):
   package architecture (amd64) does not match system (arm64)
  CROSS_INSTALL_RC=1
  ```

### 4.3 Ubuntu 24.04 amd64 访客（真实 dpkg 1.22.6，原生 amd64）

日志：`.build/verify/boot2-deb-amd64.log`（最终产物复验；`build-deb-guest.py amd64`，qemu TCG；第一轮日志 `boot-deb-amd64.log` 保留作对比）。
最终产物：`linkrd_0.1.0-1_amd64.deb` 1,229,026 B（sha256 见开头）。结果与 4.2 一致且全部 PASS：

- 环境：`dpkg 1.22.6 (amd64)`；`udevadm` NOT FOUND；`systemctl` NOT FOUND。
- Stock excludes 阶段：`INSTALL_STOCK_RC=0`，`dpkg --verify` 仅报上述 4 个被镜像策略过滤的 man/docs 文件（`VERIFY_STOCK_RC=0`），随后 `dpkg -P` rc=0。
- 完整安装阶段：`INSTALL_RC=0`；`dpkg -s` rc=0；`dpkg -L` rc=0；`dpkg --verify` 无输出 rc=0；`dpkg-query -W` → `linkrd 0.1.0-1 amd64 install ok installed`；`dpkg --audit` rc=0。
- 磁盘：`/usr/bin/linkrd` 2,783,544 B，`root:root` `-rwxr-xr-x`；man page 3,758 B；service 676 B；rules 456 B。
- `md5sum -c /var/lib/dpkg/info/linkrd.md5sums` → 全部 OK，`MD5SUMS_RC=0`。
- `/usr/bin/linkrd --version` rc=0；`--help` rc=0。
- udev 规则、systemd 用户单元内容正确；man page `gzip -t` rc=0。
- `dpkg -r` rc=0；重装 rc=0；`dpkg -P` rc=0；清除后无残留文件、`/var/lib/dpkg/info` 无 linkrd；`dpkg --audit` rc=0。

### 4.4 静态一致性与可追溯性（补充校验）

`host-checks-final.log`（针对最终产物；`gzip -dc` 与 `diff -u` 均为宿主只读比对；包内文件格式/安装行为以访客 dpkg 输出为准）：

- `.deb` 内 `/usr/bin/linkrd` 的 sha256 与 `.build/artifacts/` 对应产物**完全一致**：
  - arm64：`72ac58f1...0d5b4c77`
  - amd64：`d4bcb61a...2f763b83`
  两者均为 `ELF ... statically linked, stripped`。
- `.deb` 载荷中 `usr/lib/udev/rules.d/70-linkrd.rules` 与 `contrib/udev/70-linkrd.rules` **逐字节相同**（IDENTICAL）。
- `.deb` 载荷中 `usr/lib/systemd/user/linkrd.service` 与 `contrib/systemd/linkrd.service` **逐字节相同**（IDENTICAL）。
- `.deb` 载荷中 `usr/share/man/man1/linkrd.1.gz` 解压后与 `man/linkrd.1`（修复后版本）**逐字节相同**（IDENTICAL）；载荷内 man page 的 `.UR`/`.UE` 之间已有链接文本 `the Linkr project page on GitHub`。
- amd64 与 arm64 两个 `.deb` 的 `usr/share` 载荷树**完全相同**（IDENTICAL，仅二进制不同）。
- postinst 原文（最终 `.deb` control 区，访客内 `dpkg-deb -e` 取得；541 B、13 行，**无 `#DEBHELPER#`**）：
  ```sh
  #!/bin/sh
  set -e

  # The package installs /usr/lib/udev/rules.d/70-linkrd.rules.  ...
  # udevadm is absent in containers, chroots and non-udev systems; failures are ignored.
  if [ "$1" = "configure" ] && command -v udevadm >/dev/null 2>&1; then
      udevadm control --reload-rules 2>/dev/null || true
      udevadm trigger --subsystem-match=tty --action=change 2>/dev/null || true
  fi

  exit 0
  ```

### 4.5 `udevadm` 缺失时的安全性（任务重点）

三个阶段 B 访客中 `command -v udevadm` 均为 **NOT FOUND**（Ubuntu base 与 Alpine 都没有 udev），而：

- `dpkg -i` **全部 rc=0**（Ubuntu arm64、Ubuntu amd64、Alpine aarch64 三处）；
- `dpkg -s` 显示 `Status: install ok installed`；
- `dpkg --audit` rc=0，无半安装状态。

原因即 postinst 自身有 `command -v udevadm` 守卫 + `2>/dev/null || true`，dpkg 侧未出现任何 postinst 报错输出。最终产物复验（`boot2-*.log`）结论相同：三个访客 `dpkg -i` 均 rc=0。**结论：postinst 在无 udevadm 的最小化/容器环境中安全跳过，不会导致安装失败。**

---

## 5. 发现的问题与修复状态

### 5.1 [已修复并复验通过] man page 的 `.UR` 空块告警

- 第一轮（旧产物）发现：真实 mandoc 1.14.6 报 `linkrd.1.gz:386:2: WARNING: empty block: UR`，`mandoc -T lint` 退出码 2；根因是 `man/linkrd.1` 的 `.UR`/`.UE` 之间没有链接文本。
- 已修复：`.UR` 与 `.UE` 之间补了链接文本。最终源码 `man/linkrd.1` 对应段落为：
  ```roff
  The project documentation is available at
  .UR https://github.com/radxa\-linkr/linkrd
  the Linkr project page on GitHub
  .UE .
  ```
- 最终产物复验证据（Alpine aarch64 访客，真实 mandoc 1.14.6，日志 `boot2-aarch64-lint.log`）：
  ```
  === mandoc -T lint on packaged man page ===
  MANDOC_LINT_RC=0            # 无任何输出
  === mandoc -T lint -W all (strict) ===
  MANDOC_LINT_ALL_RC=0        # 无任何输出
  ... dpkg -i 之后，对安装的 /usr/share/man/man1/linkrd.1.gz 再验：
  MANDOC_INSTALLED_RC=0       # 无任何输出
  ```
- 结论：**已修复，`mandoc -T lint` 与 `-W all` 均 0 输出、rc=0，安装前后一致，无回归**。`.deb` 内的 gz 与修复后源码逐字节一致（§4.4）。

### 5.2 非缺陷、但需要记录的环境现象（避免误判）

1. **Ubuntu base 镜像的 dpkg path-exclude**：首次 `dpkg -i` 后 `dpkg --verify` 会报 4 个 “missing”（3 个 README + man page），**不是 `.deb` 缺文件**。Ubuntu base 自带 `/etc/dpkg/dpkg.cfg.d/excludes`（`path-exclude=/usr/share/man/*`、`path-exclude=/usr/share/doc/*`，只 `path-include` copyright 与 changelog.*）。删除该文件后完整安装，`dpkg --verify`/`md5sum -c` 全部通过。真实 Debian 桌面系统默认无此过滤。
2. **Alpine dpkg 的系统架构名是 `musl-linux-arm64`**：在 Alpine 上直接 `dpkg -i` 一个 `Architecture: arm64` 的包会得到 `package architecture (arm64) does not match system (musl-linux-arm64)`。执行 `dpkg --add-architecture arm64` 后可正常安装（实测 rc=0，md5sums/verify 通过）。这是 Alpine dpkg 构建差异，不是 `.deb` 缺陷。
3. **Alpine 上 `dpkg -r/-P` 的 3 条 warning**（`directory '/usr/share' not empty so not removed`）：dpkg 在非 Debian 基础系统上清理包内目录时的常规提示，退出码 0，无残留。
4. **postinst 中字面 `#DEBHELPER#` 占位符（已修复）**：第一轮产物的 postinst 残留该注释；最终产物已移除，postinst 现为 541 B / 13 行，三个复验访客日志中 `#DEBHELPER#` 出现次数均为 0，且 `dpkg -i` 仍 rc=0。
5. **无 `prerm/postrm`**：移除时不会自动 `udevadm control --reload-rules`；对 udev 规则包通常无实际影响（规则文件被删除后 udev 会重新加载/下次 trigger 生效），但如有更高要求可补 prerm。**本次未验证 udev 守护进程行为**（访客无 udev/systemd 运行）。

---

## 6. 宿主机不具备、因此未执行的项目（明确未覆盖）

| 未覆盖项 | 原因 / 替代 |
|---|---|
| `lintian` / `dpkg-buildpackage` 完整打包流水线检查 | 宿主 macOS 无 dpkg/lintian；`.deb` 由 `packaging/deb/build-deb.sh` 生成，其结构由 `packaging/deb/validate-deb.py` 静态校验 + 本次真实 dpkg 验证替代 |
| `man -l` 实际渲染 | Ubuntu base 被裁剪（提示 unminimize，无 man-db 内容）；由 Alpine mandoc 渲染/ lint 替代 |
| 真实 udev 守护进程对 `70-linkrd.rules` 的加载与 uaccess ACL 效果 | 访客为 initramfs、无 udev/systemd/logind；仅验证了规则文件安装位置与内容 |
| systemd 用户单元的实际启动/`systemctl --user enable` | 同上（访客无 systemd）；已验证单元安装位置、内容、未被 enable |
| USB CDC-ACM 真实设备 `/dev/ttyACM*` 读写 | 无硬件；仅在阶段 A 验证 `--list-ports` 与 transport 测试（127.0.0.1 TCP 模拟） |
| `dist/gnu/` 的 glibc 动态 `.deb` 安装 | 非主产物；本次聚焦 musl 静态主产物。其静态结构可由同一 dpkg-deb 方法验证，未纳入本轮 |
| arm64 产物的完整测试套件 | Lead 已在 aarch64 HVF 访客完成 42 项全通过（`.build/qemu/boot-hvf.log`，本报告复用，未重复执行） |

---

## 7. 复现材料

全部位于 `.build/verify/`（只读输入未被修改；`.build/qemu/`、`.build/artifacts/`、`dist/` 等未写入）：

| 文件 | 说明 |
|---|---|
| `REPORT.md` | 本报告 |
| `build-x86_64-initramfs.py` | 阶段 A：Alpine x86_64 + amd64 产物 smoke + 42 测试的 initramfs 构建脚本 |
| `build-aarch64-lint.py` | 阶段 B：Alpine aarch64 + 真实 dpkg-deb/mandoc 的 initramfs 构建脚本 |
| `build-deb-guest.py` | 阶段 B：Ubuntu base 24.04 arm64/amd64 + 真实 dpkg 完整安装/移除/清除脚本 |
| `boot-x86_64.log` | 阶段 A 完整串口日志（smoke + 42 测试；产物两轮未变） |
| `boot-aarch64-lint.log` / `boot2-aarch64-lint.log` | Alpine aarch64 dpkg-deb/mandoc/安装-移除日志（第一轮 / **最终产物复验**） |
| `boot-deb-arm64.log` / `boot2-deb-arm64.log` | Ubuntu 24.04 arm64 真实 dpkg 全流程日志（第一轮 / **最终产物复验**；含 stock-excludes 对照、跨架构拒绝、man -l） |
| `boot-deb-amd64.log` / `boot2-deb-amd64.log` | Ubuntu 24.04 amd64 真实 dpkg 全流程日志（第一轮 / **最终产物复验**） |
| `host-checks.log` / `host-checks-final.log` | 宿主侧 sha256/逐字节一致性补充校验输出（第一轮 / **最终产物**） |
| `initramfs-*.cpio.gz` | 各访客 initramfs（可复现启动） |
| `dl/` | 下载的 Alpine x86_64 内核/apk、Alpine aarch64 依赖 apk、Ubuntu base rootfs |

重新运行要点：

```bash
cd <repo>
# 阶段 A
python3 .build/verify/build-x86_64-initramfs.py
qemu-system-x86_64 -M q35 -cpu max -m 2048 -nographic -no-reboot -accel tcg,thread=multi \
  -kernel .build/verify/dl/x86_64/vmlinuz-virt -initrd .build/verify/initramfs-x86_64.cpio.gz \
  -append "console=ttyS0 rdinit=/init panic=-1 loglevel=4"

# 阶段 B（arm64 权威验证；amd64 同理）
python3 .build/verify/build-deb-guest.py arm64
qemu-system-aarch64 -M virt -cpu max -m 2048 -nographic -no-reboot -accel hvf \
  -kernel .build/qemu/vmlinuz-virt -initrd .build/verify/initramfs-deb-arm64.cpio.gz \
  -append "console=ttyAMA0 rdinit=/init panic=-1 loglevel=4"
```

（`.deb` 需以最新 `dist/*.deb` 重新构建 initramfs 后运行；本次验证的 `.deb` sha256 见报告开头。）