"""
共用通知模組 (LINE + Teams)
所有 watcher 共用此模組發送錯誤/恢復通知
"""

import time
import requests

LINE_CHANNEL_ACCESS_TOKEN = "zGxbSyzcXGPTGlJA0H9IebOMUFn0XJqgAkI0zt/6UUhUH0HTCm6sgF8vHX2nAe12b5/H2o7YWGSf4iSs7CVrmIXMvoee66U8i6bHcJQVQVuXa5ObdC4bGmeuJnxm0gwnPkrMfMsftT4wKUDeBkLIogdB04t89/1O/w1cDnyilFU="
LINE_USER_ID = "U27153a213b9284361b380b9eb419d069"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"

TEAMS_WEBHOOK_URL = (
    "https://default15d82f974f154ead9ab618aa0cd453.88.environment.api.powerplatform.com:443"
    "/powerautomate/automations/direct/cu/29/workflows/8e919cca47ed4cb49cd58e589773976c"
    "/triggers/manual/paths/invoke?api-version=1&sp=%2Ftriggers%2Fmanual%2Frun&sv=1.0"
    "&sig=qkNRsez6F5qH6tdMGB6ZNTCYhXtSAU48h2ggTkjBH0s"
)

# 每個 source 記錄上次通知時間，避免重複轟炸
_last_notified: dict[str, float] = {}
NOTIFY_COOLDOWN = 3600  # 同一來源同一小時內不重複


def _send_to_line(message: str) -> bool:
    """發送到 LINE"""
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_CHANNEL_ACCESS_TOKEN}",
    }
    payload = {
        "to": LINE_USER_ID,
        "messages": [{"type": "text", "text": message}],
    }
    try:
        resp = requests.post(LINE_PUSH_URL, json=payload, headers=headers, timeout=10)
        return resp.status_code == 200
    except Exception:
        return False


def _send_to_teams(message: str) -> bool:
    """發送到 Teams (Power Automate webhook) - Adaptive Card 格式"""
    payload = {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.2",
                    "body": [
                        {"type": "TextBlock", "text": message, "wrap": True},
                    ],
                },
            }
        ],
    }
    try:
        resp = requests.post(TEAMS_WEBHOOK_URL, json=payload, headers={"Content-Type": "application/json"}, timeout=10)
        return resp.status_code in (200, 202)
    except Exception:
        return False


def send_line_message(message: str, source: str = "default") -> bool:
    """
    發送通知到 LINE + Teams。
    source: 識別來源（避免同一 watcher 短時間重複通知）
    回傳是否至少一個通道成功發送
    """
    now = time.time()
    last = _last_notified.get(source, 0)
    if now - last < NOTIFY_COOLDOWN:
        return False

    line_ok = _send_to_line(message)
    teams_ok = _send_to_teams(message)

    if line_ok or teams_ok:
        _last_notified[source] = now
        return True
    return False
