# Codex Guard 🛡️

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Python: 3.8+](https://img.shields.io/badge/Python-3.8%2B-green.svg)](https://www.python.org/)
[![Platform: Linux](https://img.shields.io/badge/Platform-Linux-orange.svg)](https://www.linux.org/)

**Codex Guard** 是专为 **OpenAI Codex CLI / Codex Plus 订阅用户** 设计的自动化额度守护、防扣费与自动继续执行工具。

> [English README](#english) | [中文说明](#中文说明)

---

<a name="中文说明"></a>
## 💡 背景与痛点

在使用 OpenAI Codex Plus 订阅时：
1. **5小时额度轮转限制**：Plus 订阅拥有 5 小时滚动刷新额度（通常每 5 小时重置一次）。
2. **意外消耗付费 Credits**：如果账户中同时存有 API 付费余额（Credits），一旦 5h 订阅额度耗尽，OpenAI 后端将**自动转为扣除付费 Credits**，导致不必要的额外开销。
3. **长时间任务中断与等待**：任务被额度限制中断后，如果人不守在电脑前，即使 5 小时后额度刷新，Codex 也会一直停留在输入框发呆，无法自主继续推进。

**Codex Guard 彻底解决此问题**：
实时精准监听 5h 额度使用率；当剩余额度低于设定保护线（默认 **3%**）时，自动挂起暂停 Codex 任务；一旦 5h 额度到达刷新点并恢复，**自动拉起/唤醒 Codex 恢复原状，并自动发送「继续」指令，无缝继续工作，全程无需人工守候！**

---

## ✨ 核心特性

- 🎯 **精准度检测**：直接通过本地 Codex App-Server IPC 套接字（Unix WebSocket JSON-RPC）获取官方毫秒级额度数据（5h 窗口已用百分比、剩余百分比、精确重置时间戳 `resetsAt`）。
- 🛑 **零扣费协议级拦截**：额度 $\le 3\%$ 时，立即向 App-Server 发送 `turn/interrupt` 协议指令彻底掐断云端模型推理与 Token 偷跑，并对前端客户端发送 `SIGSTOP`，递归终止子工具任务，**杜绝任何 API Credits 消耗**。
- 🖱️ **终端防乱码保护**：在挂起目标进程前自动注销伪终端鼠标与焦点追踪模式（1000/1002/1003/1004/1006），彻底解决进程暂停后鼠标划过终端产生乱码字符（`^[[<35;...M`）的业界通病。
- 🔄 **原状无缝恢复与多层自愈拉起**：
  - 额度刷新后自动唤醒原进程；若原客户端已关闭或终止，自动通过 5 级快照降级链解析并自动拉起交互终端窗口（`gnome-terminal` / `screen`），无缝衔接继续执行！
  - 自动快照记录会话 ID、标题、路径，自动校验并清理无进程占用的死锁（Writer Locks），**会话列表与任务进度完整无损**。
- ⚡ **智能自主续跑（Idle Auto-Continue）与唤醒注入**：
  - **额度恢复唤醒续跑**：当 5h 额度恢复唤醒后，守护程序自动向活跃会话注入「继续」，无缝接力执行。
  - **回合完成自主接力**：当 Codex 跑完一个回合进入空闲（`idle`）状态，守护程序自动等待 5 秒防抖缓冲后自动发送「继续」，让长线任务（如全流程解题、代码重构调优）永不停歇自主向前推进！
- 🔔 **桌面通知推送**：通过系统 `notify-send` 实时推送暂停预警（含预计刷新倒计时）与恢复提醒。
- ⚙️ **Systemd 用户服务常驻**：一行命令注册为用户级后台守护服务，开机/登录自动常驻，低内存占用，零负担。
- 💻 **优雅的 top 级监控**：提供 `codex-top` 实时全屏交互看板（33FPS 极致丝滑拖拽缩放，0ms 乐观响应）。

---

## 🚀 快速安装

### 一键安装
```bash
git clone https://github.com/Au1Bhi/codex-guard.git
cd codex-guard
bash scripts/install.sh
```

一键脚本将：
1. 安装可执行文件至 `~/.local/bin/codex-guard` 及快捷软链接 `~/.local/bin/codex-quota`。
2. 注册并立即启动 Systemd 用户服务 `codex-quota-guard.service`。

---

## 📖 使用指南

### 0. 实时监控看板（类似 top / htop 动态大屏）
随时随地在终端输入 `codex-top`，享受媲美 `htop` 的全屏动态监控与键盘交互控制：
```bash
codex-top
# 或
codex-guard top
```
**动态大屏效果**：
```text
 codex-top  - 11:27:08 up 140h 23m, load: 1.12, 1.22, 1.18, Sentinel: ACTIVE (PID 267180)
5h Quota:   [████████░░░░░░░░░░░░░░░░░░░░] 29.0% used | 71.0% left (Reset: 15:24:43 in 3小时 57分钟)
Week Quota: [████████████░░░░░░░░░░░░░░░░] 43.0% used | 57.0% left | Credits: 1311.14 (SAFE - 0 used)
Guard Rule: Threshold <= 3.0% (SIGSTOP) | Auto-Continue: ON (5s debounce) | Plan: PLUS

 PID      STATE     CPU%   MEM     TURNS  STATUS       ACTIVE SESSION / TASK TITLE                  
 1230483  RUNNING   4.5%   54.5M   #41    inProgress   请完成我在firefox当前打开这个页面的所有题目

─── Live Turn Execution Stream (Turn #41 ─ inProgress, 35 actions) ────────────────────────
  • [CMD:completed] python3 tools/sim_pair_fresh.py normal-h1024 build/paired-normal.o (3478ms)
  • [CMD:completed] python3 tools/check.py (142ms)
  • [REASONING]     Analyzing memory bank conflicts and latency...
  • [CMD:running]   python3 tools/analyze.py normal-h127 auto

───────────────────────────────────────────────────────────────────────────────
 [q] 退出  [r] 刷新  [p] 暂停  [c] 唤醒  [k] 终止任务  [s] 启停服务  [Space] 注入「继续」  [+/-] 调频 (1.0s)
```
- **交互按键**：
  - `[q]` / `[ESC]` / `Ctrl+C`：安全退出 `codex-top` 控制台（不会影响后台 Codex 任务）。
  - `[Space]`（空格键）：立即向当前活跃任务注入「继续」指令，催促 Codex 推进！
  - `[p]`：一键内核级挂起（`SIGSTOP`）正在运行的 Codex 进程。
  - `[c]`：一键唤醒（`SIGCONT`）并自动继续执行。
  - `[k]`：彻底终止（Kill/Interrupt）当前运行中的任务与客户端进程（掐断模型推理，杜绝消耗额度，防止守护服务误拉起）。
  - `[s]`：**一键启停后台守护服务**（在控制台内实时开关 systemd Sentinel 守护服务，状态栏即时变色生效）。
  - `[+]` / `[-]`：实时调整刷新频率（0.2s ~ 10s）。
  - `[b]` / `-b` 参数：单次快照批处理输出（如 `codex-top -b`）。

---

### 1. 查看当前额度与会话状态
```bash
codex-quota
# 或
codex-guard status
```
**输出效果**：
```text
============================================================
              Codex Plus 5h 额度守护状态监控               
============================================================
订阅计划:        PLUS
5小时额度窗口:   已用 67.0% | 剩余 33.0% (窗口时长: 300 分钟)
重置时间:        2026-10-04 03:12:35 (距离刷新还有: 51分钟)
周额度窗口:      已用 35.0% | 剩余 65.0%
额外 Credits:    1311.14 (已充值但避免消耗)
守护触发阈值:    低于 3.0% 自动暂停
当前额度评估:    🟢 额度充足
------------------------------------------------------------
受守护进程 (1 个):
  • PID 1230483 [运行中 (RUNNING)] : codex

当前活跃会话 (已锁定载入):
  • 完成 Firefox 页面题目 (ID: 01a0f0c8...) [工作目录: /home/lizhonghu]
后台守护服务:    🟢 后台守护服务运行中 (Active)
============================================================
```

### 2. 查看会话列表（Session List）
无需进入全屏 TUI 即可快速浏览历史与活跃会话：
```bash
codex-guard sessions --limit 10
```

### 3. 一键关闭 / 彻底停止（内置停止工具）
如果你现在不需要服务，或者想立刻停止一切：
```bash
# 专属内置关闭命令（零参数，一键关停当前任务 + 关停后台服务）
codex-stop

# 一键彻底关闭并禁用开机自启
codex-stop --disable
# 或
codex-guard off

# 更多灵活控制：
codex-guard pause        # 临时挂起 (SIGSTOP) 所有运行中的 Codex 进程
codex-guard resume       # 唤醒 (SIGCONT) 所有被暂停的 Codex 进程并发送「继续」
codex-guard stop         # 彻底停止：掐断模型推理 + 终止客户端 + 停止后台守护服务
codex-guard stop --keep-service # 仅掐断当前任务，保留后台守护服务继续监控
```

### 4. 守护服务管理
```bash
# 查看后台守护服务运行状态与日志
codex-guard service status

# 停止 / 禁用服务（不需要时）
codex-guard service stop     # 停止服务
codex-guard service disable  # 停止并禁用开机/登录自启

# 启动 / 启用服务（需要时）
codex-guard service start    # 启动服务
codex-guard service enable   # 启用并启动服务
codex-guard service restart  # 重启服务

# 查看详细守护时间线日志
tail -f ~/.codex/quota_guard.log

# 自定义阈值与唤醒指令 (例如改为剩余 5% 时暂停，恢复时发送 "continue")
codex-guard service install --threshold 5.0 --interval 20 --continue-prompt "继续"

# 卸载守护服务
codex-guard service uninstall
```

---

## 🙏 致谢 (Acknowledgements)

特别鸣谢 **[LINUX DO](https://linux.do/)** 社区及其热心伙伴们在思路启发、技术探讨与使用反馈中的巨大支持！LINUX DO 开放、真诚、充满极客精神的技术交流氛围，促成了本项目的诞生与不断完善。

---

<a name="english"></a>
## 🌐 English Documentation

### Overview
Codex Guard is an automated rate-limit sentinel for users of the OpenAI Codex CLI on the Plus subscription plan.

When on a Plus plan with prepaid API credits, running out of your included 5-hour rolling quota will automatically cause OpenAI to deduct paid credits from your balance. Codex Guard prevents this by monitoring quota via the internal Codex App-Server IPC socket and freezing (`SIGSTOP`) active Codex processes when remaining quota drops below a safe threshold (default **3%**). Once the quota resets, Codex Guard seamlessly unfreezes (`SIGCONT`) your tasks, restores the full session context and task list, and automatically dispatches a `"继续"` (continue) command via the internal queue so your tasks resume execution without manual intervention.

Furthermore, Codex Guard includes an **Autonomous Idle Continuation Loop**: whenever an active Codex session finishes a turn and goes idle, the sentinel waits a brief 5-second buffer and automatically queues `"继续"` to keep long-running tasks driving forward autonomously!

### Key Architecture
```
[ Codex CLI Session ] <--- SIGSTOP / SIGCONT --- [ Codex Guard Sentinel ]
         ^                                                |
         | (auto-queues "继续" prompt)              WebSocket IPC (JSON-RPC)
         |                                                v
[ Codex Internal Queue ] <------------------- [ Codex App-Server Daemon ]
                                                          |
                                           account/rateLimits/read
                                                          v
                                                [ ChatGPT Backend ]
```

### Acknowledgements
Special thanks to the **[LINUX DO](https://linux.do/)** community for inspiration, architectural discussions, and continuous feedback!

---

## 📄 License

MIT License © 2026 [Au1Bhi](https://github.com/Au1Bhi)

