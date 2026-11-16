import json
import os
import re
import sqlite3
import time
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Header, Query, Body
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
ADMIN_SECRET_KEY = os.getenv("ADMIN_SECRET_KEY", "neadaptar_secret_key_123")
BASE_URL = "https://api.rasp.yandex.net/v3.0"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")

app = FastAPI(title="Train Info Service")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_cache (
                cache_key TEXT PRIMARY KEY,
                data_json TEXT,
                created_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS train_index (
                number TEXT,
                uid TEXT PRIMARY KEY,
                title TEXT,
                carrier TEXT
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS train_overrides (
                uid TEXT PRIMARY KEY,
                category TEXT,
                model TEXT,
                composition TEXT,
                shelves TEXT,
                sockets TEXT,
                dep_platform TEXT,
                arr_platform TEXT
            )
        """)

init_db()


def get_cached_thread(key: str, ttl_seconds: int = 43200) -> dict | None:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT data_json, created_at FROM thread_cache WHERE cache_key = ?", (key,)
        ).fetchone()
        if row and (time.time() - row[1]) < ttl_seconds:
            return json.loads(row[0])
    return None


def set_cached_thread(key: str, data: dict):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO thread_cache (cache_key, data_json, created_at) VALUES (?, ?, ?)",
            (key, json.dumps(data, ensure_ascii=False), time.time())
        )


def apply_overrides(uid: str, data: dict) -> dict:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT category, model, composition, shelves, sockets, dep_platform, arr_platform FROM train_overrides WHERE uid = ?",
            (uid,)
        ).fetchone()
        if row:
            data["category"] = row[0] or data["category"]
            data["model"] = row[1] or data["model"]
            data["composition"] = row[2] or data["composition"]
            data["shelves"] = row[3] or data["shelves"]
            data["sockets"] = row[4] or data["sockets"]
            if row[5]:
                data["dep_platform"] = row[5]
            if row[6]:
                data["arr_platform"] = row[6]
            data["is_override"] = True
    return data


def detect_rolling_stock(number: str, title: str, carrier: str, subtype: str) -> dict:
    num_digits = re.sub(r"\D", "", number)
    num_int = int(num_digits) if num_digits else 0
    title_l = title.lower()
    carrier_l = carrier.lower()
    subtype_l = subtype.lower()

    res = {
        "category": subtype or "Пассажирский поезд",
        "model": "Вагоны ТВЗ (стандартный модельный ряд)",
        "composition": "От 9 до 16 вагонов (по сезону)",
        "shelves": "Купе: 2 нижние + 2 верхние полки. Плацкарт: 2+2 в отсеке + 2 боковые.",
        "sockets": "220В и USB-порты у каждого места"
    }

    if "ласточка" in subtype_l or "ласточка" in title_l or (700 <= num_int <= 749 and ("досс" in carrier_l or "фпк" in carrier_l)):
        res["category"] = "Скоростной электропоезд «Ласточка»"
        if 715 <= num_int <= 744 or "премиум" in subtype_l:
            res["model"] = "ЭС2ГП («Ласточка-Премиум») / ЭС1П"
            res["composition"] = "5 или 10 вагонов (сдвоенный состав)"
            res["shelves"] = "Сидячий салон. Верхние полки с разрывами над частью рядов под мониторы."
            res["sockets"] = "Розетки 220В под сиденьями (классы 1Р и 2Р)"
        else:
            res["model"] = "ЭС2Г / ЭС1 («Ласточка» стандарт)"
            res["composition"] = "5 или 10 вагонов"
            res["shelves"] = "Сидячий салон (схема 3+2). Верхние полки сплошные."
            res["sockets"] = "Ограниченно (в торцах вагонов)"

    elif 751 <= num_int <= 788 and "досс" in carrier_l:
        res["category"] = "Высокоскоростной поезд «Сапсан»"
        res["model"] = "ЭВС1 / ЭВС2 (Siemens Velaro RUS)"
        res["composition"] = "10 или 20 вагонов"
        res["shelves"] = "Сидячий салон. Полки авиационного типа + стеллажи для багажа."
        res["sockets"] = "Индивидуальные розетки 220В у каждого кресла"

    elif "фпк" in carrier_l or "гранд" in carrier_l:
        if num_int in {23, 24, 25, 26, 35, 36, 49, 50, 63, 64, 73, 74, 101, 102, 103, 104}:
            res["category"] = "Скорый двухэтажный поезд"
            res["model"] = "Двухэтажные вагоны ТВЗ (мод. 61-4465)"
            res["composition"] = "12–15 двухэтажных вагонов"
            res["shelves"] = "Купе (2 нижние + 2 верхние полки). Багажных полок под потолком нет."
            res["sockets"] = "220В над столиком + USB у верхних полок"

    return res


@app.get("/api/suggest")
async def suggest_trains(q: str = Query(..., min_length=1)):
    clean_q = q.strip()
    results = []
    seen_uids = set()

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT number, uid, title, carrier FROM train_index WHERE number LIKE ?",
            (f"%{clean_q}%",)
        ).fetchall()
        for r in rows:
            seen_uids.add(r[1])
            results.append({"number": r[0], "uid": r[1], "title": r[2], "carrier": r[3]})

    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        try:
            resp = await client.get("https://rasp.yandex.ru/search/train", params={"number": clean_q}, headers={"User-Agent": "Mozilla/5.0"})
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                for a in soup.find_all("a", href=re.compile(r"/thread/")):
                    match = re.search(r"/thread/([^/?#]+)", a.get("href", ""))
                    if not match:
                        continue
                    uid = match.group(1)
                    if uid in seen_uids:
                        continue
                    text = " ".join(a.get_text(" ", strip=True).split())
                    if not text:
                        continue
                    num_match = re.match(r"^([0-9]{1,4}[А-Яа-яA-Za-z/]*)\s+(.*)$", text)
                    t_num, t_title = (num_match.group(1), num_match.group(2)) if num_match else (clean_q.upper(), text)
                    
                    seen_uids.add(uid)
                    results.append({"number": t_num, "uid": uid, "title": t_title, "carrier": "РЖД"})
        except Exception:
            pass

    return {"results": results[:15]}


@app.get("/api/train")
async def get_train_details(
    uid: str = Query(""),
    number: str = Query(""),
    date: str = Query("")
):
    if not YANDEX_API_KEY:
        raise HTTPException(status_code=500, detail="Не задан YANDEX_API_KEY")

    target_date = date or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{uid}_{number}_{target_date}"

    cached = get_cached_thread(cache_key)
    if cached:
        cached = apply_overrides(cached.get("uid", ""), cached)
        cached["from_cache"] = True
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        if uid:
            resp = await client.get(
                f"{BASE_URL}/thread/",
                params={"apikey": YANDEX_API_KEY, "uid": uid, "date": target_date, "show_systems": "all"}
            )
            if resp.status_code != 200:
                resp = await client.get(f"{BASE_URL}/thread/", params={"apikey": YANDEX_API_KEY, "uid": uid, "show_systems": "all"})
            if resp.status_code != 200:
                raise HTTPException(status_code=404, detail="Поезд не найден в API")

            t_data = resp.json()
            stops = t_data.get("stops", [])
            first_stop = stops[0] if stops else {}
            last_stop = stops[-1] if stops else {}

            full_number = t_data.get("number", number or "Б/Н")
            route_title = t_data.get("title", "Маршрут")
            carrier_title = (t_data.get("carrier") or {}).get("title", "РЖД")
            subtype_title = (t_data.get("transport_subtype") or {}).get("title", "Поезд")
            days_str = t_data.get("days", "По расписанию")

            dep_station = (first_stop.get("station") or {}).get("title", route_title.split("—")[0].strip())
            arr_station = (last_stop.get("station") or {}).get("title", route_title.split("—")[-1].strip())

            dep_time = (first_stop.get("departure") or t_data.get("start_time") or "")[11:16]
            arr_time = (last_stop.get("arrival") or "")[11:16]
            dep_platform = first_stop.get("platform") or "Уточняется на вокзале"
            arr_platform = last_stop.get("platform") or "По факту прибытия"

            total_sec = last_stop.get("duration") or 0
            hours, mins = divmod(int(total_sec // 60), 60)
            duration_str = f"{hours} ч {mins} мин" if total_sec else "По графику"
        else:
            raise HTTPException(status_code=400, detail="Укажите UID поезда")

        with sqlite3.connect(DB_PATH) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO train_index (number, uid, title, carrier) VALUES (?, ?, ?, ?)",
                (full_number, uid, route_title, carrier_title)
            )

        stock_info = detect_rolling_stock(full_number, route_title, carrier_title, subtype_title)

        result = {
            "number": full_number,
            "uid": uid,
            "category": stock_info["category"],
            "route_title": route_title,
            "dep_station": dep_station,
            "arr_station": arr_station,
            "dep_time": dep_time or "—",
            "arr_time": arr_time or "—",
            "duration": duration_str,
            "dep_platform": dep_platform,
            "arr_platform": arr_platform,
            "carrier": carrier_title,
            "days": days_str,
            "model": stock_info["model"],
            "composition": stock_info["composition"],
            "shelves": stock_info["shelves"],
            "sockets": stock_info["sockets"],
            "from_cache": False,
            "is_override": False
        }

        result = apply_overrides(uid, result)
        set_cached_thread(cache_key, result)
        return result


@app.post("/api/admin/login")
async def admin_login(payload: dict = Body(...)):
    key = payload.get("key", "")
    if key == ADMIN_SECRET_KEY:
        return {"status": "ok", "token": ADMIN_SECRET_KEY}
    raise HTTPException(status_code=403, detail="Неверный секретный ключ")


@app.post("/api/admin/update")
async def admin_update_train(payload: dict = Body(...), x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен: неверный ключ")

    uid = payload.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="Не указан UID поезда")

    category = payload.get("category")
    model = payload.get("model")
    composition = payload.get("composition")
    shelves = payload.get("shelves")
    sockets = payload.get("sockets")
    dep_platform = payload.get("dep_platform")
    arr_platform = payload.get("arr_platform")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO train_overrides (uid, category, model, composition, shelves, sockets, dep_platform, arr_platform)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, category, model, composition, shelves, sockets, dep_platform, arr_platform))
        conn.execute("DELETE FROM thread_cache WHERE cache_key LIKE ?", (f"{uid}%",))

    return {"status": "success", "message": "Данные поезда успешно обновлены в базе"}


app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")

@app.get("/admin")
async def serve_admin():
    return FileResponse("static/admin.html")