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
- 🛑 **零扣费拦截保证**：额度 $\le 3\%$ 时自动向 Codex 发送 `SIGSTOP`，冻结网络请求与 CPU 执行，**杜绝任何 API Credits 消耗**。
- 🔄 **原状无缝恢复（Session & Locks 保全）**：
  - 不采用粗暴的 kill 进程方案，使用内核挂起/唤醒，终端画面、RAM 内存、活跃任务不丢。
  - 自动快照记录会话 ID、标题、路径，自动校验并清理无进程占用的死锁（Writer Locks），**会话列表与任务进度完整无损**。
- ⚡ **智能自主续跑（Idle Auto-Continue）与唤醒注入**：
  - **额度恢复唤醒续跑**：当 5h 额度恢复唤醒后，守护程序自动向活跃会话注入「继续」，无缝接力执行。
  - **回合完成自主接力**：当 Codex 跑完一个回合进入空闲（`idle`）状态，守护程序自动等待 5 秒防抖缓冲后自动发送「继续」，让长线任务（如全流程解题、代码重构调优）永不停歇自主向前推进！
- 🔔 **桌面通知推送**：通过系统 `notify-send` 实时推送暂停预警（含预计刷新倒计时）与恢复提醒。
- ⚙️ **Systemd 用户服务常驻**：一行命令注册为用户级后台守护服务，开机/登录自动常驻，低至 7MB 内存占用，零负担。
- 💻 **优雅的命令行工具**：提供 `codex-quota`、`codex-guard status`、`codex-guard sessions` 等便捷命令。

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

### 3. 手动暂停 / 恢复
如需手动强制控制正在运行的任务：
```bash
codex-guard pause   # 暂停所有运行中的 Codex 客户端进程并记录快照
codex-guard resume  # 唤醒所有被暂停的 Codex 客户端进程，并自动发送「继续」
```

### 4. 守护服务管理
```bash
# 查看后台守护服务日志与运行状态
systemctl --user status codex-quota-guard

# 查看详细守护时间线日志
tail -f ~/.codex/quota_guard.log

# 自定义阈值与唤醒指令 (例如改为剩余 5% 时暂停，恢复时发送 "continue")
codex-guard service install --threshold 5.0 --interval 20 --continue-prompt "继续"

# 卸载守护服务
codex-guard service uninstall
```

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

---

## 📄 License

MIT License © 2026 [Au1Bhi](https://github.com/Au1Bhi)
