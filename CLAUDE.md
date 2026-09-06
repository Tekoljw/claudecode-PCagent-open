# claudecode-PCagent-open — PC 远程控制客户端

跨平台（Windows/macOS/Linux）PC 远程控制客户端，配合
[claude-code-AI](https://github.com/Tekoljw/claude-code-AI) 的 Telegram Bot 使用。
**这个仓库是 claude-code-AI 的 git 子模块**（挂载路径 `agent-client/`）——这是
唯一的权威副本，不要再手动 `cp` 同步到别处，也不会有别处再手动 `cp` 过来。

## 铁律：这里的代码改了不需要、也不能重启 claude-code-AI 的 bot 服务

这是本仓库存在的**根本原因**：`agent.py` 是跑在用户自己电脑上的独立进程，
跟 claude-code-AI 里 `agent-server.js`/`claude-bot.js`（跑在 EC2、需要重启才能生效
的 bot 服务进程）完全分开。改这个仓库、提交、推送、等 CI 出新版安装包——全程不涉及
重启任何东西。这也是为什么它被拆成独立子仓库而不是留在 claude-code-AI 的
`agent/` 目录里：`agent/` 下的 `agent-server.js`/`pc-mcp-server.js` 是真正的
bot 服务端代码，改了必须重启 bot 才生效，混在一起会让"这次改动要不要动到生产 bot"
变得含糊。

## 架构

单文件 `agent.py`：`websocket-client` 连到 `wss://claudbotjs.doez.ai/agent`
（跟 claude-code-AI 的 Mini App 共用同一个 CloudFront 分发/端口），验证码配对 +
设备专属 token 认证，收到 `command` 消息就用 `subprocess.run` 本地执行并回传结果。
另外内置一套"灯光库"能力，供现场灯光控制使用：

- **USB 灯光控制**（`run_light_cli`/`send_midi_message`/`send_dmx_frame`）：
  `agent.exe light list|midi|dmx` 命令行子命令，通过 `python-rtmidi`/`pyserial`
  直接操控 MIDI/DMX 硬件。
- **灯光库本地 SQLite**（`light_library.db`，`_light_library_db()`/
  `read_light_memory()`/`write_light_memory()`）：存现场灯具的白话定义 + 5 张
  参考照片（BLOB），**这台 PC 是灯光库数据的真实数据源**，claude-code-AI 那边的
  云端数据库只是"最后一次成功写入后"的只读镜像，绝不反向覆盖这里。
- **设备扫描**（`light_list_devices()`）：枚举当前系统识别到的 MIDI 端口/串口
  设备，通过 WebSocket 消息 `scan_devices`/`scan_result` 响应远端请求——只如实
  展示，不自动把结果映射成灯具型号定义（型号名跟物理端口的对应关系靠人工在
  `light_devices.json` 里维护）。

WebSocket 协议完整清单、灯光库读写细节，见 claude-code-AI 仓库的
`agent/README.md`（那边文档更全，这里不重复）。

## 构建

**不要手动打包，也不要直接跑上游/官方发布产物**——三平台可执行文件全部由
`.github/workflows/build.yml` 的 GitHub Actions 自动构建：push 到 `master`
即触发，产物自动传到 S3（`latest/agent-{windows,macos,linux}.zip`），Telegram
里的"⬇️ 下载远程端"按钮直接指向这里，不需要手动介入。

本地跑源码调试：
```bash
pip install -r requirements.txt
python agent.py
```

`python-rtmidi`（C 扩展，PyInstaller 容易漏掉原生绑定）打包时需要
`--collect-all rtmidi`，已经在 `build.yml` 里配好，改依赖时注意保留这个 flag。

## 测试

无自动化测试框架。语法检查 `python -m py_compile agent.py`。改灯光库/扫描相关
函数时，参考 claude-code-AI 仓库里验证 SQLite 迁移用的思路：`import agent` 后
monkeypatch `agent.LIGHT_LIBRARY_DB_FILE` 到临时目录，直接调用
`read_light_memory()`/`write_light_memory()` 断言往返一致——不需要启动真实 GUI
（`__main__` 守卫挡住了，纯 import 不会弹窗）。真实硬件（MIDI/DMX 设备、真实
串口）必须在装了对应硬件的电脑上人工验证，开发环境通常没有真设备。
