from __future__ import annotations

import base64
import hashlib
import json
import os
import queue
import random
import socket
import ssl
import struct
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from typing import Any


OP_HEARTBEAT = 2
OP_HEARTBEAT_REPLY = 3
OP_MESSAGE = 5
OP_AUTH = 7
OP_AUTH_REPLY = 8
HEADER_LEN = 16

_LOCK = threading.Lock()
_THREAD: threading.Thread | None = None
_STOP = threading.Event()
_EVENTS: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=200)
_STATUS: dict[str, Any] = {"running": False, "connected": False, "error": "", "roomId": 0, "startedAt": 0}
_REPLY_TIMES: list[float] = []
_FINGERPRINT_COOKIE = ""


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _config(payload: dict[str, Any]) -> dict[str, Any]:
    config = payload.get("config", {})
    return config if isinstance(config, dict) else {}


def _config_bool(config: dict[str, Any], key: str, env_name: str, default: bool) -> bool:
    value = config.get(key)
    if isinstance(value, bool):
        return value
    if value not in (None, ""):
        return str(value).strip().lower() in {"1", "true", "yes", "on"}
    return _env(env_name, str(default).lower()).lower() in {"1", "true", "yes", "on"}


def _config_int(config: dict[str, Any], key: str, env_name: str, default: int) -> int:
    value = config.get(key)
    if value not in (None, ""):
        return int(value)
    return int(_env(env_name, str(default)) or str(default))


def _config_list(config: dict[str, Any], key: str) -> list[str]:
    value = config.get(key, [])
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def _fingerprint_cookie(timeout_seconds: int) -> str:
    global _FINGERPRINT_COOKIE
    if _FINGERPRINT_COOKIE:
        return _FINGERPRINT_COOKIE
    request = urllib.request.Request(
        "https://api.bilibili.com/x/frontend/finger/spi",
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
            ),
            "Referer": "https://live.bilibili.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=min(timeout_seconds, 8)) as response:
            data = json.loads(response.read().decode("utf-8", errors="replace"))
            payload = data.get("data", {}) if isinstance(data, dict) else {}
            buvid3 = str(payload.get("b_3") or "").strip()
            buvid4 = str(payload.get("b_4") or "").strip()
            parts = []
            if buvid3:
                parts.append(f"buvid3={buvid3}")
            if buvid4:
                parts.append(f"buvid4={buvid4}")
            if parts:
                _FINGERPRINT_COOKIE = "; ".join(parts)
    except Exception:
        return ""
    return _FINGERPRINT_COOKIE


def _cookie_from_file(config: dict[str, Any] | None) -> str:
    if config is None:
        return ""
    path = str(config.get("cookieFile") or "").strip()
    if not path:
        return ""
    base_dir = os.path.dirname(__file__)
    full_path = path if os.path.isabs(path) else os.path.join(base_dir, path)
    try:
        with open(full_path, "r", encoding="utf-8-sig") as handle:
            return handle.read().lstrip("\ufeff").strip()
    except OSError:
        return ""


def _cookie_value(cookie: str, name: str) -> str:
    prefix = name + "="
    for part in cookie.split(";"):
        item = part.strip()
        if item.startswith(prefix):
            return item[len(prefix) :].strip()
    return ""


def _configured_cookie(config: dict[str, Any], timeout_seconds: int) -> str:
    cookie = str(config.get("cookie") or "").strip()
    if not cookie:
        cookie = _cookie_from_file(config)
    cookie = cookie or _env("YUYU_BILIBILI_COOKIE") or _env("BILIBILI_COOKIE")
    if not cookie:
        cookie = _fingerprint_cookie(timeout_seconds)
    return cookie


def _api_headers(room_id: int = 0, config: dict[str, Any] | None = None, timeout_seconds: int = 12) -> dict[str, str]:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7",
        "Origin": "https://live.bilibili.com",
        "Referer": f"https://live.bilibili.com/{room_id}" if room_id else "https://live.bilibili.com/",
    }
    cookie = _configured_cookie(config or {}, timeout_seconds)
    if cookie:
        headers["Cookie"] = cookie
    return headers


def _api_json(url: str, timeout_seconds: int, room_id: int = 0, config: dict[str, Any] | None = None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers=_api_headers(room_id, config, timeout_seconds))
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            return json.loads(response.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as error:
        detail = error.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"Bilibili API error {error.code}: {detail}") from error


def _room_init(room_id: int, timeout_seconds: int, config: dict[str, Any]) -> int:
    data = _api_json(f"https://api.live.bilibili.com/room/v1/Room/room_init?id={room_id}", timeout_seconds, room_id, config)
    if data.get("code") != 0:
        raise RuntimeError(f"room_init failed: {data}")
    payload = data.get("data", {})
    real_room_id = int(payload.get("room_id") or room_id)
    return real_room_id


def _danmu_info(room_id: int, timeout_seconds: int, config: dict[str, Any]) -> tuple[str, str, int, str]:
    errors: list[str] = []
    try:
        data = _api_json(
            f"https://api.live.bilibili.com/xlive/web-room/v1/index/getDanmuInfo?id={room_id}&type=0",
            timeout_seconds,
            room_id,
            config,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"getDanmuInfo failed: {data}")
        info = data.get("data", {})
        token = str(info.get("token") or "")
        hosts = info.get("host_list") or []
        if not hosts:
            return "broadcastlv.chat.bilibili.com", token, 443, "api-empty-hosts"
        host = hosts[0]
        return (
            str(host.get("host") or "broadcastlv.chat.bilibili.com"),
            token,
            int(host.get("wss_port") or host.get("ws_port") or 443),
            "getDanmuInfo",
        )
    except Exception as error:
        errors.append(str(error))

    try:
        data = _api_json(
            f"https://api.live.bilibili.com/room/v1/Danmu/getConf?room_id={room_id}&platform=pc&player=web",
            timeout_seconds,
            room_id,
            config,
        )
        if data.get("code") != 0:
            raise RuntimeError(f"getConf failed: {data}")
        info = data.get("data", {})
        token = str(info.get("token") or "")
        hosts = info.get("host_server_list") or []
        if not hosts:
            return "broadcastlv.chat.bilibili.com", token, 443, "getConf-empty-hosts"
        host = hosts[0]
        return (
            str(host.get("host") or "broadcastlv.chat.bilibili.com"),
            token,
            int(host.get("wss_port") or host.get("ws_port") or 443),
            "getConf",
        )
    except Exception as error:
        errors.append(str(error))
    raise RuntimeError(" | ".join(errors))


def _websocket_key() -> str:
    return base64.b64encode(os.urandom(16)).decode("ascii")


def _connect_websocket(host: str, port: int, timeout_seconds: int) -> ssl.SSLSocket:
    raw = socket.create_connection((host, port), timeout=timeout_seconds)
    sock = ssl.create_default_context().wrap_socket(raw, server_hostname=host)
    key = _websocket_key()
    request = (
        f"GET /sub HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "User-Agent: Yuyu-Mind bilibili_live plugin\r\n"
        "\r\n"
    ).encode("ascii")
    sock.sendall(request)
    response = b""
    while b"\r\n\r\n" not in response:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("websocket handshake returned empty response")
        response += chunk
    if b" 101 " not in response.split(b"\r\n", 1)[0]:
        raise RuntimeError(f"websocket handshake failed: {response[:160]!r}")
    return sock


def _send_ws_frame(sock: ssl.SSLSocket, payload: bytes, opcode: int = 2) -> None:
    first = 0x80 | opcode
    mask_bit = 0x80
    length = len(payload)
    if length < 126:
        header = struct.pack("!BB", first, mask_bit | length)
    elif length < 65536:
        header = struct.pack("!BBH", first, mask_bit | 126, length)
    else:
        header = struct.pack("!BBQ", first, mask_bit | 127, length)
    mask = os.urandom(4)
    masked = bytes(payload[index] ^ mask[index % 4] for index in range(length))
    sock.sendall(header + mask + masked)


def _recv_exact(sock: ssl.SSLSocket, length: int) -> bytes:
    data = b""
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("websocket connection closed")
        data += chunk
    return data


def _recv_ws_frame(sock: ssl.SSLSocket) -> bytes:
    header = _recv_exact(sock, 2)
    opcode = header[0] & 0x0F
    second = header[1]
    length = second & 0x7F
    if length == 126:
        length = struct.unpack("!H", _recv_exact(sock, 2))[0]
    elif length == 127:
        length = struct.unpack("!Q", _recv_exact(sock, 8))[0]
    if second & 0x80:
        mask = _recv_exact(sock, 4)
        payload = _recv_exact(sock, length)
        payload = bytes(payload[index] ^ mask[index % 4] for index in range(length))
    else:
        payload = _recv_exact(sock, length)
    if opcode == 0x8:
        code = 0
        reason = ""
        if len(payload) >= 2:
            code = struct.unpack("!H", payload[:2])[0]
            reason = payload[2:].decode("utf-8", errors="replace")
        raise RuntimeError(f"websocket closed by server: code={code} reason={reason}")
    if opcode == 0x9:
        _send_ws_frame(sock, payload, opcode=0xA)
        return b""
    return payload


def _pack_packet(operation: int, body: dict[str, Any] | bytes, version: int = 1) -> bytes:
    payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if isinstance(body, dict) else body
    header = struct.pack("!IHHII", HEADER_LEN + len(payload), HEADER_LEN, version, operation, 1)
    return header + payload


def _unpack_messages(packet: bytes) -> list[dict[str, Any]]:
    messages: list[dict[str, Any]] = []
    offset = 0
    while offset + HEADER_LEN <= len(packet):
        packet_len, header_len, version, operation, _sequence = struct.unpack("!IHHII", packet[offset : offset + HEADER_LEN])
        body = packet[offset + header_len : offset + packet_len]
        offset += packet_len
        if operation == OP_MESSAGE:
            if version == 2:
                try:
                    messages.extend(_unpack_messages(zlib.decompress(body)))
                except Exception:
                    continue
            elif version in {0, 1}:
                for line in body.split(b"\x00"):
                    if not line.strip():
                        continue
                    try:
                        value = json.loads(line.decode("utf-8", errors="replace"))
                        if isinstance(value, dict):
                            messages.append(value)
                    except Exception:
                        continue
        elif operation in {OP_AUTH_REPLY, OP_HEARTBEAT_REPLY}:
            continue
    return messages


def _push_event(event: dict[str, Any]) -> None:
    try:
        _EVENTS.put_nowait(event)
    except queue.Full:
        try:
            _EVENTS.get_nowait()
        except queue.Empty:
            pass
        _EVENTS.put_nowait(event)


def _can_reply(config: dict[str, Any], priority: int) -> bool:
    now = time.time()
    max_replies = _config_int(config, "maxRepliesPerMinute", "YUYU_BILIBILI_MAX_REPLIES_PER_MINUTE", 4)
    if max_replies <= 0:
        return False
    while _REPLY_TIMES and now - _REPLY_TIMES[0] > 60:
        _REPLY_TIMES.pop(0)
    if priority >= _config_int(config, "speakPriority", "YUYU_BILIBILI_SPEAK_PRIORITY", 60):
        _REPLY_TIMES.append(now)
        return True
    if len(_REPLY_TIMES) >= max_replies:
        return False
    _REPLY_TIMES.append(now)
    return True


def _contains_blocked(text: str, config: dict[str, Any]) -> bool:
    lowered = text.lower()
    return any(word.lower() in lowered for word in _config_list(config, "blockedWords"))


def _mentioned(text: str, config: dict[str, Any]) -> bool:
    lowered = text.lower()
    return any(name.lower() in lowered for name in _config_list(config, "mentionNames"))


def _event_from_message(message: dict[str, Any], config: dict[str, Any]) -> dict[str, Any] | None:
    command = str(message.get("cmd", "")).split(":", 1)[0]
    now_ms = int(time.time() * 1000)
    if command == "DANMU_MSG":
        info = message.get("info") or []
        if not isinstance(info, list) or len(info) < 3:
            return None
        text = str(info[1]).strip()
        user_info = info[2] if isinstance(info[2], list) else []
        user = str(user_info[1] if len(user_info) > 1 else "观众")
        if not text or _contains_blocked(text, config):
            return None
        mention = _mentioned(text, config)
        should_reply = mention or not _config_bool(config, "onlyReplyMentions", "YUYU_BILIBILI_ONLY_REPLY_MENTIONS", True)
        priority = 55 if mention else 25
        if should_reply:
            should_reply = _can_reply(config, priority)
        return {
            "id": hashlib.sha1(f"{now_ms}:{user}:{text}".encode("utf-8")).hexdigest(),
            "type": "danmaku",
            "user": user,
            "text": text,
            "priority": priority,
            "shouldReply": should_reply,
            "speak": priority >= _config_int(config, "speakPriority", "YUYU_BILIBILI_SPEAK_PRIORITY", 60),
            "metadata": {"command": command, "mention": mention},
        }
    if command == "SEND_GIFT" and _config_bool(config, "replyToGifts", "YUYU_BILIBILI_REPLY_TO_GIFTS", True):
        data = message.get("data") or {}
        user = str(data.get("uname") or "观众")
        gift = str(data.get("giftName") or "礼物")
        count = data.get("num") or 1
        text = f"送出了 {count} 个 {gift}"
        priority = 80
        return {
            "id": hashlib.sha1(f"gift:{now_ms}:{user}:{gift}:{count}".encode("utf-8")).hexdigest(),
            "type": "gift",
            "user": user,
            "text": text,
            "priority": priority,
            "shouldReply": _can_reply(config, priority),
            "speak": True,
            "metadata": {"command": command, "gift": gift, "count": count},
        }
    if command in {"INTERACT_WORD", "WELCOME"} and _config_bool(config, "replyToEnter", "YUYU_BILIBILI_REPLY_TO_ENTER", False):
        data = message.get("data") or {}
        user = str(data.get("uname") or data.get("uname_color") or "观众")
        return {
            "id": hashlib.sha1(f"enter:{now_ms}:{user}".encode("utf-8")).hexdigest(),
            "type": "enter",
            "user": user,
            "text": "进入了直播间",
            "priority": 20,
            "shouldReply": _can_reply(config, 20),
            "speak": False,
            "metadata": {"command": command},
        }
    return None


def _set_status(**values: Any) -> None:
    with _LOCK:
        _STATUS.update(values)


def _worker(config: dict[str, Any]) -> None:
    room_id = _config_int(config, "roomId", "YUYU_BILIBILI_ROOM_ID", 0)
    timeout_seconds = _config_int(config, "timeoutSeconds", "YUYU_BILIBILI_TIMEOUT_SECONDS", 12)
    _set_status(running=True, connected=False, error="", roomId=room_id, startedAt=time.time())
    while not _STOP.is_set():
        sock: ssl.SSLSocket | None = None
        try:
            real_room_id = _room_init(room_id, timeout_seconds, config)
            _set_status(roomId=real_room_id, configuredRoomId=room_id)
            try:
                host, token, port, route = _danmu_info(real_room_id, timeout_seconds, config)
                route_error = ""
            except Exception as error:
                host, token, port, route = "broadcastlv.chat.bilibili.com", "", 443, "default-gateway"
                route_error = str(error)
                _set_status(lastApiError=route_error)
            _set_status(host=host, port=port, route=route, tokenLength=len(token))
            sock = _connect_websocket(host, port, timeout_seconds)
            cookie = _configured_cookie(config, timeout_seconds)
            uid_text = _cookie_value(cookie, "DedeUserID")
            uid = int(uid_text) if uid_text.isdigit() else 0
            auth = {
                "uid": uid,
                "roomid": real_room_id,
                "protover": 2,
                "platform": "web",
                "type": 2,
                "key": token,
                "buvid": _cookie_value(cookie, "buvid3"),
            }
            _send_ws_frame(sock, _pack_packet(OP_AUTH, auth), opcode=2)
            _send_ws_frame(sock, _pack_packet(OP_HEARTBEAT, b"[object Object]"), opcode=2)
            last_heartbeat = time.time()
            _set_status(connected=True, error=route_error, roomId=real_room_id, host=host, port=port, route=route, tokenLength=len(token))
            while not _STOP.is_set():
                if time.time() - last_heartbeat > 25:
                    _send_ws_frame(sock, _pack_packet(OP_HEARTBEAT, b"[object Object]"), opcode=2)
                    last_heartbeat = time.time()
                sock.settimeout(1.5)
                try:
                    frame = _recv_ws_frame(sock)
                except socket.timeout:
                    continue
                if not frame:
                    continue
                for raw in _unpack_messages(frame):
                    event = _event_from_message(raw, config)
                    if event is not None:
                        _push_event(event)
        except Exception as error:
            _set_status(connected=False, error=str(error))
            time.sleep(5)
        finally:
            if sock is not None:
                try:
                    sock.close()
                except Exception:
                    pass
    _set_status(running=False, connected=False)


def _ensure_worker(config: dict[str, Any]) -> None:
    global _THREAD
    enabled = _config_bool(config, "enabled", "YUYU_BILIBILI_ENABLED", False)
    room_id = _config_int(config, "roomId", "YUYU_BILIBILI_ROOM_ID", 0)
    if not enabled or room_id <= 0:
        _set_status(running=False, connected=False, roomId=room_id, error="" if room_id else "roomId is not configured")
        return
    with _LOCK:
        if _THREAD is not None and _THREAD.is_alive():
            return
        _STOP.clear()
        _THREAD = threading.Thread(target=_worker, args=(dict(config),), daemon=True)
        _THREAD.start()


def poll(payload: dict[str, Any]) -> dict[str, Any]:
    config = _config(payload)
    _ensure_worker(config)
    max_events = max(1, min(20, int(payload.get("maxEvents") or _config_int(config, "maxEventsPerPoll", "YUYU_BILIBILI_MAX_EVENTS_PER_POLL", 5))))
    events: list[dict[str, Any]] = []
    for _ in range(max_events):
        try:
            events.append(_EVENTS.get_nowait())
        except queue.Empty:
            break
    return {
        "ok": True,
        "plugin": "bilibili_live",
        "action": "poll",
        "summary": f"Fetched {len(events)} Bilibili live event(s).",
        "events": events,
        "metadata": dict(_STATUS),
    }


def status(payload: dict[str, Any]) -> dict[str, Any]:
    config = _config(payload)
    _ensure_worker(config)
    return {"ok": True, "plugin": "bilibili_live", "action": "status", "summary": "Bilibili live status.", "metadata": dict(_STATUS)}


ACTIONS = {"poll": poll, "status": status}
