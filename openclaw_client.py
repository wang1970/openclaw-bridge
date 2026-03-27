"""OpenClaw 客户端（WebSocket 主通道 + CLI 备用）

V5：基于 Gateway WebSocket 协议，Ed25519 设备签名认证，
     chat.send 发送消息，流式接收 Agent 响应。
"""

import asyncio
import base64
import hashlib
import json
import logging
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid

from config import (OPENCLAW_SESSION, OPENCLAW_SESSION_KEY, OPENCLAW_TIMEOUT,
                    OPENCLAW_TOKEN, OPENCLAW_URL, VOICE_BRIDGE_TAG)

logger = logging.getLogger(__name__)

# ── 设备密钥 ──────────────────────────────────────────────────

DEVICE_KEY_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".device_key.json")

try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    _CRYPTO_AVAILABLE = True
except ImportError:
    _CRYPTO_AVAILABLE = False

try:
    import websockets
    _WS_AVAILABLE = True
except ImportError:
    _WS_AVAILABLE = False


def _gen_id():
    return uuid.uuid4().hex[:12]


def _load_or_create_device_key():
    """加载或创建 Ed25519 设备密钥对（持久化到文件）"""
    if os.path.exists(DEVICE_KEY_FILE):
        with open(DEVICE_KEY_FILE, "r") as f:
            data = json.load(f)
        private_key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(data["private_key"])
        )
        return {
            "device_id": data["device_id"],
            "private_key": private_key,
            "public_key": data["public_key"],
        }

    private_key = Ed25519PrivateKey.generate()
    public_key_bytes = private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_key_bytes = private_key.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )

    # deviceId = hex(SHA-256(publicKey))
    device_id = hashlib.sha256(public_key_bytes).hexdigest()
    data = {
        "device_id": device_id,
        "public_key": base64.b64encode(public_key_bytes).decode(),
        "private_key": base64.b64encode(private_key_bytes).decode(),
    }
    with open(DEVICE_KEY_FILE, "w") as f:
        json.dump(data, f, indent=2)

    return {
        "device_id": device_id,
        "private_key": private_key,
        "public_key": data["public_key"],
    }


def _build_connect_payload(device, token, nonce):
    """构建 connect 请求参数（含 Ed25519 签名）"""
    signed_at_ms = int(time.time() * 1000)
    client_id = "clawdbot-control-ui"
    client_mode = "webchat"

    # 签名内容
    parts = [
        "v2", device["device_id"], client_id, client_mode,
        "operator", "operator.admin,operator.approvals,operator.pairing",
        str(signed_at_ms), token or "", nonce or "",
    ]
    message = "|".join(parts)
    signature_bytes = device["private_key"].sign(message.encode("utf-8"))
    signature_b64 = base64.b64encode(signature_bytes).decode()

    return {
        "minProtocol": 3,
        "maxProtocol": 3,
        "client": {
            "id": client_id,
            "version": "0.5.0",
            "platform": "windows",
            "mode": client_mode,
        },
        "role": "operator",
        "scopes": ["operator.admin", "operator.approvals", "operator.pairing"],
        "auth": {"token": token},
        "device": {
            "id": device["device_id"],
            "publicKey": device["public_key"],
            "signature": signature_b64,
            "signedAt": signed_at_ms,
            "nonce": nonce,
        },
        "userAgent": "openclaw-bridge/1.0.0",
        "locale": "zh-CN",
    }


# ── CLI 备用通道 ──────────────────────────────────────────────


def _cli_available() -> bool:
    return shutil.which("openclaw") is not None


def _cli_send(text: str, on_response=None) -> bool:
    tagged_text = f"{VOICE_BRIDGE_TAG}{text}" if VOICE_BRIDGE_TAG else text
    try:
        result = subprocess.run(
            f'openclaw agent --session-id "{OPENCLAW_SESSION}" --message "{tagged_text}" --json',
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, shell=True,
        )
        if result.returncode == 0:
            logger.info(f"[发送] {tagged_text[:80]}")
            reply = _parse_cli_reply(result.stdout)
            if reply:
                logger.info(f"[Agent] {reply[:200]}")
                if on_response:
                    on_response(reply)
            return True
        else:
            logger.error(f"[CLI] 发送失败 (code={result.returncode}): {result.stderr.strip()[:200]}")
            return False
    except Exception as e:
        logger.error(f"[CLI] 异常: {e}")
        return False


def _parse_cli_reply(stdout: str) -> str:
    if not stdout or not stdout.strip():
        return ""
    try:
        resp = json.loads(stdout)
        payloads = resp.get("result", {}).get("payloads", [])
        if payloads:
            return payloads[0].get("text", "")
    except json.JSONDecodeError:
        return stdout.strip()[:200]
    return ""


# ── OpenClawClient（WebSocket 主通道）─────────────────────────


class OpenClawClient:
    """
    V5：WebSocket 主通道 + CLI 备用。

    WebSocket 通道：基于 Gateway 协议，Ed25519 签名认证，
                    chat.send 发消息，流式接收 Agent 响应。
    CLI 通道：当 WebSocket 不可用时自动降级。
    """

    def __init__(self, url: str = OPENCLAW_URL, timeout: int = OPENCLAW_TIMEOUT,
                 on_response=None):
        self.timeout = timeout
        self.session = OPENCLAW_SESSION
        self.token = OPENCLAW_TOKEN
        self.running = True
        self.authenticated = False
        self.warmed_up = False
        self.on_response = on_response  # fn(reply_text)

        # 发送队列
        self.send_queue: queue.Queue = queue.Queue()

        # WebSocket 模式
        self._use_ws = _WS_AVAILABLE and _CRYPTO_AVAILABLE
        if self._use_ws:
            raw_url = url.rstrip("/")
            self.ws_url = raw_url.replace("http://", "ws://").replace("https://", "wss://")
            self.device = _load_or_create_device_key()
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(target=self._ws_thread, daemon=True, name="openclaw-ws")
            self._thread.start()
            logger.debug(f"使用 WebSocket 模式, deviceId={self.device['device_id'][:16]}...")
        else:
            reasons = []
            if not _WS_AVAILABLE:
                reasons.append("websockets 未安装")
            if not _CRYPTO_AVAILABLE:
                reasons.append("cryptography 未安装")
            logger.warning("WebSocket 不可用: " + "; ".join(reasons) + "，降级为 CLI 模式")
            self._use_ws = False
            if _cli_available():
                threading.Thread(target=self._cli_warmup, daemon=True, name="openclaw-warmup").start()

    # ── WebSocket 后台线程 ─────────────────────────────────

    def _ws_thread(self):
        """在独立线程中运行 asyncio 事件循环"""
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._ws_main())
        self._loop.close()

    async def _ws_main(self):
        """WebSocket 主循环：连接 → 认证 → 消息收发 → 自动重连"""
        while self.running:
            try:
                await self._ws_connect_and_run()
            except Exception as e:
                logger.error(f"WebSocket 异常: {e}")
            if not self.running:
                break
            logger.debug("WebSocket 断开，5 秒后重连...")
            self.authenticated = False
            await asyncio.sleep(5)

    async def _ws_connect_and_run(self):
        async with websockets.connect(self.ws_url) as ws:
            logger.debug("[WS] 已连接，等待 challenge...")
            self._ws = ws

            # ── 1. 等待 challenge ──
            challenge_data = await self._recv_json(ws, timeout=10)
            if not challenge_data:
                logger.error("[WS] 未收到 challenge")
                return
            nonce = challenge_data.get("payload", {}).get("nonce", "")
            logger.debug(f"[WS] challenge nonce={nonce[:12]}...")

            # ── 2. 发送 connect（含签名认证）──
            connect_payload = _build_connect_payload(self.device, self.token, nonce)
            req_id = _gen_id()
            await self._send_json(ws, {
                "type": "req", "id": req_id,
                "method": "connect", "params": connect_payload,
            })

            # ── 3. 等待认证结果 ──
            auth_result = await self._recv_response(ws, expect_id=req_id, timeout=15)
            if not auth_result or not auth_result.get("ok"):
                error = auth_result.get("error", "未知错误") if auth_result else "超时"
                logger.error(f"[WS] 认证失败: {error}")
                return

            hello = auth_result.get("payload", {})
            self.authenticated = True
            self.warmed_up = True
            logger.debug("[WS] 认证成功，Agent 就绪")

            # 预热完成通知
            if self.on_response:
                self.on_response("小龙虾就绪")

            # ── 4. 消息收发循环 ──
            await self._ws_message_loop(ws)

    async def _ws_message_loop(self, ws):
        """消息循环：消费发送队列 + 接收响应"""
        while self.running and self.authenticated:
            # 非阻塞消费发送队列
            sent = await self._flush_queue(ws)
            if not sent:
                # 队列空，等待接收（带超时以定期检查队列）
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.2)
                    self._handle_ws_message(raw)
                except asyncio.TimeoutError:
                    continue
                except websockets.ConnectionClosed:
                    logger.debug("[WS] 连接已关闭")
                    self.authenticated = False
                    return

    async def _flush_queue(self, ws) -> bool:
        """消费发送队列"""
        sent = False
        while not self.send_queue.empty():
            try:
                text = self.send_queue.get_nowait()
                tagged_text = f"{VOICE_BRIDGE_TAG}{text}" if VOICE_BRIDGE_TAG else text
                req_id = _gen_id()
                await self._send_json(ws, {
                    "type": "req", "id": req_id,
                    "method": "chat.send",
                    "params": {
                        "sessionKey": OPENCLAW_SESSION_KEY,
                        "message": tagged_text,
                        "idempotencyKey": _gen_id(),
                    },
                })
                logger.info(f"[发送] {tagged_text[:80]}")
                sent = True
            except queue.Empty:
                break
            except Exception as e:
                logger.error(f"[WS] 发送失败: {e}")
                break
        return sent

    def _handle_ws_message(self, raw: str):
        """处理接收到的 WebSocket 消息"""
        if not raw or not raw.strip():
            return
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return

        msg_type = data.get("type", "")

        if msg_type == "res":
            # chat.send 的确认响应（非流式文本，仅记录）
            ok = data.get("ok", False)
            if not ok:
                logger.error(f"[WS] chat.send 失败: {data.get('error')}")
            return

        if msg_type == "event":
            event = data.get("event", "")
            payload = data.get("payload", {})

            if event == "agent":
                stream = payload.get("stream", "")
                stream_data = payload.get("data", {})

                if stream == "assistant":
                    delta = stream_data.get("delta", "")
                    if delta:
                        current = stream_data.get("text", "")
                        if not hasattr(self, '_stream_text'):
                            self._stream_text = ""
                        if current and len(current) > len(self._stream_text):
                            self._stream_text = current
                        elif delta:
                            self._stream_text += delta

                elif stream == "end":
                    # 不在此回调，等 chat.final 统一输出（避免重复）
                    self._stream_text = ""

                elif stream == "lifecycle":
                    phase = stream_data.get("phase", "")
                    logger.debug(f"[WS] lifecycle: {phase}")

                elif stream == "tool":
                    logger.debug(f"[WS] tool: {json.dumps(stream_data, ensure_ascii=False)[:100]}")

            elif event == "chat":
                state = payload.get("state", "")
                msg = payload.get("message", {})
                content = msg.get("content", [])
                text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                if state == "final" and text:
                    # 过滤心跳等系统消息
                    if text.upper().startswith("HEARTBEAT"):
                        logger.debug(f"[WS] 跳过心跳消息: {text}")
                        return
                    logger.info(f"[Agent] {text[:200]}")
                    if self.on_response:
                        self.on_response(text)

            # 忽略其他事件（如 agent.list 等）

    # ── WebSocket 工具方法 ─────────────────────────────────

    async def _send_json(self, ws, data):
        await ws.send(json.dumps(data, ensure_ascii=False))

    async def _recv_json(self, ws, timeout=30):
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            return json.loads(raw)
        except (asyncio.TimeoutError, websockets.ConnectionClosed, json.JSONDecodeError):
            return None

    async def _recv_response(self, ws, expect_id=None, timeout=30):
        """接收消息，区分 res/event，支持等待特定 id"""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return None
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 5))
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed:
                return None

            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                continue

            msg_type = data.get("type", "")
            if msg_type == "res":
                res_id = data.get("id", "")
                if expect_id and res_id != expect_id:
                    continue
                return {
                    "ok": data.get("ok", False),
                    "payload": data.get("payload"),
                    "error": data.get("error"),
                }
            elif msg_type == "event":
                # challenge 等特殊事件也需要处理
                event = data.get("event", "")
                if event == "connect.challenge":
                    return {"type": "challenge", "payload": data.get("payload", {})}
                # 非目标事件，在事件处理中消费掉
                self._handle_ws_message(raw)

    # ── CLI 预热 ───────────────────────────────────────────

    def _cli_warmup(self):
        """CLI 模式的预热"""
        logger.info("正在预热 OpenClaw agent (CLI)...")
        if self.on_response:
            self.on_response("请稍候，小龙虾马上就来")
        try:
            result = subprocess.run(
                f'openclaw agent --session-id "{OPENCLAW_SESSION}" --message "init" --json',
                capture_output=True, text=True, encoding="utf-8",
                errors="replace", timeout=120, shell=True,
            )
            if result.returncode == 0:
                self.warmed_up = True
                logger.info("OpenClaw agent 预热完成")
                if self.on_response:
                    self.on_response("小龙虾就绪")
            else:
                logger.warning(f"预热失败: {result.stderr.strip()[:100]}")
        except Exception as e:
            logger.warning(f"预热异常: {e}")

    # ── 公开接口 ───────────────────────────────────────────

    def send_text(self, text: str) -> bool:
        """发送文本消息（非阻塞）"""
        if not text.strip():
            return False

        if self._use_ws:
            self.send_queue.put(text)
            return True

        # CLI 降级
        if not _cli_available():
            logger.error("CLI 不可用且 WebSocket 未就绪，消息丢弃")
            return False
        threading.Thread(
            target=_cli_send, args=(text, self.on_response), daemon=True, name="openclaw-cli"
        ).start()
        return True

    def stop(self):
        self.running = False
        if self._use_ws and self._loop and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    print(f"WS:   {'可用' if _WS_AVAILABLE else '不可用 (pip install websockets)'}")
    print(f"加密: {'可用' if _CRYPTO_AVAILABLE else '不可用 (pip install cryptography)'}")
    print(f"CLI:  {'可用' if _cli_available() else '不可用'}")

    def on_reply(text):
        print(f"\n[回复] {text}\n")

    client = OpenClawClient(on_response=on_reply)
    time.sleep(3)

    print("\n=== 发送测试消息 ===")
    client.send_text("你好，简短回复")
    time.sleep(15)

    client.stop()
    print("已退出")
