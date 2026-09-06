#!/usr/bin/env python3
"""
PC 远程控制 Agent - 跨平台（Windows/macOS/Linux）
运行方式：python agent.py 或双击可执行文件

认证模型：验证码配对 + 设备专属令牌
- 首次使用：点击"获取验证码"，服务器把验证码推给 Telegram 里的管理员，
  1 小时内有效。把验证码填回这里完成配对，之后自动颁发本机专属的
  device_token。配对成功后由哪个 Bot 管理这台设备，是服务端的管理员
  决定的事（在 /pc_list 里绑定），客户端无需也无法指定。
- 之后每次启动：用本地保存的 device_token 自动认证，无需人工操作。
"""

import sys
import json
import threading
import time
import socket
import os
import subprocess
import platform
from pathlib import Path
import websocket

try:
    # 用 FreeSimpleGUI 而非 PySimpleGUI：后者自 5.x 起转为商业授权模式，
    # 4.60.5（此前锁定的版本）已从 PyPI 撤下装不了，且新版首次运行会弹出
    # 注册/许可证对话框，破坏"双击就能用"。FreeSimpleGUI 是社区 fork，
    # 保持 4.x 最后一个开源版本的 LGPL 协议，API 完全兼容，一行 import 切换。
    import FreeSimpleGUI as sg
except ImportError:
    print("需要安装依赖: pip install FreeSimpleGUI websocket-client")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────────────────
#  配置
# ─────────────────────────────────────────────────────────────────────────

CONFIG_DIR = Path.home() / ".claude-agent"
CONFIG_FILE = CONFIG_DIR / "config.json"
LOG_FILE = CONFIG_DIR / "agent.log"

DEFAULT_CONFIG = {
    # 复用 claude-bot 现有的 CloudFront 域名和 Mini App 路径，
    # Agent 走 wss://claudbotjs.doez.ai/agent，不需要单独的域名/证书/端口。
    # （旧域名 claudebot.bc361.com 过渡期内仍保留，见 CLAUDE.md）
    "server_host": "claudbotjs.doez.ai",
    "server_port": 443,
    "device_token": None,  # 配对成功后颁发，本机专属，不要跟其他设备共用
    "enabled": True,
}

PAIRING_FAIL_REASONS = {
    "not_found": "验证码不存在",
    "expired": "验证码已过期，请重新获取",
    "used": "验证码已被使用，请重新获取",
    "mac_mismatch": "验证码错误",
    "too_many_attempts": "错误次数过多，请重新获取验证码",
    "error": "服务器内部错误",
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


def get_mac_address():
    """获取 MAC 地址"""
    import uuid
    mac = uuid.getnode()
    mac_str = ':'.join(("%012X" % mac)[i:i+2] for i in range(0, 12, 2))
    return mac_str


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


# ─────────────────────────────────────────────────────────────────────────
#  WebSocket 客户端
# ─────────────────────────────────────────────────────────────────────────

class AgentClient:
    def __init__(self, config, callbacks):
        self.config = config
        self.ws = None
        self.connected = False       # WebSocket 传输层是否连通
        self.authenticated = False   # 应用层是否已认证/配对完成，可以正常工作
        self.mac_address = get_mac_address()
        self.hostname = get_hostname()
        self.os = get_os_name()
        self.pc_name = None
        self.callbacks = callbacks or {}
        self.reconnect_count = 0
        self.max_reconnect = 5
        self._connecting = False  # 防止开关被连续点击时启动重复的连接线程

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

    def connect(self):
        """建立连接。已配对（有 device_token）则自动认证；未配对则只连接，等待 GUI 操作"""
        if not self.config.get("enabled"):
            log("Agent 已禁用，跳过连接")
            return

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
            ws.run_forever()
        except Exception as e:
            log(f"连接失败: {e}")
            self.connected = False
            self._connecting = False
            self._cb("on_status_change", "连接失败", False)
            self.reconnect()

    def on_open(self, ws):
        self.connected = True
        self._connecting = False
        self.reconnect_count = 0
        log("WebSocket 连接打开")

        device_token = self.config.get("device_token")
        if device_token:
            ws.send(json.dumps({
                "type": "auth",
                "mac_address": self.mac_address,
                "hostname": self.hostname,
                "os": self.os,
                "device_token": device_token,
            }))
            log("已发送 auth（使用已保存的 device_token）")
        else:
            log("尚未配对，等待用户在 GUI 里获取验证码")
            self._cb("on_status_change", "未配对", False)

    def request_pairing_code(self):
        """GUI 点击"获取验证码"时调用"""
        if not self.ws or not self.connected:
            return False
        self.ws.send(json.dumps({
            "type": "request_pairing_code",
            "mac_address": self.mac_address,
            "hostname": self.hostname,
            "os": self.os,
        }))
        log("已请求配对验证码")
        return True

    def submit_pairing_code(self, code):
        """GUI 输入验证码点击"验证"时调用"""
        if not self.ws or not self.connected:
            return False
        self.ws.send(json.dumps({
            "type": "verify_pairing_code",
            "mac_address": self.mac_address,
            "code": code,
        }))
        log("已提交验证码")
        return True

    def on_message(self, ws, message):
        try:
            data = json.loads(message)
            msg_type = data.get("type")

            if msg_type == "pairing_code_sent":
                log("验证码已发送到 Telegram")
                self._cb("on_pairing_code_sent")

            elif msg_type == "pairing_success":
                device_token = data.get("device_token")
                self.config["device_token"] = device_token
                save_config(self.config)
                log("配对成功，device_token 已保存到本地")
                self._cb("on_pairing_success")

            elif msg_type == "pairing_failed":
                reason = data.get("reason", "error")
                log(f"配对失败: {reason}")
                self._cb("on_pairing_failed", reason)

            elif msg_type == "pc_info":
                self.pc_name = data.get("name", "未命名")
                self.authenticated = True
                self._cb("on_pc_info", self.pc_name)
                self._cb("on_status_change", "已连接", True)
                log(f"PC 信息已更新: {self.pc_name}")

            elif msg_type == "command":
                cmd_id = data.get("id")
                cmd = data.get("command")
                log(f"执行命令: {cmd}")
                result = self.execute_command(cmd)
                ws.send(json.dumps({
                    "type": "command_result",
                    "id": cmd_id,
                    "status": "success" if result["success"] else "failed",
                    "output": result["output"],
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
        self._cb("on_status_change", "已断开", False)
        if close_status_code == 4001:
            # 认证失败：device_token 被吊销或错误，清空本地令牌，转回配对界面
            log("认证被拒绝，可能已被吊销，清空本地配对信息")
            self.config["device_token"] = None
            save_config(self.config)
            self._cb("on_auth_revoked")
            return
        self.reconnect()

    def execute_command(self, cmd):
        try:
            result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
            return {"success": result.returncode == 0, "output": result.stdout + result.stderr}
        except subprocess.TimeoutExpired:
            return {"success": False, "output": "命令执行超时"}
        except Exception as e:
            return {"success": False, "output": str(e)}

    def reconnect(self):
        if not self.config.get("enabled"):
            return
        if self.reconnect_count < self.max_reconnect:
            self.reconnect_count += 1
            wait_time = min(2 ** self.reconnect_count, 60)
            log(f"将在 {wait_time} 秒后重连（第 {self.reconnect_count} 次）...")
            time.sleep(wait_time)
            self.connect()
        else:
            log("重连失败次数过多，停止重试")

    def set_enabled(self, enabled):
        """开关立即生效：关闭时断开连接，开启时拉起新的连接线程。
        不能只写配置——之前的实现只在点"保存设置"时更新 config，开关本身不触发
        任何实际的连接/断开动作，用户勾掉复选框后连接依然挂着。"""
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
    has_token = bool(config.get("device_token"))

    pairing_layout = [
        [sg.Text("首次使用需要配对", font=FONT_BOLD, pad=((0, 0), (0, 10)))],
        [sg.Text("点击下方按钮获取验证码，管理员会在 Telegram 收到推送",
                  font=FONT_SUB, text_color="grey", pad=((0, 0), (0, 12)))],
        [sg.Button("获取验证码", key="-GET_CODE-", size=(14, 1))],
        [sg.Text("验证码", font=FONT_SUB, text_color="grey", key="-PAIR_CODE_LABEL-",
                  visible=False, pad=((0, 0), (14, 2)))],
        [
            sg.InputText("", key="-PAIR_CODE-", size=(16, 1), visible=False),
            sg.Button("验证", key="-VERIFY_CODE-", size=(8, 1), visible=False),
        ],
        [sg.Text("", key="-PAIR_STATUS-", font=FONT_SUB, text_color=ACCENT,
                  size=(45, 2), pad=((0, 0), (12, 0)))],
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
        [sg.HSeparator(pad=((0, 0), (14, 12)))],
        [sg.Checkbox("启用远程访问", default=config.get("enabled", True), key="-ENABLED-", enable_events=True)],
        [sg.Button("解除配对", key="-UNPAIR-",
                    tooltip="清除本机令牌，需要重新走验证码配对", pad=((0, 0), (12, 0)))],
    ]

    layout = [
        [sg.Text("🖥  Agent", font=FONT_TITLE)],
        [sg.Text("PC 远程控制客户端", font=FONT_SUB, text_color="grey")],
        [sg.HSeparator(pad=((0, 0), (14, 14)))],
        [sg.Text("设备 ID", font=FONT_SUB, text_color="grey", size=(10, 1)),
         sg.Push(), sg.Text(get_mac_address(), font=FONT_LABEL)],
        [sg.HSeparator(pad=((0, 0), (14, 14)))],
        [sg.Column(pairing_layout, key="-PAIRING_COL-", visible=not has_token)],
        [sg.Column(main_layout, key="-MAIN_COL-", visible=has_token)],
        [sg.HSeparator(pad=((0, 0), (18, 14)))],
        [sg.Button("保存设置", key="-SAVE-"), sg.Button("打开日志"),
         sg.Push(), sg.Button("退出", button_color=("white", "#B00020"))],
    ]

    return sg.Window("Agent", layout, finalize=True, keep_on_top=False,
                      margins=(24, 20), element_padding=(4, 4))


def main():
    log("Agent 启动")
    config = load_config()

    window = create_gui(config)

    # 后台线程的回调必须通过 write_event_value 转发到主循环，
    # 不能直接操作 GUI 控件（PySimpleGUI 不是线程安全的）。
    client = AgentClient(config, {
        "on_status_change": lambda status, connected: window.write_event_value("-EVT_STATUS-", (status, connected)),
        "on_pc_info": lambda name: window.write_event_value("-EVT_PCINFO-", name),
        "on_pairing_code_sent": lambda: window.write_event_value("-EVT_CODE_SENT-", None),
        "on_pairing_success": lambda: window.write_event_value("-EVT_PAIR_SUCCESS-", None),
        "on_pairing_failed": lambda reason: window.write_event_value("-EVT_PAIR_FAILED-", reason),
        "on_auth_revoked": lambda: window.write_event_value("-EVT_AUTH_REVOKED-", None),
    })

    threading.Thread(target=client.connect, daemon=True).start()

    last_beat = time.time()

    while True:
        event, values = window.read(timeout=1000)

        if event == sg.WINDOW_CLOSED or event == "退出":
            break

        if event == "-GET_CODE-":
            if client.request_pairing_code():
                window["-PAIR_STATUS-"].update("正在请求验证码…", text_color="orange")
            else:
                window["-PAIR_STATUS-"].update("尚未连接到服务器，请稍候重试", text_color="red")

        elif event == "-VERIFY_CODE-":
            code = values["-PAIR_CODE-"].strip()
            if code:
                client.submit_pairing_code(code)
                window["-PAIR_STATUS-"].update("正在验证…", text_color="orange")

        elif event == "-EVT_CODE_SENT-":
            window["-PAIR_CODE_LABEL-"].update(visible=True)
            window["-PAIR_CODE-"].update(visible=True)
            window["-VERIFY_CODE-"].update(visible=True)
            window["-PAIR_STATUS-"].update("验证码已发送，请在 Telegram 查收（1 小时内有效）", text_color="light green")

        elif event == "-EVT_PAIR_SUCCESS-":
            window["-PAIRING_COL-"].update(visible=False)
            window["-MAIN_COL-"].update(visible=True)
            log("配对成功，切换到主界面")

        elif event == "-EVT_PAIR_FAILED-":
            reason = values[event]
            window["-PAIR_STATUS-"].update(
                "配对失败: " + PAIRING_FAIL_REASONS.get(reason, reason), text_color="red")

        elif event == "-EVT_AUTH_REVOKED-":
            window["-MAIN_COL-"].update(visible=False)
            window["-PAIRING_COL-"].update(visible=True)
            window["-PAIR_STATUS-"].update("配对已被吊销，请重新获取验证码配对", text_color="red")

        elif event == "-EVT_STATUS-":
            status, connected = values[event]
            window["-STATUS-"].update(status)
            window["-STATUS_DOT-"].update(text_color="#43A047" if connected else "#E53935")

        elif event == "-EVT_PCINFO-":
            window["-PC_NAME-"].update(values[event])

        elif event == "-ENABLED-":
            enabled = values["-ENABLED-"]
            client.set_enabled(enabled)
            window["-STATUS-"].update("已禁用" if not enabled else "正在连接…")
            window["-STATUS_DOT-"].update(text_color="#E53935")
            log(f"远程访问开关: enabled={enabled}")

        elif event == "-SAVE-":
            save_config(config)
            log("设置已保存")

        elif event == "-UNPAIR-":
            config["device_token"] = None
            save_config(config)
            client.config = config
            client.authenticated = False
            if client.ws:
                client.ws.close()
            window["-MAIN_COL-"].update(visible=False)
            window["-PAIRING_COL-"].update(visible=True)
            window["-PAIR_STATUS-"].update("已解除配对，请重新获取验证码", text_color="orange")
            log("用户主动解除配对")

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
