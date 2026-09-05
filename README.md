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
2. 填入要绑定的 Telegram Bot Token
3. 点击「获取验证码」
4. 去 Telegram 查看 Bot 推送的验证码（1 小时内有效）
5. 把验证码填回界面，点击「验证」
6. 配对成功后自动进入工作界面，之后每次启动自动连接，无需再操作

## 认证模型

不使用固定的共享密钥。每台设备通过一次性验证码完成配对后，
会获得一个专属的设备令牌（保存在本机 `~/.claude-agent/config.json`），
之后凭这个令牌自动认证。可以在 Bot 里单独吊销某一台设备，不影响其他设备。

## 权限说明

一台设备可以绑定一个 Bot（配对时指定）。绑定后，只有这个 Bot 能对它下发命令，
其他 Bot 无权操作。未绑定时任意 Bot 可操作。

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
pyinstaller --onedir --windowed --name agent agent.py
```

CI（`.github/workflows/build.yml`）会在 push 到 master 时自动为
Windows / macOS / Linux 三个平台分别构建、压缩并发布。
