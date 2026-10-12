# -*- coding: utf-8 -*-
"""
OZON FBS Express — уведомления о новых заказах прямо на ПК.

Опрашивает Ozon Seller API каждые POLL_SECONDS секунд, ловит НОВЫЕ заказы
(особенно RealFBS Express, окно сборки ~15 мин) и поднимает на весь экран
тревожное окно + сирену, пока ты не нажмёшь "ПРИНЯЛ".

Зависимостей нет — только стандартная библиотека Python.

Запуск:
    python ozon_alert.py            обычный режим (фон)
    python ozon_alert.py --test     показать демо-окно, проверить как орёт
    python ozon_alert.py --reset    забыть все известные заказы
    python ozon_alert.py --express-only   орать только на Express-заказы
"""

import sys
import os
import json
import time
import queue
import threading
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def _load_dotenv():
    """Простейшая подгрузка .env (KEY=VALUE) без внешних зависимостей."""
    path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                val = val.strip().strip('"').strip("'")
                os.environ.setdefault(key, val)
    except Exception:
        pass


_load_dotenv()

# ──────────────────────────── НАСТРОЙКИ ────────────────────────────
# Учётные данные берём из переменных окружения (см. .env.example).
CLIENT_ID = os.environ.get("OZON_CLIENT_ID", "<OZON_CLIENT_ID>")
API_KEY   = os.environ.get("OZON_API_KEY", "<OZON_API_KEY>")

POLL_SECONDS   = 20          # как часто опрашивать Ozon (сек)
LOOKBACK_HOURS = 6           # за сколько часов назад смотреть заказы
EXPRESS_ONLY   = True        # True = орать ТОЛЬКО на Express (is_express). --all снимет на время
WAREHOUSE_IDS  = set()       # пусто = любой склад; вписать id (int) чтобы жёстко прибить к складу Express
ALERT_STATUSES = {"awaiting_packaging"}  # на каких статусах поднимать тревогу
# ───────────────────────────────────────────────────────────────────

SEEN_FILE = os.path.join(BASE_DIR, "seen.json")
LOG_FILE  = os.path.join(BASE_DIR, "ozon_alert.log")
API_URL   = "https://api-seller.ozon.ru/v3/posting/fbs/list"
ORDERS_URL = "https://seller.ozon.ru/app/orders/fbs-orders"

# ──────────────────────────── ЛОГ ────────────────────────────
log = logging.getLogger("ozon_alert")
log.setLevel(logging.INFO)
_h = RotatingFileHandler(LOG_FILE, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
_h.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
log.addHandler(_h)
_c = logging.StreamHandler(sys.stdout)
_c.setFormatter(logging.Formatter("%(asctime)s  %(message)s"))
log.addHandler(_c)


# ──────────────────────────── СОСТОЯНИЕ ────────────────────────────
def load_seen():
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    except Exception:
        return set()


def save_seen(seen):
    try:
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            json.dump(sorted(seen), f, ensure_ascii=False)
    except Exception as e:
        log.info(f"Не смог сохранить seen.json: {e}")


# ──────────────────────────── API ────────────────────────────
def api_list_postings():
    """Возвращает список заказов за последние LOOKBACK_HOURS часов (с пагинацией)."""
    now = datetime.now(timezone.utc)
    since = (now - timedelta(hours=LOOKBACK_HOURS)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    to    = (now + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    headers = {
        "Client-Id": CLIENT_ID,
        "Api-Key": API_KEY,
        "Content-Type": "application/json",
    }
    out = []
    offset = 0
    while True:
        payload = {
            "dir": "DESC",
            "filter": {"since": since, "to": to},
            "limit": 50,
            "offset": offset,
            "with": {"analytics_data": False, "financial_data": False},
        }
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(API_URL, data=data, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=25) as resp:
            body = json.loads(resp.read().decode("utf-8"))
        postings = body.get("result", {}).get("postings", []) or []
        out.extend(postings)
        if body.get("result", {}).get("has_next") and len(postings) == 50:
            offset += 50
        else:
            break
    return out


# ──────────────────────────── ОЧЕРЕДЬ АЛЕРТОВ ────────────────────────────
alert_q = queue.Queue()


def poll_loop():
    seen = load_seen()
    first_run = not os.path.exists(SEEN_FILE)
    if first_run:
        log.info("Первый запуск: помечаю текущие заказы как известные, чтобы не орать на старые.")

    while True:
        try:
            postings = api_list_postings()
            fresh = []
            for p in postings:
                pn = p.get("posting_number")
                if not pn or pn in seen:
                    continue
                # фиксируем как виденный сразу, чтобы не задвоить
                seen.add(pn)
                status = p.get("status")
                is_express = bool(p.get("is_express"))
                dm = p.get("delivery_method") or {}
                wid = dm.get("warehouse_id")
                # диагностика КАЖДОГО нового заказа — чтобы поймать, как Ozon метит Express
                if not first_run:
                    log.info(f"new posting {pn} | express={is_express} | wh_id={wid} "
                             f"| integ={p.get('tpl_integration_type')} | status={status}")
                if status not in ALERT_STATUSES:
                    continue
                if EXPRESS_ONLY and not is_express:
                    continue
                if WAREHOUSE_IDS and wid not in WAREHOUSE_IDS:
                    continue
                fresh.append(p)

            save_seen(seen)

            if fresh and not first_run:
                for p in fresh:
                    log.info(f"НОВЫЙ ЗАКАЗ: {p.get('posting_number')} "
                             f"express={p.get('is_express')} status={p.get('status')}")
                    alert_q.put(p)
            elif first_run:
                log.info(f"Стартовых заказов в работе: {len(postings)}. Жду новые...")
            first_run = False

        except urllib.error.HTTPError as e:
            log.info(f"HTTP {e.code}: {e.read().decode('utf-8', 'ignore')[:300]}")
        except Exception as e:
            log.info(f"Ошибка опроса: {e}")

        time.sleep(POLL_SECONDS)


# ──────────────────────────── ЗВУК ────────────────────────────
def siren(stop_event, express):
    """Сирена в отдельном потоке, пока не нажмут 'ПРИНЯЛ'."""
    try:
        import winsound
    except Exception:
        return
    while not stop_event.is_set():
        try:
            if express:
                winsound.Beep(1500, 250);
                if stop_event.is_set(): break
                winsound.Beep(1000, 250)
            else:
                winsound.Beep(1000, 350)
                if stop_event.is_set(): break
                winsound.Beep(700, 350)
        except Exception:
            time.sleep(0.5)
        stop_event.wait(0.15)


# ──────────────────────────── ОКНО ────────────────────────────
def human_deadline(shipment_date):
    if not shipment_date:
        return "—"
    try:
        dt = datetime.strptime(shipment_date, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        left = dt - datetime.now(timezone.utc)
        mins = int(left.total_seconds() // 60)
        local = dt + timedelta(hours=4)  # Ижевск UTC+4
        when = local.strftime("%d.%m %H:%M")
        if mins < 0:
            return f"{when} (мск+1)  ⚠ ПРОСРОЧЕН на {abs(mins)} мин"
        if mins < 600:
            return f"{when} (мск+1)  ⏳ осталось ~{mins} мин"
        return f"{when} (мск+1)"
    except Exception:
        return shipment_date


def show_alert(root, p):
    import tkinter as tk
    import webbrowser

    express = bool(p.get("is_express"))
    pn = p.get("posting_number", "—")
    status = p.get("status", "")
    ship = p.get("shipment_date", "")
    wh = (p.get("delivery_method") or {}).get("warehouse", "")
    products = p.get("products", []) or []

    win = tk.Toplevel(root)
    win.title("OZON — НОВЫЙ ЗАКАЗ")
    bg = "#b00020" if express else "#0a4d8c"
    win.configure(bg=bg)
    win.attributes("-topmost", True)
    win.resizable(False, False)

    W, H = 600, 470
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    win.geometry(f"{W}x{H}+{(sw-W)//2}+{(sh-H)//3}")

    head = "⚡ EXPRESS — СОБРАТЬ СРОЧНО!" if express else "📦 НОВЫЙ ЗАКАЗ OZON"
    tk.Label(win, text=head, bg=bg, fg="white",
             font=("Segoe UI", 22, "bold")).pack(pady=(18, 4))

    tk.Label(win, text=f"№ {pn}", bg=bg, fg="white",
             font=("Consolas", 18, "bold")).pack(pady=2)

    info = f"Статус: {status}\nСклад: {wh}\nОтгрузка: {human_deadline(ship)}"
    tk.Label(win, text=info, bg=bg, fg="white", justify="left",
             font=("Segoe UI", 13)).pack(pady=8, padx=20, anchor="w")

    # товары
    box = tk.Frame(win, bg="white")
    box.pack(fill="both", expand=True, padx=20, pady=6)
    tk.Label(box, text="Собрать:", bg="white", fg="#222",
             font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=10, pady=(8, 2))
    for it in products:
        line = f"• {it.get('quantity')} × {it.get('name','')}  [{it.get('offer_id','')}]"
        tk.Label(box, text=line, bg="white", fg="#111", justify="left",
                 wraplength=W-70, font=("Segoe UI", 12)).pack(anchor="w", padx=14, pady=1)

    stop_event = threading.Event()
    threading.Thread(target=siren, args=(stop_event, express), daemon=True).start()

    def accept():
        stop_event.set()
        win.destroy()

    def open_ozon():
        try:
            webbrowser.open(ORDERS_URL)
        except Exception:
            pass

    btns = tk.Frame(win, bg=bg)
    btns.pack(pady=12)
    tk.Button(btns, text="ПРИНЯЛ ✓", command=accept,
              font=("Segoe UI", 16, "bold"), bg="#0a8a0a", fg="white",
              activebackground="#0c6e0c", width=14, height=1, bd=0).pack(side="left", padx=8)
    tk.Button(btns, text="Открыть Ozon", command=open_ozon,
              font=("Segoe UI", 12), bg="#dddddd", fg="#111",
              width=14, bd=0).pack(side="left", padx=8)

    win.protocol("WM_DELETE_WINDOW", accept)
    win.bell()
    win.lift()
    win.focus_force()


# ──────────────────────────── MAIN ────────────────────────────
def pump(root):
    try:
        while True:
            p = alert_q.get_nowait()
            show_alert(root, p)
    except queue.Empty:
        pass
    root.after(400, lambda: pump(root))


def make_demo_posting():
    now = datetime.now(timezone.utc) + timedelta(minutes=14)
    return {
        "posting_number": "0150534008-9999-1",
        "status": "awaiting_packaging",
        "is_express": True,
        "shipment_date": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "delivery_method": {"warehouse": "Ижевск"},
        "products": [{"quantity": 3, "name": "Огурцы свежие, 1 кг", "offer_id": "OG-001"},
                     {"quantity": 1, "name": "Помидоры черри, 250 г", "offer_id": "TM-014"}],
    }


def main():
    import tkinter as tk

    args = set(sys.argv[1:])
    global EXPRESS_ONLY
    if "--express-only" in args:
        EXPRESS_ONLY = True
    if "--all" in args:
        EXPRESS_ONLY = False   # временно: орать на ВСЕ новые заказы (отладка)
    if "--reset" in args:
        try:
            os.remove(SEEN_FILE)
            print("seen.json удалён — известные заказы забыты.")
        except FileNotFoundError:
            print("seen.json и так нет.")
        return

    root = tk.Tk()
    root.withdraw()

    if "--test" in args:
        log.info("ТЕСТ: показываю демо-окно Express-заказа.")
        alert_q.put(make_demo_posting())
    else:
        if not CLIENT_ID or not API_KEY or CLIENT_ID.startswith("<") or API_KEY.startswith("<"):
            log.info("Не заданы OZON_CLIENT_ID / OZON_API_KEY. "
                     "Скопируйте .env.example в .env и впишите ключи кабинета Ozon.")
            return
        log.info(f"Старт. Опрос каждые {POLL_SECONDS}с, lookback {LOOKBACK_HOURS}ч, "
                 f"express_only={EXPRESS_ONLY}.")
        threading.Thread(target=poll_loop, daemon=True).start()

    pump(root)
    root.mainloop()


if __name__ == "__main__":
    main()
