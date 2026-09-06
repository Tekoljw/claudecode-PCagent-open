# Claude PC Agent

跨平台的 PC 远程控制客户端（Windows / macOS / Linux），配合 Telegram Bot 使用，
让 Bot 可以在这台电脑上远程执行命令。

## 下载

从 [Actions](https://github.com/Tekoljw/claudecode-PCagent-open/actions) 对应的构建产物获取，
或直接联系 Bot 管理员索取下载链接。下载到的是一个 zip 压缩包，**解压后
运行文件夹里的 `agent` 程序**（不是单个可执行文件——见下面"打包"一节
为什么）。

## 从源码运行

```bash
pip install -r requirements.txt
python agent.py
```

## 首次使用：配对

1. 启动后，界面显示这台设备的 ID（MAC 地址）
2. 点击「获取验证码」
3. 去 Telegram 查看管理员收到的验证码推送（1 小时内有效）
4. 把验证码填回界面，点击「验证」
5. 配对成功后自动进入工作界面，之后每次启动自动连接，无需再操作

配对完成后，这台设备归哪个 Bot 管理是服务端的事——管理员在 Telegram 里
的设备列表中手动绑定，客户端不需要也无法指定。

## 认证模型

不使用固定的共享密钥。每台设备通过一次性验证码完成配对后，
会获得一个专属的设备令牌（保存在本机 `~/.claude-agent/config.json`），
之后凭这个令牌自动认证。可以在 Bot 里单独吊销某一台设备，不影响其他设备。

## 权限说明

一台设备可以绑定一个 Bot（配对完成后由管理员在服务端指定）。绑定后，只有这个
Bot 能对它下发命令，其他 Bot 无权操作。未绑定时任意 Bot 可操作。

## USB 灯光控制（MIDI / DMX）

同一个 `agent` 程序额外支持一个 `light` 子命令，用来操控这台电脑上通过 USB
连接的灯光设备。不是新协议——Bot 本来就能把任意 shell 命令送到这台电脑执行，
`light` 只是让程序被这样调用时执行完就退出，不进入 GUI：

```bash
agent light list                                                    # 列出可用 MIDI 端口 / 串口设备
agent light midi --device 金刚台 --type note_on --note 60 --velocity 100
agent light dmx  --device 白色台子 --set 1=255 --set 2=128          # Enttec DMX USB Pro 协议
```

`--device` 可以直接写 MIDI 端口名子串/索引或 COM 口，也可以是跟程序同目录的
`light_devices.json` 里配置的别名（先跑 `light list` 现场核对设备名，再填写）：

```json
{
  "金刚台":   { "type": "midi", "port_name": "USB MIDI Device" },
  "白色台子": { "type": "dmx",  "com_port": "COM3", "baudrate": 57600 }
}
```

## 隐私与安全

- 通信全程走 TLS（`wss://`），证书正常校验，不接受禁用校验的修改
- 执行的每条命令都由绑定的 Bot 主动下发，客户端不会主动上报数据
- 本地配置文件只包含设备令牌和连接参数，不含任何密钥硬编码在代码里

## 打包

用 `--onedir` 而不是 `--onefile`：onefile 在 Windows 上会拆成"引导进程
解压临时文件"+"真正执行代码的子进程"两个进程，两个都各自弹出一个 GUI
窗口（PyInstaller 6.x 已知问题，实测复现过），onedir 没有这层中间的
bootloader，天然只有一个进程/窗口。代价是产物是一个文件夹，所以 CI
打包完会压缩成 zip 再发布。

```bash
pip install pyinstaller
pyinstaller --onedir --windowed --name agent --collect-all rtmidi --hidden-import=serial.tools.list_ports agent.py
```

`--collect-all rtmidi` 是因为 `python-rtmidi`（灯光控制用到）是 C 扩展，
PyInstaller 静态分析常漏掉其原生绑定。Linux 上从源码构建它还需要系统装
`libasound2-dev`（CI 已处理，本地在 Linux 上打包需要自己先装）。

CI（`.github/workflows/build.yml`）会在 push 到 master 时自动为
Windows / macOS / Linux 三个平台分别构建、压缩并发布。
