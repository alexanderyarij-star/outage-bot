#!/usr/bin/env python3
"""
Бот для GitHub Actions: один запуск = одна проверка канала «Включатель Одесса»
(t.me/Odessa_svet_voda). Пишет в Telegram ТОЛЬКО когда меняется график очереди GROUP.
Если обновились другие очереди, а твоя та же, бот молчит.

Токен и ID чата берутся из секретов GitHub (BOT_TOKEN, CHAT_ID), в код их вставлять не нужно.
Память между запусками хранится в файле state.json (его сохраняет workflow).
"""
import html
import json
import os
import re
import urllib.parse
import urllib.request

BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
CHAT_IDS = [c.strip() for c in os.environ.get("CHAT_ID", "").split(",") if c.strip()]
CHANNEL = "Odessa_svet_voda"
GROUP = "2.2"                # твоя очередь
NOTIFY_EMERGENCY = True      # присылать и аварийные изменения по твоей очереди
STATE_FILE = "state.json"

GROUP_RE = re.escape(GROUP)
INTERVAL_RE = re.compile(r"(\d{1,2}):(\d{2})\s*[–—-]\s*(\d{1,2}):(\d{2})")


def safe(e):
    """Текст ошибки без токена (логи в публичном репозитории видны всем)."""
    text = str(e)
    return text.replace(BOT_TOKEN, "***") if BOT_TOKEN else text


# ---------- сеть ----------
def http_get(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def tg(method, **params):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/{method}"
    data = urllib.parse.urlencode(params).encode()
    with urllib.request.urlopen(url, data=data, timeout=30) as r:
        return json.loads(r.read().decode())


def send(chat_id, text):
    try:
        tg("sendMessage", chat_id=chat_id, text=text)
        return True
    except Exception as e:
        print("Не удалось отправить сообщение:", safe(e))
        return False


# ---------- чтение канала ----------
def fetch_posts():
    """Возвращает список (id_поста, текст) из веб-версии канала."""
    page = http_get(f"https://t.me/s/{CHANNEL}")
    parts = re.split(r'data-post="%s/(\d+)"' % re.escape(CHANNEL), page)
    posts = {}
    for i in range(1, len(parts) - 1, 2):
        m = re.search(r"tgme_widget_message_text[^>]*>(.*?)</div>", parts[i + 1], re.S)
        if m and int(parts[i]) not in posts:
            t = re.sub(r"<br\s*/?>", "\n", m.group(1))
            posts[int(parts[i])] = html.unescape(re.sub(r"<[^>]+>", "", t))
    return sorted(posts.items())


def parse_schedule(text):
    """Пост «Графік погодинних відключень на ДД.ММ.ГГГГ» -> (дата, строка для GROUP)."""
    d = re.search(r"Графік погодинних відключень на (\d{2}\.\d{2}\.\d{4})", text)
    if not d:
        return None
    line = re.search(r"Черга\s+%s\s*:\s*(.+)" % GROUP_RE, text)
    if not line:
        return None
    return d.group(1), line.group(1).strip()


def parse_emergency(text):
    """Пост «ДТЕК: зміни по відключеннях» -> строки про твою очередь."""
    if "зміни по відключеннях" not in text:
        return []
    out = []
    for line in text.splitlines():
        if re.search(r"Черга\s+%s(?!\d)" % GROUP_RE, line):
            out.append(re.sub(r"^[•\s]+", "", line).strip())
    return list(dict.fromkeys(out))


# ---------- текст сообщений ----------
def describe(value):
    iv = INTERVAL_RE.findall(value)
    if not iv:
        return "отключений не указано"
    mins, times = 0, []
    for h1, m1, h2, m2 in iv:
        a, b = int(h1) * 60 + int(m1), int(h2) * 60 + int(m2)
        if b <= a:
            b += 24 * 60
        mins += b - a
        times.append(f"{int(h1):02d}:{m1}–{int(h2):02d}:{m2}")
    h, m = divmod(mins, 60)
    total = (f"{h} ч " if h else "") + (f"{m} мин" if m else "")
    return ", ".join(times) + f"\nВсего без света: {total.strip()}"


def schedule_message(date, new, old):
    msg = f"💡 График на {date}, очередь {GROUP}\n{describe(new)}"
    if old:
        msg += f"\n\nБыло:\n{describe(old)}"
    return msg


def current_text(state):
    items = list(state["schedule"].items())[-2:]
    if not items:
        return "Пока графика нет, напишу, как только появится."
    return "\n\n".join(f"{d}, очередь {GROUP}\n{describe(v)}" for d, v in items)


# ---------- логика ----------
def process(posts, state, notify):
    msgs = []
    ids = {str(i) for i, _ in posts}
    for pid, text in posts:
        sch = parse_schedule(text)
        if sch:
            date, value = sch
            if state["posts"].get(str(pid)) != value:      # новый пост или правка поста
                state["posts"][str(pid)] = value
                old = state["schedule"].get(date)
                if old != value:                            # у ТВОЕЙ очереди реально изменилось
                    state["schedule"][date] = value
                    msgs.append(schedule_message(date, value, old))
        if NOTIFY_EMERGENCY:
            lines = parse_emergency(text)
            if lines:
                sent = state["emerg"].setdefault(str(pid), [])
                new = [l for l in lines if l not in sent]
                if new:
                    sent.extend(new)
                    msgs.append(f"⚡ ДТЕК, изменения по очереди {GROUP}:\n" + "\n".join("• " + l for l in new))
    state["posts"] = {k: v for k, v in state["posts"].items() if k in ids}
    state["emerg"] = {k: v for k, v in state["emerg"].items() if k in ids}
    while len(state["schedule"]) > 20:
        state["schedule"].pop(next(iter(state["schedule"])))
    return msgs if notify else []


def load_state():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    for key, default in (("posts", {}), ("schedule", {}), ("emerg", {}), ("init", False)):
        state.setdefault(key, default)
    return state


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


def main():
    if not BOT_TOKEN or not CHAT_IDS:
        raise SystemExit("Не заданы секреты BOT_TOKEN и CHAT_ID (Settings → Secrets and variables → Actions).")
    state = load_state()
    first = not state["init"]
    try:
        posts = fetch_posts()
    except Exception as e:
        print("Не удалось открыть канал:", safe(e))
        return
    if not posts:
        print("В канале не нашлось постов (страница пустая или изменился формат).")
        return
    msgs = process(posts, state, notify=not first)   # первый запуск: только запоминаем
    if first:
        msgs = [f"✅ Бот запущен, слежу за очередью {GROUP}.\n\n" + current_text(state)]
    state["init"] = True
    ok = True
    for m in msgs:
        for chat_id in CHAT_IDS:
            ok = send(chat_id, m) and ok
    if not ok:
        print("Состояние не сохранено, повторю при следующем запуске.")
        return
    save_state(state)
    print(f"Готово. Отправлено сообщений: {len(msgs)}.")


if __name__ == "__main__":
    main()
