#!/usr/bin/env python3
"""
PC 远程控制 Agent - 跨平台（Windows/macOS/Linux）
运行方式：python agent.py 或双击可执行文件

认证模型（2026-09-16 起，第二版：密钥持久化，Bot 不是权限维度）：
- 服务端/数据库对 PC 本身仍然零持久化（不存 MAC/hostname 这类"设备身份"）；
  但每台客户端持有一把服务端签发的强密钥（不是弱验证码），这把密钥归属于
  某个 operator（不是某个 Bot——Bot 在这套模型里完全不是权限维度）。
- 管理员在 Telegram 里用 /pc_newkey 生成密钥，推一条消息给对应的人（那条
  消息 5 分钟后自动撤回），把密钥粘贴进这里的"密钥"输入框，保存后**加密**
  存本地（见 encrypt_key/decrypt_key，本机专属的对称密钥单独存一个文件，
  拷走 config.json 本身解不开）。
- 之后每次启动直接用保存的密钥连接，不需要人工干预；断线（网络波动、
  服务端重启……任何原因）会无限自动重连，直到用户手动勾掉"启用远程连接"
  或退出程序——不会像上一版那样每次断线都要重新走一遍人工流程。
- 密钥被管理员吊销后，服务端会在一轮心跳内主动断开这条连接，此后自动重连
  会不断失败（服务端拒绝这把已吊销的密钥）；界面上会看到反复"连接被拒绝"，
  这时需要联系管理员要一把新密钥，用"更换密钥"重新粘贴。
- 每条下发的命令都带着真实发起人的 Telegram 用户 ID（和"是不是 owner"这个
  服务端算好的标记）：客户端自己也会核对一遍——只有这把密钥的 operator 本人
  或 owner 的命令才会真的执行，这是跟服务端路由校验并列的第二道硬卡，
  不是只信任传输链路。
"""

import sys
import json
import threading
import time
import socket
import os
import subprocess
import platform
import sqlite3
import base64
from pathlib import Path
import websocket

try:
    # 用 FreeSimpleGUI 而非 PySimpleGUI：后者自 5.x 起转为商业授权模式，
    # 4.60.5（此前锁定的版本）已从 PyPI 撤下装不了，且新版首次运行会弹出
    # 注册/许可证对话框，破坏"双击就能用"。FreeSimpleGUI 是社区 fork，
    # 保持 4.x 最后一个开源版本的 LGPL 协议，API 完全兼容，一行 import 切换。
    import FreeSimpleGUI as sg
except ImportError:
    print("需要安装依赖: pip install -r requirements.txt")
    sys.exit(1)

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:
    print("需要安装依赖: pip install -r requirements.txt（缺 cryptography，密钥加密存储要用）")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────
#  配置
# ─────────────────────────────────────────────────────────────────────────

# 手工维护，没有自动生成机制——改了行为就顺手加一位，方便排查"用户手上跑的是不是
# 最新版"（2026-09-07 实锤过：CI 每次都覆盖 S3 上的 latest 包，此前完全没有版本号，
# 没法确认运行中的 exe 对应哪次提交）。GUI 标题栏和主界面都会显示这个值。
# 2026-09-16：密钥持久化重写，协议再次破坏性变更（旧版的 verify_pairing_code
# 服务端已不再认识），跳到 3.0.0。
__version__ = "3.0.0"

CONFIG_DIR = Path.home() / ".claude-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "agent.log"
# 本机专属的对称加密密钥，单独存一个文件——只用来加密/解密 config.json 里的
# 那把服务端密钥，不随 config.json 一起分享/拷贝就解不开，防止简单地复制
# config.json 就能在另一台机器上冒充这台设备连接。
LOCAL_KEYFILE = CONFIG_DIR / ".local.key"

# 心跳判活：超过这么久没收到服务端任何消息，就主动判定连接已死（不能只靠收到一次
# pc_info 就永远显示绿色）。服务端心跳间隔 15 秒，这里留约 3 倍余量。
HEARTBEAT_STALE_SECS = 45

DEFAULT_CONFIG = {
    # 复用 claude-bot 现有的 CloudFront 域名和 Mini App 路径，
    # Agent 走 wss://claudbotjs.doez.ai/agent，不需要单独的域名/证书/端口。
    # （旧域名 claudebot.bc361.com 过渡期内仍保留，见 CLAUDE.md）
    "server_host": "claudbotjs.doez.ai",
    "server_port": 443,
    "name": None,       # 本机展示名，留空时用 hostname；服务端只读展示，不做修改
    "key_enc": None,    # 服务端签发的密钥，加密后存这里；明文密钥只活在内存里
    "enabled": True,
}


# ─────────────────────────────────────────────────────────────────────────
#  工具函数
# ─────────────────────────────────────────────────────────────────────────

def log(msg):
    """记录日志"""
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{ts}] {msg}"
    print(log_msg)
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_msg + "\n")
    except Exception:
        pass


def get_hostname():
    return socket.gethostname()


def get_os_name():
    return platform.system().lower()


def load_config():
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        if CONFIG_FILE.exists():
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                cfg = DEFAULT_CONFIG.copy()
                cfg.update(json.load(f))
                return cfg
    except Exception as e:
        log(f"加载配置失败: {e}")
    return DEFAULT_CONFIG.copy()


def save_config(config):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
    except Exception as e:
        log(f"保存配置失败: {e}")


def _local_fernet():
    """本机专属的加解密器——密钥文件第一次用到时生成，此后固定不变。
    单独存放、权限收紧到仅本用户可读，跟 config.json 分开，拷走 config.json
    本身不含解密所需的东西。"""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    if not LOCAL_KEYFILE.exists():
        LOCAL_KEYFILE.write_bytes(Fernet.generate_key())
        try:
            os.chmod(LOCAL_KEYFILE, 0o600)
        except Exception:
            pass  # Windows 上 chmod 效果有限，但不影响功能，只是纵深防御的一层
    return Fernet(LOCAL_KEYFILE.read_bytes())


def encrypt_key(plain_key):
    return _local_fernet().encrypt(plain_key.strip().encode("utf-8")).decode("ascii")


def decrypt_key(token):
    if not token:
        return None
    try:
        return _local_fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, Exception) as e:
        log(f"解密本地密钥失败（本地密钥文件可能被换过/损坏）: {e}")
        return None


# ─────────────────────────────────────────────────────────────────────────
#  WebSocket 客户端
# ─────────────────────────────────────────────────────────────────────────

class AgentClient:
    def __init__(self, config, callbacks):
        self.config = config
        self.ws = None
        self.connected = False       # WebSocket 传输层是否连通
        self.authenticated = False   # 应用层是否已认证（收到过 pc_info）
        self.hostname = get_hostname()
        self.os = get_os_name()
        self.pc_name = None
        self.operator_id = None      # 这把密钥归属的 operator（来自服务端 pc_info）
        self.callbacks = callbacks or {}
        self.reconnect_count = 0
        self._connecting = False  # 防止开关被连续点击时启动重复的连接线程
        self.last_server_seen = None  # 最近一次收到服务端任意消息的时间，心跳判活用

    def _cb(self, name, *args):
        fn = self.callbacks.get(name)
        if fn:
            try:
                fn(*args)
            except Exception as e:
                log(f"回调 {name} 出错: {e}")

    def get_server_url(self):
        port = self.config.get("server_port", 443)
        port_part = "" if port == 443 else f":{port}"
        return f"wss://{self.config['server_host']}{port_part}/agent"

    def get_plain_key(self):
        return decrypt_key(self.config.get("key_enc"))

    def connect(self):
        """后台线程入口：只要本地有已保存的密钥，就无限循环"连接 → 断开 → 退避
        等待 → 再连"，直到用户手动禁用或密钥被清空——不需要任何人工步骤。
        写成迭代循环而不是"on_close 里递归调用 connect()"，是特意避免的一个坑：
        旧版（5 次重连上限）用递归也没事，反正最多 5 层；这版断线重连没有次数
        上限，24/7 挂着跑，网络抖个几千次，递归会让 Python 调用栈无限往深
        长（每次重连的栈帧永远不会退栈，因为下一次 connect() 是在上一次的
        on_close 回调里直接调用的），迟早栈溢出。循环没有这个问题。"""
        while True:
            if not self.config.get("enabled"):
                log("Agent 已禁用，停止连接循环")
                self._connecting = False
                return
            key = self.get_plain_key()
            if not key:
                log("本地没有保存密钥，等待用户粘贴")
                self._cb("on_status_change", "未配置密钥", False)
                self._connecting = False
                return

            self._connect_once()

            if not self.config.get("enabled") or not self.get_plain_key():
                self._connecting = False
                return
            self.reconnect_count += 1
            wait_time = min(2 ** min(self.reconnect_count, 5), 30)
            log(f"将在 {wait_time} 秒后重连（第 {self.reconnect_count} 次，无限重试）...")
            time.sleep(wait_time)

    def _connect_once(self):
        """建立一次连接，阻塞到这条连接断开为止（ws.run_forever 的语义）。
        不在这里做任何重试决策——重试节奏统一由 connect() 的循环控制。"""
        try:
            server_url = self.get_server_url()
            log(f"正在连接到 {server_url}...")

            # 目标是 CloudFront（ACM 签发的可信证书），必须正常校验证书，
            # 不能禁用——这是防中间人劫持命令通道的唯一屏障。
            ws = websocket.WebSocketApp(
                server_url,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close,
                on_open=self.on_open,
            )
            self.ws = ws
            # ping_interval/ping_timeout 不能省：CloudFront 对空闲连接有默认 60 秒左右
            # 的超时，中间人（CloudFront/路由器/NAT）可能早就把连接悄悄断了，但本地
            # socket 在收到明确的 RST/FIN 之前会一直显示"已连接"——没有心跳包，客户端
            # 可能永远发现不了连接已经死了。25/10 秒留出足够余量，一旦探测失败会触发
            # on_close（run_forever 内部调用完 on_close 后自然返回，回到 connect() 的循环）。
            ws.run_forever(ping_interval=25, ping_timeout=10)
        except Exception as e:
            log(f"连接失败: {e}")
            self.connected = False
            self._cb("on_status_change", "连接失败", False)

    def on_open(self, ws):
        self.connected = True
        self._connecting = False
        self.reconnect_count = 0
        self.last_server_seen = time.time()
        log("WebSocket 连接打开，发送密钥认证")
        key = self.get_plain_key()
        if not key:
            log("密钥丢失（解密失败），断开等待用户重新粘贴")
            ws.close()
            return
        ws.send(json.dumps({
            "type": "connect",
            "key": key,
            "hostname": self.hostname,
            "os": self.os,
            "name": self.config.get("name") or self.hostname,
        }))
        self._cb("on_status_change", "正在认证…", False)

    def on_message(self, ws, message):
        # 收到服务端任意消息都算一次"确认还活着"——不止 pong，这样即使服务端
        # 只发 pc_info/command 这类业务消息，也一样能重置心跳判活的计时。
        self.last_server_seen = time.time()
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "pc_info":
                self.pc_name = data.get("name", "未命名")
                self.operator_id = data.get("operatorId")
                self.authenticated = True
                self._cb("on_pc_info", self.pc_name, self.operator_id)
                self._cb("on_status_change", "已连接", True)
                log(f"认证成功: {self.pc_name}（operator={self.operator_id}）")

            elif msg_type == "command":
                cmd_id = data.get("id")
                cmd = data.get("command")
                executed_by = str(data.get("executedBy") or "")
                is_owner_cmd = bool(data.get("isOwner"))
                # 客户端自己核对一遍发起人身份——服务端路由时已经挡过一次，这里
                # 是第二道硬卡：不是这把密钥的 operator 本人、也不是 owner，拒绝执行。
                if not is_owner_cmd and self.operator_id and executed_by != str(self.operator_id):
                    log(f"拒绝执行：发起人 {executed_by} 既不是本机 operator（{self.operator_id}）也不是 owner")
                    ws.send(json.dumps({
                        "type": "command_result", "id": cmd_id,
                        "status": "failed", "output": "拒绝执行：发起人身份跟本机不匹配",
                    }))
                    return
                self._cb("on_command_from", executed_by, is_owner_cmd)
                log(f"执行命令（来自 {executed_by}）: {cmd}")
                result = self.execute_command(cmd)
                ws.send(json.dumps({
                    "type": "command_result",
                    "id": cmd_id,
                    "status": "success" if result["success"] else "failed",
                    "output": result["output"],
                }))

            elif msg_type == "read_memory":
                req_id = data.get("id")
                result = read_light_memory()
                ws.send(json.dumps({
                    "type": "memory_content",
                    "id": req_id,
                    **result,
                }))

            elif msg_type == "write_memory":
                req_id = data.get("id")
                result = write_light_memory(data.get("content"))
                ws.send(json.dumps({
                    "type": "memory_write_result",
                    "id": req_id,
                    **result,
                }))

            elif msg_type == "scan_devices":
                req_id = data.get("id")
                # 复用 light_list_devices()（`agent.exe light list` CLI 子命令用的同一个函数）
                # 而不是另写一遍枚举逻辑——只是给用户看这台 PC 实际接了什么，不自动生成
                # 灯具型号定义，型号跟物理端口的对应关系仍靠人工在 light_devices.json 里维护。
                scan = light_list_devices()
                ws.send(json.dumps({
                    "type": "scan_result",
                    "id": req_id,
                    "ok": True,
                    "devices": {
                        "midi": scan.get("midi_ports", []),
                        "serial": scan.get("serial_ports", []),
                    },
                    "error": scan.get("midi_error") or scan.get("serial_error"),
                }))

            elif msg_type == "ping":
                ws.send(json.dumps({"type": "pong"}))

        except Exception as e:
            log(f"处理消息失败: {e}")

    def on_error(self, ws, error):
        log(f"WebSocket 错误: {error}")
        self.connected = False
        self._cb("on_status_change", "连接错误", False)

    def on_close(self, ws, close_status_code, close_msg):
        log(f"WebSocket 关闭: code={close_status_code} msg={close_msg}")
        self.connected = False
        self.authenticated = False
        reason = "连接已断开，正在自动重连…"
        if close_status_code == 4001:
            reason = "密钥无效或已被管理员吊销，请联系管理员获取新密钥后点\"更换密钥\""
        self._cb("on_status_change", reason, False)
        self._cb("on_disconnected", close_status_code)
        # 不在这里发起重连——run_forever() 到这里就要返回了，控制权交回
        # connect() 的循环，由它统一决定要不要、等多久再重连，见 connect() 里
        # 那段"为什么用循环不用递归"的注释。

    def execute_command(self, cmd):
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return {"success": result.returncode == 0, "output": result.stdout + result.stderr}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "命令执行超时"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def set_enabled(self, enabled):
        """开关立即生效：关闭时断开连接，开启时拉起新的连接线程。
        这是用户唯一的"手动离线"入口——不勾掉这个、不退出程序，客户端就会
        一直自动重连，不会像旧版那样断一次就死等人工重新配对。"""
        self.config["enabled"] = enabled
        save_config(self.config)
        if enabled:
            if not self.connected and not self._connecting:
                self._connecting = True
                self.reconnect_count = 0
                threading.Thread(target=self.connect, daemon=True).start()
        else:
            if self.ws:
                self.ws.close()

    def set_key(self, plain_key):
        """保存一把新密钥（加密落盘）并立即尝试连接——用于首次配置，或吊销后换新。"""
        self.config["key_enc"] = encrypt_key(plain_key)
        save_config(self.config)
        if self.ws:
            self.ws.close()
        self.reconnect_count = 0
        if self.config.get("enabled") and not self._connecting:
            self._connecting = True
            threading.Thread(target=self.connect, daemon=True).start()

    def stop(self):
        self.config["enabled"] = False
        if self.ws:
            self.ws.close()


# ─────────────────────────────────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────────────────────────────────

FONT_TITLE = ("Segoe UI", 16, "bold")
FONT_SUB = ("Segoe UI", 9)
FONT_LABEL = ("Segoe UI", 10)
FONT_BOLD = ("Segoe UI", 10, "bold")
ACCENT = "#4FC3F7"


def create_gui(config):
    sg.theme("DarkGrey13")
    sg.set_options(font=FONT_LABEL)
    has_key = bool(config.get("key_enc"))

    key_layout = [
        [sg.Text("粘贴管理员发给你的密钥", font=FONT_BOLD, pad=((0, 0), (0, 10)))],
        [sg.Text("在 Telegram 里找 /pc_newkey 生成的那条消息（5 分钟后会自动撤回，\n请先复制），粘贴到下面，保存后自动连接，以后不用再输入",
                  font=FONT_SUB, text_color="grey", pad=((0, 0), (0, 12)))],
        [sg.InputText("", key="-KEY_INPUT-", size=(48, 1))],
        [sg.Button("保存并连接", key="-SAVE_KEY-", size=(14, 1))],
        [sg.Text("", key="-KEY_STATUS-", font=FONT_SUB, text_color=ACCENT,
                  size=(50, 2), pad=((0, 0), (12, 0)))],
    ]

    main_layout = [
        [sg.Text("PC 名称", font=FONT_SUB, text_color="grey", size=(10, 1)),
         sg.Push(), sg.Text("", key="-PC_NAME-", font=FONT_LABEL)],
        [sg.Text("状态", font=FONT_SUB, text_color="grey", size=(10, 1)),
         sg.Push(),
         sg.Text("●", key="-STATUS_DOT-", text_color="#E53935", font=("Segoe UI", 12)),
         sg.Text("", key="-STATUS-", font=FONT_LABEL)],
        [sg.Text("最后心跳", font=FONT_SUB, text_color="grey", size=(10, 1)),
         sg.Push(), sg.Text("", key="-LAST_BEAT-", font=FONT_LABEL)],
        [sg.Text("最近命令来自", font=FONT_SUB, text_color="grey", size=(10, 1)),
         sg.Push(), sg.Text("", key="-LAST_CMD_FROM-", font=FONT_LABEL)],
        [sg.HSeparator(pad=((0, 0), (14, 12)))],
        [sg.Checkbox("启用远程连接（勾掉=手动离线，停止自动重连）",
                      default=config.get("enabled", True), key="-ENABLED-", enable_events=True)],
        [sg.Button("更换密钥", key="-CHANGE_KEY-", pad=((0, 0), (10, 0)))],
    ]

    layout = [
        [sg.Text("🖥  Agent", font=FONT_TITLE)],
        [sg.Text("PC 远程控制客户端", font=FONT_SUB, text_color="grey"),
         sg.Push(), sg.Text(f"v{__version__}", font=FONT_SUB, text_color="grey")],
        [sg.HSeparator(pad=((0, 0), (14, 14)))],
        [sg.Text("显示名称", font=FONT_SUB, text_color="grey", size=(10, 1)),
         sg.Push(), sg.InputText(config.get("name") or get_hostname(), key="-NAME-", size=(22, 1))],
        [sg.HSeparator(pad=((0, 0), (14, 14)))],
        [sg.Column(key_layout, key="-KEY_COL-", visible=not has_key)],
        [sg.Column(main_layout, key="-MAIN_COL-", visible=has_key)],
        [sg.HSeparator(pad=((0, 0), (18, 14)))],
        [sg.Button("保存设置", key="-SAVE-"), sg.Button("打开日志"),
         sg.Push(), sg.Button("退出", button_color=("white", "#B00020"))],
    ]

    return sg.Window(f"Agent v{__version__}", layout, finalize=True, keep_on_top=False,
                      margins=(24, 20), element_padding=(4, 4))


def main():
    log(f"Agent 启动 v{__version__}")
    config = load_config()

    window = create_gui(config)

    # 后台线程的回调必须通过 write_event_value 转发到主循环，
    # 不能直接操作 GUI 控件（PySimpleGUI 不是线程安全的）。
    client = AgentClient(config, {
        "on_status_change": lambda status, connected: window.write_event_value("-EVT_STATUS-", (status, connected)),
        "on_pc_info": lambda name, operator_id: window.write_event_value("-EVT_PCINFO-", (name, operator_id)),
        "on_disconnected": lambda code: window.write_event_value("-EVT_DISCONNECTED-", code),
        "on_command_from": lambda who, is_owner: window.write_event_value("-EVT_CMD_FROM-", (who, is_owner)),
    })

    if client.get_plain_key():
        threading.Thread(target=client.connect, daemon=True).start()

    last_beat = time.time()

    while True:
        event, values = window.read(timeout=1000)

        if event == sg.WINDOW_CLOSED or event == "退出":
            break

        if event == "-SAVE_KEY-":
            key = (values.get("-KEY_INPUT-") or "").strip()
            if not key:
                window["-KEY_STATUS-"].update("请先粘贴密钥", text_color="red")
            else:
                config["name"] = (values.get("-NAME-") or "").strip() or None
                client.set_key(key)
                window["-KEY_COL-"].update(visible=False)
                window["-MAIN_COL-"].update(visible=True)
                window["-STATUS-"].update("正在连接…")
                log("已保存新密钥，开始连接")

        elif event == "-CHANGE_KEY-":
            window["-MAIN_COL-"].update(visible=False)
            window["-KEY_COL-"].update(visible=True)
            window["-KEY_INPUT-"].update("")
            window["-KEY_STATUS-"].update("")

        elif event == "-EVT_DISCONNECTED-":
            # 密钥仍然有效的话，客户端在后台会自动无限重连，界面留在原地
            # 就行，不用像旧版那样切回什么"配对表单"——除非密钥被吊销了
            # （4001），那种情况下手动重连也没用，提示用户换密钥。
            code = values[event]
            if code == 4001:
                window["-KEY_STATUS-"].update("密钥无效或已被吊销，请点\"更换密钥\"粘贴新的", text_color="red")

        elif event == "-EVT_STATUS-":
            status, connected = values[event]
            window["-STATUS-"].update(status)
            window["-STATUS_DOT-"].update(text_color="#43A047" if connected else "#E53935")

        elif event == "-EVT_PCINFO-":
            name, operator_id = values[event]
            window["-PC_NAME-"].update(name + (f"（operator: {operator_id}）" if operator_id else ""))

        elif event == "-EVT_CMD_FROM-":
            who, is_owner = values[event]
            window["-LAST_CMD_FROM-"].update(who + ("（owner）" if is_owner else "") + " · " + time.strftime("%H:%M:%S"))

        elif event == "-ENABLED-":
            enabled = values["-ENABLED-"]
            client.set_enabled(enabled)
            window["-STATUS-"].update("已手动离线" if not enabled else "正在连接…")
            window["-STATUS_DOT-"].update(text_color="#E53935")
            log(f"远程连接开关: enabled={enabled}")

        elif event == "-SAVE-":
            config["name"] = (values.get("-NAME-") or "").strip() or None
            save_config(config)
            log("设置已保存")

        elif event == "打开日志":
            try:
                if platform.system() == "Windows":
                    os.startfile(LOG_FILE)
                elif platform.system() == "Darwin":
                    subprocess.run(["open", LOG_FILE])
                else:
                    subprocess.run(["xdg-open", LOG_FILE])
            except Exception as e:
                log(f"打开日志失败: {e}")

        if time.time() - last_beat >= 2:
            if client.connected and client.authenticated:
                window["-LAST_BEAT-"].update(time.strftime("%H:%M:%S"))
            last_beat = time.time()

        # 心跳判活兜底：已认证但太久没收到服务端任何消息，说明连接已经死了
        # （TCP 半开、中间设备悄悄断了但本地 socket 没收到 FIN/RST），不能让
        # GUI 一直显示"已连接"。主动断开，交给 on_close 走统一的自动重连流程。
        if client.authenticated and client.last_server_seen is not None \
                and time.time() - client.last_server_seen > HEARTBEAT_STALE_SECS:
            log(f"心跳超时（{HEARTBEAT_STALE_SECS}秒未收到服务端消息），判定连接已死")
            if client.ws:
                client.ws.close()

    client.stop()
    window.close()
    log("Agent 退出")


# ─────────────────────────────────────────────────────────────────────────
#  USB 灯光控制（MIDI / DMX）—— 供远程通过 shell 命令调用，不进 GUI 主循环
#
#  不走 WebSocket 新协议：远程侧（Claude Code 的 pc_exec MCP 工具，或 Telegram
#  /pc_cmd）本来就能把任意 shell 命令送到这台 PC 执行（agent.py 已有的
#  execute_command() 用 subprocess.run 跑），这里只是把同一个 exe 变成一个
#  "被调用时执行完就退出"的 CLI 工具，复用现成的命令通道，不新增协议。
#
#  调用方式（同一个 agent.exe，用子命令分流）：
#    agent.exe light list
#    agent.exe light midi --device 金刚台 --type note_on --note 60 --velocity 100
#    agent.exe light dmx  --device 白色台子 --set 1=255 --set 2=128
# ─────────────────────────────────────────────────────────────────────────

DMX_CHANNELS = 512


def get_app_dir():
    """打包成 exe 后用 exe 所在目录；直接跑 .py 时用脚本所在目录——
    不能用 Path.cwd()，双击启动时工作目录不一定是安装目录。"""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


LIGHT_DEVICES_FILE = get_app_dir() / "light_devices.json"
LIGHT_DMX_STATE_FILE = get_app_dir() / "light_dmx_state.json"

# 声光同步网页（/lightsync 远程控制端）编辑的"灯光库"——设备白话定义 + 素材说明，
# 喂给 Claude Code 编灯用。这台 PC 是真实数据源（不是原来的 light_library_memory.json
# 那个平文件，云端 lib/db.js 的 light_library_cache 只是这份数据成功写入后的只读镜像，
# 见 lib/miniapp.js 的 /api/light/save-library）。跟 exe 同目录，单例 SQLite 文件。
LIGHT_LIBRARY_DB_FILE = get_app_dir() / "light_library.db"


def _light_library_db():
    conn = sqlite3.connect(str(LIGHT_LIBRARY_DB_FILE))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS light_library (
            id                      INTEGER PRIMARY KEY CHECK (id = 1),
            generated_at            TEXT,
            prompt_for_claude_code  TEXT,
            venue_fixtures_json     TEXT NOT NULL DEFAULT '[]',
            updated_at              TEXT NOT NULL
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS light_reference_media (
            slot        TEXT PRIMARY KEY,
            label       TEXT,
            kind        TEXT,
            file_name   TEXT,
            data        BLOB,
            note        TEXT,
            updated_at  TEXT
        )
    """)
    return conn


def _data_url_to_bytes(data_url):
    """'data:image/jpeg;base64,xxxx' -> 原始字节。不是 data URL（没有逗号）就返回 None。"""
    if not data_url or "," not in data_url:
        return None
    return base64.b64decode(data_url.split(",", 1)[1])


def _bytes_to_data_url(raw, kind):
    if raw is None:
        return None
    mime = "video/mp4" if kind == "video" else "image/jpeg"
    return f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")


def read_light_memory():
    """返回形状特意保持跟原来读文件版一样：{exists, content, error}——
    这样 agent-server.js 的 readMemory、pc-mcp-server.js 的 formatMemoryNote() 都不用改。"""
    try:
        conn = _light_library_db()
        try:
            row = conn.execute(
                "SELECT generated_at, prompt_for_claude_code, venue_fixtures_json FROM light_library WHERE id = 1"
            ).fetchone()
            if not row:
                return {"exists": False, "content": None, "error": None}

            media = {}
            for slot, label, kind, file_name, data, note in conn.execute(
                "SELECT slot, label, kind, file_name, data, note FROM light_reference_media"
            ):
                media[slot] = {
                    "label": label,
                    "kind": kind,
                    "fileName": file_name,
                    "dataUrl": _bytes_to_data_url(data, kind) if kind == "image" else None,
                    "note": note,
                }
            for slot in ("front", "left", "right", "frontWide", "video30s"):
                media.setdefault(slot, None)

            content = {
                "generatedAt": row[0],
                "promptForClaudeCode": row[1],
                "venueFixtures": json.loads(row[2] or "[]"),
                "venueReferenceMedia": media,
            }
            return {"exists": True, "content": content, "error": None}
        finally:
            conn.close()
    except Exception as e:
        return {"exists": False, "content": None, "error": str(e)}


def write_light_memory(content):
    try:
        content = content or {}
        conn = _light_library_db()
        try:
            now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            conn.execute(
                "INSERT INTO light_library (id, generated_at, prompt_for_claude_code, venue_fixtures_json, updated_at) "
                "VALUES (1, ?, ?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET generated_at=excluded.generated_at, "
                "  prompt_for_claude_code=excluded.prompt_for_claude_code, "
                "  venue_fixtures_json=excluded.venue_fixtures_json, updated_at=excluded.updated_at",
                (
                    content.get("generatedAt"),
                    content.get("promptForClaudeCode"),
                    json.dumps(content.get("venueFixtures", []), ensure_ascii=False),
                    now,
                ),
            )

            media = content.get("venueReferenceMedia") or {}
            for slot in ("front", "left", "right", "frontWide", "video30s"):
                asset = media.get(slot)
                if not asset:
                    conn.execute("DELETE FROM light_reference_media WHERE slot = ?", (slot,))
                    continue
                raw = _data_url_to_bytes(asset.get("dataUrl")) if asset.get("kind") == "image" else None
                conn.execute(
                    "INSERT INTO light_reference_media (slot, label, kind, file_name, data, note, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(slot) DO UPDATE SET label=excluded.label, kind=excluded.kind, "
                    "  file_name=excluded.file_name, data=excluded.data, note=excluded.note, updated_at=excluded.updated_at",
                    (slot, asset.get("label"), asset.get("kind"), asset.get("fileName"), raw, asset.get("note"), now),
                )
            conn.commit()
            return {"ok": True, "error": None}
        finally:
            conn.close()
    except Exception as e:
        return {"ok": False, "error": str(e)}


def load_light_devices():
    """读取设备别名映射表（跟 exe 同目录，方便现场手动编辑）：
    {"金刚台": {"type":"midi","port_name":"USB MIDI Device"},
     "白色台子": {"type":"dmx","com_port":"COM3","baudrate":57600}}
    文件不存在时返回空表——先跑 `light list` 现场核对可用设备，再手填这个文件。"""
    try:
        if LIGHT_DEVICES_FILE.exists():
            with open(LIGHT_DEVICES_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception as e:
        log(f"读取 light_devices.json 失败: {e}")
    return {}


def light_list_devices():
    """列出系统当前识别到的 MIDI 端口和串口设备，供现场核对、填写别名表。"""
    result = {"midi_ports": [], "serial_ports": [], "configured_aliases": load_light_devices()}
    try:
        import rtmidi
        midiout = rtmidi.MidiOut()
        result["midi_ports"] = midiout.get_ports()
    except Exception as e:
        result["midi_error"] = str(e)
    try:
        import serial.tools.list_ports
        result["serial_ports"] = [
            {"device": p.device, "description": p.description}
            for p in serial.tools.list_ports.comports()
        ]
    except Exception as e:
        result["serial_error"] = str(e)
    return result


def send_midi_message(device_arg, msg_type, channel, note=None, velocity=100, controller=None, value=None):
    """device_arg：light_devices.json 里的别名（type 必须是 midi），或直接的端口名子串/端口索引。"""
    import rtmidi

    devices = load_light_devices()
    entry = devices.get(device_arg)
    port_name = entry["port_name"] if entry and entry.get("type") == "midi" else device_arg

    midiout = rtmidi.MidiOut()
    ports = midiout.get_ports()
    idx = None
    if port_name.isdigit():
        idx = int(port_name)
    else:
        for i, p in enumerate(ports):
            if port_name in p:
                idx = i
                break
    if idx is None or idx >= len(ports):
        raise RuntimeError(f"未找到 MIDI 端口: {port_name}（当前可用: {ports}）")

    midiout.open_port(idx)
    try:
        channel = channel & 0x0F
        if msg_type == "note_on":
            if note is None:
                raise ValueError("note_on 需要 --note")
            midiout.send_message([0x90 | channel, note & 0x7F, (velocity or 0) & 0x7F])
        elif msg_type == "note_off":
            if note is None:
                raise ValueError("note_off 需要 --note")
            midiout.send_message([0x80 | channel, note & 0x7F, 0])
        elif msg_type == "cc":
            if controller is None or value is None:
                raise ValueError("cc 需要 --controller 和 --value")
            midiout.send_message([0xB0 | channel, controller & 0x7F, value & 0x7F])
        else:
            raise ValueError(f"未知 MIDI 消息类型: {msg_type}")
    finally:
        midiout.close_port()
    return {"port": ports[idx]}


def _load_dmx_frame():
    try:
        if LIGHT_DMX_STATE_FILE.exists():
            with open(LIGHT_DMX_STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            frame = bytearray(DMX_CHANNELS)
            for i, v in enumerate(data.get("frame", [])[:DMX_CHANNELS]):
                frame[i] = v & 0xFF
            return frame
    except Exception as e:
        log(f"读取 DMX 状态失败: {e}")
    return bytearray(DMX_CHANNELS)


def _save_dmx_frame(frame):
    try:
        with open(LIGHT_DMX_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"frame": list(frame)}, f)
    except Exception as e:
        log(f"保存 DMX 状态失败: {e}")


def send_dmx_frame(device_arg, channel_updates):
    """device_arg：light_devices.json 里的别名（type 必须是 dmx），或直接的 COM 口。
    Enttec DMX USB Pro 协议（USB-DMX 适配器最常见的协议）：
      0x7E, label=6(Output Only Send DMX Packet), len_lo, len_hi,
      [0x00 start-code, ch1..ch512], 0xE7
    DMX 是全量帧协议——只改一两个通道也要发满 512 字节，所以本地维护一份帧状态文件，
    每次只更新其中几个通道后仍整帧发送。
    波特率因适配器而异，常见 57600，若现场设备无响应就在 light_devices.json 里加
    "baudrate" 字段试其他值（比如 250000）——这个值没法在没有实体设备的情况下确定。"""
    import serial

    devices = load_light_devices()
    entry = devices.get(device_arg)
    if entry and entry.get("type") == "dmx":
        com_port = entry["com_port"]
        baudrate = entry.get("baudrate", 57600)
    else:
        com_port = device_arg
        baudrate = 57600

    frame = _load_dmx_frame()
    for ch, val in channel_updates.items():
        ch = int(ch)
        if not (1 <= ch <= DMX_CHANNELS):
            raise ValueError(f"DMX 通道号超出范围(1-512): {ch}")
        frame[ch - 1] = int(val) & 0xFF
    _save_dmx_frame(frame)

    payload = bytes([0]) + bytes(frame)  # 0x00 start code + 512 通道
    length = len(payload)
    packet = bytes([0x7E, 6, length & 0xFF, (length >> 8) & 0xFF]) + payload + bytes([0xE7])

    with serial.Serial(com_port, baudrate=baudrate, timeout=2) as ser:
        ser.write(packet)
    return {"port": com_port, "baudrate": baudrate, "channels_updated": len(channel_updates)}


def run_light_cli(argv):
    """`agent.exe light ...` 的入口。返回进程退出码。"""
    import argparse

    parser = argparse.ArgumentParser(prog="agent.exe light", add_help=True)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list", help="列出可用的 MIDI 端口和串口设备")

    p_midi = sub.add_parser("midi", help="发送 MIDI 消息")
    p_midi.add_argument("--device", required=True, help="light_devices.json 别名，或 MIDI 端口名子串/索引")
    p_midi.add_argument("--type", required=True, choices=["note_on", "note_off", "cc"])
    p_midi.add_argument("--channel", type=int, default=0)
    p_midi.add_argument("--note", type=int)
    p_midi.add_argument("--velocity", type=int, default=100)
    p_midi.add_argument("--controller", type=int)
    p_midi.add_argument("--value", type=int)

    p_dmx = sub.add_parser("dmx", help="发送 DMX 数据（Enttec DMX USB Pro 协议）")
    p_dmx.add_argument("--device", required=True, help="light_devices.json 别名，或 COM 口")
    p_dmx.add_argument("--set", action="append", required=True, metavar="CH=VAL",
                        help="通道=数值，可重复，如 --set 1=255 --set 2=128")

    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 2

    try:
        if args.cmd == "list":
            result = {"success": True, **light_list_devices()}
        elif args.cmd == "midi":
            info = send_midi_message(args.device, args.type, args.channel,
                                      note=args.note, velocity=args.velocity,
                                      controller=args.controller, value=args.value)
            result = {"success": True, **info}
        elif args.cmd == "dmx":
            updates = {}
            for item in args.set:
                if "=" not in item:
                    raise ValueError(f"--set 参数格式应为 通道=数值，收到: {item}")
                ch, val = item.split("=", 1)
                updates[int(ch)] = int(val)
            info = send_dmx_frame(args.device, updates)
            result = {"success": True, **info}
        else:
            result = {"success": False, "error": f"未知子命令: {args.cmd}"}
    except Exception as e:
        result = {"success": False, "error": str(e)}

    try:
        print(json.dumps(result, ensure_ascii=False))
    except Exception:
        pass  # 防御：极端情况下 stdout 不可写也不影响退出码
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "light":
        sys.exit(run_light_cli(sys.argv[2:]))
    else:
        main()
