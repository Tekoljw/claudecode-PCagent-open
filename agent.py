#!/usr/bin/env python3
"""
PC 远程控制 Agent - 跨平台（Windows/macOS/Linux）
运行方式：python agent.py 或双击可执行文件

认证模型：验证码配对 + 设备专属令牌
- 首次使用：填入关联的 Bot Token，点击"获取验证码"，
  服务器把验证码推给 Telegram 里的管理员，1 小时内有效。
  把验证码填回这里完成配对，之后自动颁发本机专属的 device_token。
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
    # Agent 走 wss://<server_host>/agent 连接服务器。
    "server_host": "claudbotjs.doez.ai",
    "server_port": 443,
    "bot_token": "",
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
            self._cb("on_status_change", "连接失败", False)
            self.reconnect()

    def on_open(self, ws):
        self.connected = True
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

    def request_pairing_code(self, bot_token):
        """GUI 点击"获取验证码"时调用"""
        if not self.ws or not self.connected:
            return False
        self.ws.send(json.dumps({
            "type": "request_pairing_code",
            "mac_address": self.mac_address,
            "hostname": self.hostname,
            "os": self.os,
            "bot_token": bot_token or "",
        }))
        log(f"已请求配对验证码 (bot_token={'已填写' if bot_token else '未填写'})")
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

    def stop(self):
        self.config["enabled"] = False
        if self.ws:
            self.ws.close()


# ─────────────────────────────────────────────────────────────────────────
#  GUI
# ─────────────────────────────────────────────────────────────────────────

def create_gui(config):
    sg.theme("DarkBlue3")
    has_token = bool(config.get("device_token"))

    pairing_layout = [
        [sg.Text("首次使用需要配对", font=("Arial", 10, "bold"))],
        [sg.Text("关联 Bot Token:"), sg.InputText(
            config.get("bot_token", ""), key="-BOT_TOKEN-", size=(32, 1),
            tooltip="填入 Telegram Bot Token；验证码会通过它推给管理员")],
        [sg.Button("获取验证码", key="-GET_CODE-")],
        [
            sg.Text("验证码:", key="-PAIR_CODE_LABEL-", visible=False),
            sg.InputText("", key="-PAIR_CODE-", size=(15, 1), visible=False),
            sg.Button("验证", key="-VERIFY_CODE-", visible=False),
        ],
        [sg.Text("", key="-PAIR_STATUS-", text_color="orange", size=(45, 2))],
    ]

    main_layout = [
        [sg.Text("PC 名称:"), sg.Text("", key="-PC_NAME-", size=(30, 1))],
        [sg.Text("状态:"), sg.Text("", key="-STATUS-", size=(30, 1), text_color="red")],
        [sg.Text("最后心跳:"), sg.Text("", key="-LAST_BEAT-", size=(30, 1))],
        [sg.Checkbox("启用远程访问", default=config.get("enabled", True), key="-ENABLED-")],
        [sg.Button("解除配对", key="-UNPAIR-", tooltip="清除本机令牌，需要重新走验证码配对")],
    ]

    layout = [
        [sg.Text("Agent 远程控制客户端", font=("Arial", 14, "bold"))],
        [sg.Separator()],
        [sg.Text("PC ID:"), sg.Text(get_mac_address(), size=(30, 1))],
        [sg.Separator()],
        [sg.Column(pairing_layout, key="-PAIRING_COL-", visible=not has_token)],
        [sg.Column(main_layout, key="-MAIN_COL-", visible=has_token)],
        [sg.Separator()],
        [sg.Button("保存设置", key="-SAVE-"), sg.Button("打开日志"), sg.Button("退出")],
    ]

    return sg.Window("Agent", layout, finalize=True, keep_on_top=False)


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
            bot_token = values["-BOT_TOKEN-"].strip()
            config["bot_token"] = bot_token
            save_config(config)
            if client.request_pairing_code(bot_token):
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
            window["-STATUS-"].update(status, text_color="green" if connected else "red")

        elif event == "-EVT_PCINFO-":
            window["-PC_NAME-"].update(values[event])

        elif event == "-SAVE-":
            config["enabled"] = values["-ENABLED-"]
            save_config(config)
            client.config = config
            log(f"设置已保存: enabled={config['enabled']}")

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


if __name__ == "__main__":
    main()
