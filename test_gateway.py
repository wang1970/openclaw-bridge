"""OpenClaw Gateway WebSocket 测试 V2

关键发现（前端 JS 反编译）：
- client.id 必须是 "clawdbot-control-ui"
- client.mode 必须是 "webchat"
- device 需要签名：Ed25519 签名 "v1|deviceId|clientId|clientMode|role|scopes|signedAtMs|token"
- 首次认证后缓存 deviceToken 可加速后续连接
"""

import asyncio
import base64
import hashlib
import json
import os
import time
import uuid

import websockets
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

GATEWAY_URL = "ws://127.0.0.1:18789"
TOKEN = "106d50240bfdb0a93666180c7acce7afc68181d5cb91c14e"
DEVICE_KEY_FILE = os.path.join(os.path.dirname(__file__), ".device_key.json")
DEVICE_ID = "openclaw-bridge"


def gen_id():
    return uuid.uuid4().hex[:12]


def load_or_create_device_key():
    """加载或创建设备密钥对（持久化到文件）"""
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

    # 创建新密钥对
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

    # deviceId = hex(SHA-256(publicKey))（前端 Mr 函数逻辑）
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


def sign_connect_payload(device_id, public_key, signed_at_ms, token, nonce=None):
    """
    生成签名。签名内容格式（从前端 Yu 函数）：
    v1|{deviceId}|{clientId}|{clientMode}|{role}|{scopes}|{signedAtMs}|{token}
    """
    version = "v2" if nonce else "v1"
    client_id = "clawdbot-control-ui"
    client_mode = "webchat"
    role = "operator"
    scopes = "operator.admin,operator.approvals,operator.pairing"

    parts = [version, device_id, client_id, client_mode, role, scopes, str(signed_at_ms), token or ""]
    if version == "v2":
        parts.append(nonce or "")

    message = "|".join(parts)
    return message


async def send_request(ws, method, params):
    req_id = gen_id()
    msg = {"type": "req", "id": req_id, "method": method, "params": params}
    await ws.send(json.dumps(msg, ensure_ascii=False))
    return req_id


async def recv_message(ws, timeout=30):
    """接收原始消息"""
    try:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        return json.loads(raw)
    except asyncio.TimeoutError:
        return None


async def recv_response(ws, expect_id=None, timeout=30):
    """接收消息，区分 res/event"""
    while True:
        data = await recv_message(ws, timeout)
        if data is None:
            return None

        msg_type = data.get("type", "")

        if msg_type == "event":
            event = data.get("event", "")
            payload = data.get("payload", {})
            if event == "connect.challenge":
                return {"type": "challenge", "nonce": payload.get("nonce")}
            # 有 expect_id 时跳过非目标事件，只等待 res
            if expect_id:
                continue
            return {"type": "event", "event": event, "payload": payload}

        elif msg_type == "res":
            res_id = data.get("id", "")
            if expect_id and res_id != expect_id:
                print(f"[跳过] 非预期响应 id={res_id}")
                continue
            return {
                "type": "res",
                "ok": data.get("ok", False),
                "payload": data.get("payload"),
                "error": data.get("error"),
            }

        else:
            print(f"[未知] {json.dumps(data)[:200]}")


async def main():
    device = load_or_create_device_key()
    print(f"设备 ID: {device['device_id']}")

    ws = await websockets.connect(GATEWAY_URL)
    print("[1] WS 已连接，等待 challenge...")

    result = await recv_response(ws)
    if not result or result["type"] != "challenge":
        print(f"[失败] 未收到 challenge: {result}")
        return

    nonce = result["nonce"]
    print(f"[2] 收到 challenge, nonce={nonce}")

    signed_at_ms = int(time.time() * 1000)
    message = sign_connect_payload(device["device_id"], device["public_key"], signed_at_ms, TOKEN, nonce)

    signature_bytes = device["private_key"].sign(message.encode("utf-8"))
    signature_b64 = base64.b64encode(signature_bytes).decode()

    req_id = await send_request(ws, "connect", {
        "minProtocol": 3,
        "maxProtocol": 3,
        "client": {
            "id": "clawdbot-control-ui",
            "version": "0.4.0",
            "platform": "windows",
            "mode": "webchat",
        },
        "role": "operator",
        "scopes": ["operator.admin", "operator.approvals", "operator.pairing"],
        "auth": {"token": TOKEN},
        "device": {
            "id": device["device_id"],
            "publicKey": device["public_key"],
            "signature": signature_b64,
            "signedAt": signed_at_ms,
            "nonce": nonce,
        },
        "userAgent": "openclaw-bridge/1.0.0",
        "locale": "zh-CN",
    })

    result = await recv_response(ws, expect_id=req_id)
    if not result or not result["ok"]:
        print(f"[失败] 认证失败: {result}")
        return

    hello = result["payload"]
    device_token = hello.get("auth", {}).get("deviceToken")
    print(f"[3] 认证成功! protocol={hello.get('protocol')}, deviceToken={device_token}")

    # 测试 chat.send
    print("\n" + "=" * 50)
    print("测试 2: chat.send")
    print("=" * 50)

    req_id = await send_request(ws, "chat.send", {
        "sessionKey": "agent:main:main",
        "message": "你好，简短回复",
        "idempotencyKey": gen_id(),
    })
    print(f"[1] 已发送 chat.send")

    result = await recv_response(ws, expect_id=req_id, timeout=30)
    if result:
        if result["ok"]:
            print(f"[2] chat.send 成功: {json.dumps(result['payload'], ensure_ascii=False)[:300]}")
        else:
            print(f"[2] chat.send 失败: {result['error']}")

    print("[3] 等待 Agent 响应 (20s)...")
    chat_count = 0
    full_text = ""
    try:
        for _ in range(200):
            result = await recv_response(ws, timeout=20)
            if result is None:
                break
            if result.get("type") == "event":
                event = result.get("event", "")
                payload = result.get("payload", {})
                if event == "agent":
                    stream = payload.get("stream", "")
                    data = payload.get("data", {})
                    run_id = payload.get("runId", "")
                    if stream == "lifecycle":
                        phase = data.get("phase", "")
                        print(f"  [lifecycle] phase={phase}")
                    elif stream == "assistant":
                        delta = data.get("delta", "")
                        if delta:
                            full_text = data.get("text", full_text + delta)
                            print(delta, end="", flush=True)
                        chat_count += 1
                    elif stream == "tool":
                        print(f"  [tool] {json.dumps(data, ensure_ascii=False)[:150]}")
                    elif stream == "end" or (stream == "lifecycle" and data.get("phase") == "end"):
                        print(f"\n  [end]")
                        break
                    else:
                        print(f"  [{stream}] {json.dumps(data, ensure_ascii=False)[:150]}")
                elif event == "chat":
                    state = payload.get("state", "")
                    msg = payload.get("message", {})
                    content = msg.get("content", [])
                    text = "".join(c.get("text", "") for c in content if isinstance(c, dict))
                    if state == "final" and text:
                        full_text = text
                        print(f"\n  [chat/final] {text}")
                        break
                else:
                    print(f"  [{event}] {json.dumps(payload, ensure_ascii=False)[:100]}")
    except Exception as e:
        print(f"\n  [结束] {e}")

    print(f"\n共收到 {chat_count} 段文本")
    print(f"完整回复: {full_text[:500]}")

    await ws.close()
    print("\n测试完成")


if __name__ == "__main__":
    asyncio.run(main())
