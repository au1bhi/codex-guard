# Codex Guard 🛡️

Linux 上的 Codex 额度守护：额度不足时暂停，额度恢复后自动恢复已记录的任务。

## 安装或更新

```bash
bash scripts/install.sh
```

安装后守护自动开启，并随用户登录启动。更新会移除旧命令别名，只保留以下三个日常命令。

## 使用

打开 `codex-top`，所有日常操作都在界面内完成：

| 按键 | 操作 |
| --- | --- |
| `g` | 开启 / 关闭后台守护 |
| `a` | 开启 / 关闭自动发送「继续」 |
| `p` | 暂停任务 |
| `c` | 唤醒任务 |
| 空格 | 向有任务历史的空闲根会话发送「继续」并启动回合；跳过空白、忙碌和子代理会话 |
| `r` | 刷新 |
| `+/-` | 调整界面刷新速度 |
| `q` / `Ctrl+C` | 退出界面 |

界面显示守护和自动继续的 ON/OFF 状态；配置尚未被后台读取时显示「待后台应用」。操作在后台执行，失败会提示原因。

自动继续开关会保存，重开界面和重启守护后仍然有效，通常在下一次后台检查（默认 10 秒）生效。关闭后不再空闲续跑，也不在额度恢复时自动发送「继续」；额度恢复仍会解除守护暂停。手动按空格发送不受自动开关影响，但会检查额度。

自动继续由守护后台执行，守护关闭时不执行，设置保留。关闭守护会解除它记录并验证身份的客户端暂停，不杀 Codex、不自动发送消息。退出界面不会停止后台守护。

也保留 `codexguard-start` / `codexguard-stop`，需要时可直接从终端开关守护。

默认保护线为剩余 **3%**，每 **10 秒**检查 5h 和周额度；额度不可用时暂停，确认恢复后唤醒。默认保留空闲自动续跑（5 秒缓冲后发送「继续」）。

需要 Linux、Python 3.8+、systemd 用户服务，以及支持本地 daemon/queue 的 Codex CLI。本次实机只读验证版本为 `0.160.0`。自动重开会话需要可用终端或 `screen`。

**本地轮询与中断无法保证绝对零扣费。** 详细修复和限制见[审查报告](docs/SECURITY_REVIEW.md)。状态默认保存到 `~/.codex`，支持 `CODEX_HOME`。

## 卸载

```bash
bash scripts/uninstall.sh
```

## 开发验证

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile bin/codex-guard
shellcheck scripts/install.sh scripts/uninstall.sh
```

感谢 [LINUX DO](https://linux.do/) 社区的启发及反馈。MIT 许可，详见 [LICENSE](LICENSE)。
