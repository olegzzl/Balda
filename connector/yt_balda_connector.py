# -*- coding: utf-8 -*-
"""
Балда: локальная игра + коннектор к чату YouTube.

Режимы:
  python yt_balda_connector.py --demo
      Игра + имитация зрителей (ничего настраивать не нужно).
  python yt_balda_connector.py
      Игра + настоящий чат YouTube (нужен config.txt рядом с этим файлом).

Открой в браузере: http://127.0.0.1:8765/
"""
import json
import os
import re
import sys
import threading
import time
import random
import urllib.parse
import urllib.request
from collections import deque
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
GAME_DIR = os.path.normpath(os.path.join(HERE, "..", "game"))
CONFIG_PATH = os.path.join(HERE, "config.txt")
HOST, PORT = "127.0.0.1", 8765

MESSAGES = deque(maxlen=5000)   # элементы: {"id": int, "nick": str, "text": str}
SEQ = {"n": 0}
LOCK = threading.Lock()


def push_message(nick, text):
    text = (text or "").strip()
    if not text:
        return
    with LOCK:
        SEQ["n"] += 1
        MESSAGES.append({"id": SEQ["n"], "nick": (nick or "зритель")[:40], "text": text[:120]})


# ----------------------------- демо-режим ---------------------------------
DEMO_NICKS = ["Катя", "ol_ze_fan", "Максим", "Юля", "streamer_friend", "Настя"]
# Реальные ходы для стартового поля БАЛДА: каждое слово добавляет одну новую
# букву и читается по соседним клеткам (проверено по правилам игры и словарю).
DEMO_WORDS = ["вал", "еда", "дар", "сад", "раб", "лад", "гад"]


def demo_loop():
    time.sleep(2)
    while True:
        push_message(random.choice(DEMO_NICKS), random.choice(DEMO_WORDS))
        time.sleep(random.uniform(2.5, 5.0))


# --------------------------- чтение config.txt -----------------------------
def read_config():
    cfg = {}
    if not os.path.exists(CONFIG_PATH):
        return cfg
    with open(CONFIG_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip().upper()] = v.strip()
    return cfg


def video_id_from(text):
    text = (text or "").strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", text):
        return text
    m = re.search(r"(?:v=|youtu\.be/|/live/)([A-Za-z0-9_-]{11})", text)
    return m.group(1) if m else None


def resolve_live_chat_id(api_key, video_id):
    url = ("https://www.googleapis.com/youtube/v3/videos"
           "?part=liveStreamingDetails&id=%s&key=%s"
           % (video_id, urllib.parse.quote(api_key)))
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8"))
    except Exception:
        # не включаем url (в нём ключ API) в текст ошибки
        raise RuntimeError(
            "Запрос к YouTube Data API не удался. Проверь API_KEY "
            "(включён ли YouTube Data API v3) и VIDEO_URL.")
    items = data.get("items") or []
    if not items:
        raise RuntimeError("Видео не найдено. Проверь VIDEO_URL в config.txt")
    details = items[0].get("liveStreamingDetails") or {}
    chat_id = details.get("activeLiveChatId")
    if not chat_id:
        raise RuntimeError(
            "У этого видео нет активного чата. Стрим должен быть запущен "
            "(или запланирован с включённым чатом).")
    return chat_id


# ------------------------ поток чата YouTube (gRPC) ------------------------
def youtube_loop(api_key, video_input):
    import grpc
    sys.path.insert(0, HERE)
    import stream_list_pb2
    import stream_list_pb2_grpc

    video_id = video_id_from(video_input)
    if not video_id:
        print("[yt] Не понял VIDEO_URL из config.txt:", repr(video_input))
        return
    print("[yt] Видео:", video_id)
    try:
        chat_id = resolve_live_chat_id(api_key, video_id)
    except Exception as e:
        print("[yt]", e)
        return
    print("[yt] liveChatId получен. Подключаюсь к чату...")

    page_token = ""
    backoff = 2
    while True:
        channel = None
        try:
            channel = grpc.secure_channel("youtube.googleapis.com:443",
                                          grpc.ssl_channel_credentials())
            stub = stream_list_pb2_grpc.V3DataLiveChatMessageServiceStub(channel)
            req = stream_list_pb2.LiveChatMessageListRequest(
                live_chat_id=chat_id,
                part=["snippet", "authorDetails"],
            )
            if page_token:
                req.page_token = page_token
            meta = [("x-goog-api-key", api_key)]
            responses = stub.StreamList(req, metadata=meta)
            backoff = 2
            for resp in responses:
                if resp.next_page_token:
                    page_token = resp.next_page_token
                for item in resp.items:
                    sn = item.snippet
                    if sn.type == 1:  # TEXT_MESSAGE_EVENT
                        push_message(item.author_details.display_name,
                                     sn.display_message)
                    elif sn.type == 4:  # CHAT_ENDED_EVENT
                        print("[yt] YouTube сообщил: чат трансляции завершён.")
                        return
            print("[yt] Поток закрыт сервером, переподключаюсь...")
        except KeyboardInterrupt:
            raise
        except Exception as e:
            print("[yt] Ошибка: %s. Повтор через %s с." % (e, backoff))
            time.sleep(backoff)
            backoff = min(backoff * 2, 60)
        finally:
            if channel is not None:
                try:
                    channel.close()
                except Exception:
                    pass


# ------------------------------ HTTP-сервер --------------------------------
MIME = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8", ".png": "image/png",
        ".woff2": "font/woff2", ".ttf": "font/ttf", ".otf": "font/otf"}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/yt-feed":
            q = urllib.parse.parse_qs(parsed.query)
            try:
                after = int(q.get("after", ["0"])[0])
            except ValueError:
                after = 0
            with LOCK:
                msgs = [m for m in MESSAGES if m["id"] > after][:200]
            body = json.dumps({"messages": msgs}, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path in ("/", ""):
            path = "/index.html"
        safe = os.path.normpath(path).lstrip("/\\")
        full = os.path.normpath(os.path.join(GAME_DIR, safe))
        if not full.startswith(GAME_DIR) or not os.path.isfile(full):
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        with open(full, "rb") as f:
            body = f.read()
        ext = os.path.splitext(full)[1].lower()
        self._send(200, body, MIME.get(ext, "application/octet-stream"))


def main():
    demo = "--demo" in sys.argv
    if demo:
        threading.Thread(target=demo_loop, daemon=True).start()
        print("[demo] Имитация зрителей включена.")
    else:
        cfg = read_config()
        api_key = cfg.get("API_KEY", "")
        video = cfg.get("VIDEO_URL", "")
        if not api_key or not video:
            print(__doc__)
            print("Нет config.txt (или в нём пусто). Для проверки без YouTube запусти:")
            print("    python yt_balda_connector.py --demo")
            sys.exit(1)
        t = threading.Thread(target=youtube_loop, args=(api_key, video), daemon=True)
        t.start()

    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print("Игра открыта тут:  http://%s:%d/" % (HOST, PORT))
    print("Остановка: Ctrl+C")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановлено.")


if __name__ == "__main__":
    main()
