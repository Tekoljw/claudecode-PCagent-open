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

## ⛔ 铁律：改了 agent.py 就必须同时提升 `__version__`

改这个文件里任何会影响运行时行为的代码（不只是文案/注释），提交前必须把顶部的
`__version__` 往上提一位。**没有例外，不要图省事漏掉。**

**为什么定这条**：2026-09-08 修 WebSocket 心跳（`ping_interval`/`ping_timeout`）那次
提交漏了改版本号——这个仓库没有自动生成/CI 打版本号的机制，S3 上的 `latest/` 包
每次构建都直接覆盖，如果代码变了但版本号没变，用户手上跑的旧版和刚发布的新版会
显示同一个号，没法通过 GUI 标题栏或日志判断自己是不是已经更新到位，等于让版本号
这个刚加的排错工具直接失效。

判断标准：改动**不影响运行时行为**（纯注释、纯文档措辞调整）可以不提；只要改了
一行会实际执行的代码——哪怕只是调整一个参数的默认值——就要提版本号。

## 架构

单文件 `agent.py`：`websocket-client` 连到 `wss://claudbotjs.doez.ai/agent`
（跟 claude-code-AI 的 Mini App 共用同一个 CloudFront 分发/端口）。2026-09-16 起
认证换成服务端签发的强密钥（不再是验证码）：管理员在 Telegram 用 `/pc_newkey`
生成一把密钥推给某个 operator，粘贴进 GUI 后本地**加密**保存（`encrypt_key`/
`decrypt_key`，本机专属的对称密钥另存一个文件，拷走 `config.json` 本身解不开），
之后每次启动/断线都自动带着这把密钥重连，无限重试直到用户手动勾掉"启用远程
连接"或退出——不再需要人工过一遍配对流程。密钥被管理员吊销后，服务端会在一轮
心跳内主动断开，此后自动重连会持续被拒绝（收到 WS 关闭码 4001），需要联系管理员
拿新密钥、在 GUI 里点"更换密钥"重新粘贴。收到 `command` 消息后先核对里面的
`executedBy`/`isOwner`（是不是这把密钥的 operator 本人或 owner）——这是跟服务端
路由校验并列的第二道硬卡，不只信任传输链路——通过才用 `subprocess.run` 本地
执行并回传结果。另外内置一套"灯光库"能力，供现场灯光控制使用：

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

`python-rtmidi`/`cryptography`（都带 C 扩展，PyInstaller 容易漏掉原生绑定）
打包时需要 `--collect-all rtmidi --collect-all cryptography`，已经在
`build.yml` 里配好，改依赖时注意保留这两个 flag。

## 测试

无自动化测试框架。语法检查 `python -m py_compile agent.py`。改灯光库/扫描相关
函数时，参考 claude-code-AI 仓库里验证 SQLite 迁移用的思路：`import agent` 后
monkeypatch `agent.LIGHT_LIBRARY_DB_FILE` 到临时目录，直接调用
`read_light_memory()`/`write_light_memory()` 断言往返一致——不需要启动真实 GUI
（`__main__` 守卫挡住了，纯 import 不会弹窗）。真实硬件（MIDI/DMX 设备、真实
串口）必须在装了对应硬件的电脑上人工验证，开发环境通常没有真设备。
