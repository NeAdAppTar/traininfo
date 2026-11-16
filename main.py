import json
import os
import re
import sqlite3
import time
from datetime import datetime
import httpx
from bs4 import BeautifulSoup
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

load_dotenv()

YANDEX_API_KEY = os.getenv("YANDEX_API_KEY", "")
BASE_URL = "https://api.rasp.yandex.net/v3.0"
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache.db")

app = FastAPI(title="Train Info Service")


# --- Инициализация SQLite кэша (экономия лимитов API) ---
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


# --- Анализатор подвижного состава и полок ---
def detect_rolling_stock(number: str, title: str, carrier: str, subtype: str) -> dict:
    num_digits = re.sub(r"\D", "", number)
    num_int = int(num_digits) if num_digits else 0
    title_l = title.lower()
    carrier_l = carrier.lower()
    subtype_l = subtype.lower()

    # Дефолтные значения
    res = {
        "category": subtype or "Пассажирский поезд",
        "model": "Вагоны ТВЗ (стандартный модельный ряд)",
        "composition": "От 9 до 16 вагонов (по сезону)",
        "shelves": "Купе: 2 нижние + 2 верхние полки (по 2 с каждой стороны). Плацкарт: 2+2 в отсеке + 2 боковые. Сверху — багажная ниша.",
        "sockets": "220В и USB-порты (в вагонах нового модельного ряда у каждого места)"
    }

    # 1. Электропоезда «Ласточка» (ЭС2ГП / ЭС2Г / ЭС1 / ЭС1П)
    if "ласточка" in subtype_l or "ласточка" in title_l or (700 <= num_int <= 749 and ("досс" in carrier_l or "фпк" in carrier_l)):
        res["category"] = "Скоростной электропоезд «Ласточка»"
        if 715 <= num_int <= 744 or "премиум" in subtype_l:
            res["model"] = "ЭС2ГП («Ласточка-Премиум») / ЭС1П (Siemens / Уральские локомотивы)"
            res["composition"] = "5 или 10 вагонов (сдвоенный состав)"
            res["shelves"] = "Сидячий салон. ВНИМАНИЕ: в верхних багажных полках есть РАЗРЫВЫ над частью рядов (в классах 2Р и 2С) из-за потолочных мониторов."
            res["sockets"] = "Розетки 220В расположены под сиденьями почти у каждого ряда (классы 1Р и 2Р)"
        else:
            res["model"] = "ЭС2Г / ЭС1 («Ласточка» стандартной компоновки)"
            res["composition"] = "5 или 10 вагонов"
            res["shelves"] = "Сидячий салон (схема 3+2). Верхние полки СПЛОШНЫЕ по всей длине вагона без разрывов."
            res["sockets"] = "Ограниченное количество (на стенах у торцов вагона и в отдельных рядах)"

    # 2. Высокоскоростные поезда «Сапсан»
    elif 751 <= num_int <= 788 and "досс" in carrier_l:
        res["category"] = "Высокоскоростной поезд «Сапсан»"
        res["model"] = "ЭВС1 / ЭВС2 (Siemens Velaro RUS)"
        res["composition"] = "10 или 20 вагонов (сдвоенный состав)"
        res["shelves"] = "Сидячий салон. Сплошные верхние полки авиационного типа + отдельные стеллажи в торцах вагона."
        res["sockets"] = "Индивидуальные розетки 220В у каждого кресла во всех классах"

    # 3. Электропоезда «Финист» / ЭП2Д / РА-3 «Орлан» (пригород и экспрессы)
    elif 800 <= num_int <= 899 or 6000 <= num_int <= 7999:
        res["category"] = subtype if subtype else "Пригородный поезд / Экспресс"
        if "пк чернноземье" in carrier_l or "черноземье" in carrier_l:
            res["model"] = "РА-3 «Орлан» (рельсовый автобус) или ЭП2Д / ЭД9М"
            res["composition"] = "2–3 вагона (РА-3) или 4–6 вагонов (электропоезд)"
            res["shelves"] = "Сидячие места, сплошные верхние полки над окнами."
            res["sockets"] = "USB-разъемы под сиденьями (в составах РА-3 и ЭП2Д)"
        else:
            res["model"] = "Электропоезд (ЭП2Д / ЭС2Г / ЭС104 «Финист»)"
            res["composition"] = "От 5 до 11 вагонов"
            res["shelves"] = "Сидячий салон, сплошные верхние полки без разрывов."
            res["sockets"] = "USB-разъемы (в новых составах ЭП2Д/Финист)"

    # 4. Двухэтажные поезда ФПК и вагоны габарита RIC (Siemens / ТВЗ 61-4476)
    elif "фпк" in carrier_l or "гранд" in carrier_l:
        # Номера типичных двухэтажных поездов РФ
        two_story_nums = {23, 24, 25, 26, 35, 36, 49, 50, 63, 64, 73, 74, 101, 102, 103, 104, 151, 152}
        # Маршруты, где встречаются купейные вагоны габарита RIC (ТВЗ 61-4476 совместно с Siemens)
        ric_nums = {17, 18, 21, 22, 27, 28, 29, 30, 159, 160}

        if num_int in two_story_nums:
            res["category"] = "Скорый двухэтажный поезд"
            res["model"] = "Двухэтажные вагоны ТВЗ (мод. 61-4465 / 61-4472)"
            res["composition"] = "12–15 двухэтажных вагонов"
            res["shelves"] = "Купе (2 нижние + 2 верхние полки). Третьих (багажных) полок под потолком НЕТ — вещи размещаются только в нишах под нижними полками. На 2-м этаже скос потолка у верхней полки."
            res["sockets"] = "220В над столиком + USB-разъемы у верхних полок"
        elif num_int in ric_nums:
            res["category"] = "Фирменный / Скорый поезд"
            res["model"] = "Вагоны ТВЗ (в состав могут входить вагоны габарита RIC мод. 61-4476 ТВЗ/Siemens)"
            res["composition"] = "10–16 вагонов"
            res["shelves"] = "В вагонах RIC (61-4476, 32 места): классические 4 полки в купе (нижняя и верхняя с каждой стороны), а под откидной крышкой столика у окна встроена раковина-умывальник."
            res["sockets"] = "Индивидуальные розетки 220В и панель климат-контроля в каждом купе"
        elif 1 <= num_int <= 150:
            res["category"] = "Скорый / Фирменный поезд дальнего следования"
            res["model"] = "Одноэтажные вагоны ТВЗ (в т.ч. двухвагонные сцепы мод. 61-4516 / 61-4517)"
            res["composition"] = "12–17 вагонов"
            res["shelves"] = "Классическая компоновка: 2 нижние и 2 верхние полки в купе/отсеке + просторная третья багажная полка сверху."
            res["sockets"] = "Розетки 220В под столом и USB-порты у верхних полок, биотуалеты с душем"

    return res


# --- Бесплатный автопоиск по номеру поезда (без траты лимита API!) ---
@app.get("/api/suggest")
async def suggest_trains(q: str = Query(..., min_length=1, description="Номер поезда")):
    clean_q = q.strip()
    results = []
    seen_uids = set()

    # 1. Сначала проверяем локальный индекс уже известных поездов
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT number, uid, title, carrier FROM train_index WHERE number LIKE ?",
            (f"%{clean_q}%",)
        ).fetchall()
        for r in rows:
            seen_uids.add(r[1])
            results.append({"number": r[0], "uid": r[1], "title": r[2], "carrier": r[3]})

    # 2. Обращаемся к внутреннему подсказчику и веб-поиску ниток Яндекса (0 трат API-лимита)
    async with httpx.AsyncClient(timeout=10.0, follow_redirects=True) as client:
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"
        }
        try:
            # Запрос к странице поиска нитки по номеру на rasp.yandex.ru
            search_url = "https://rasp.yandex.ru/search/train"
            resp = await client.get(search_url, params={"number": clean_q}, headers=headers)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.text, "html.parser")
                # Ищем ссылки вида /thread/UID
                for a in soup.find_all("a", href=re.compile(r"/thread/")):
                    href = a.get("href", "")
                    match = re.search(r"/thread/([^/?#]+)", href)
                    if not match:
                        continue
                    uid = match.group(1)
                    if uid in seen_uids:
                        continue
                    text = " ".join(a.get_text(" ", strip=True).split())
                    if not text:
                        continue
                    # Выделяем номер с буквой и маршрут
                    num_match = re.match(r"^([0-9]{1,4}[А-Яа-яA-Za-z/]*)\s+(.*)$", text)
                    if num_match:
                        t_num, t_title = num_match.group(1), num_match.group(2)
                    else:
                        t_num, t_title = clean_q.upper(), text

                    seen_uids.add(uid)
                    results.append({
                        "number": t_num,
                        "uid": uid,
                        "title": t_title,
                        "carrier": "РЖД / Перевозчик РФ"
                    })
        except Exception:
            pass

    return {"results": results[:15]}


# --- Получение полной карточки поезда (1 запрос к API + SQLite кэш) ---
@app.get("/api/train")
async def get_train_details(
    uid: str = Query("", description="UID нитки поезда"),
    from_city: str = Query("", description="Станция отправления (резервный поиск)"),
    to_city: str = Query("", description="Станция прибытия (резервный поиск)"),
    number: str = Query("", description="Номер поезда"),
    date: str = Query("", description="Дата ГГГГ-ММ-ДД")
):
    if not YANDEX_API_KEY:
        raise HTTPException(status_code=500, detail="Не задан YANDEX_API_KEY в файле .env")

    target_date = date or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{uid}_{from_city}_{to_city}_{number}_{target_date}"

    cached = get_cached_thread(cache_key)
    if cached:
        cached["from_cache"] = True
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Если передан прямой UID нитки — тратим ровно 1 запрос к /thread/
        if uid:
            resp = await client.get(
                f"{BASE_URL}/thread/",
                params={"apikey": YANDEX_API_KEY, "uid": uid, "date": target_date, "show_systems": "all"}
            )
            if resp.status_code != 200:
                # Пробуем без привязки к дате, если в этот день поезд не в ходу
                resp = await client.get(
                    f"{BASE_URL}/thread/",
                    params={"apikey": YANDEX_API_KEY, "uid": uid, "show_systems": "all"}
                )
            if resp.status_code != 200:
                raise HTTPException(status_code=404, detail="Не удалось получить данные нитки из API")

            t_data = resp.json()
            stops = t_data.get("stops", [])
            first_stop = stops[0] if stops else {}
            last_stop = stops[-1] if stops else {}

            full_number = t_data.get("number", number or "Б/Н")
            route_title = t_data.get("title", "Маршрут не указан")
            carrier_title = (t_data.get("carrier") or {}).get("title", "РЖД / ФПК / ДОСС")
            subtype_title = (t_data.get("transport_subtype") or {}).get("title", "Поезд")
            days_str = t_data.get("days", "По расписанию")

            dep_station = (first_stop.get("station") or {}).get("title", route_title.split("—")[0].strip())
            arr_station = (last_stop.get("station") or {}).get("title", route_title.split("—")[-1].strip())

            dep_time_raw = first_stop.get("departure") or t_data.get("start_time") or ""
            arr_time_raw = last_stop.get("arrival") or ""

            dep_time = dep_time_raw[11:16] if len(dep_time_raw) >= 16 else dep_time_raw
            arr_time = arr_time_raw[11:16] if len(arr_time_raw) >= 16 else arr_time_raw

            dep_platform = first_stop.get("platform") or "Уточняется на табло вокзала (за 30 мин)"
            arr_platform = last_stop.get("platform") or "Назначается дежурным по станции"

            total_sec = last_stop.get("duration") or 0
            hours, mins = divmod(int(total_sec // 60), 60)
            duration_str = f"{hours} ч {mins} мин" if total_sec else "По графику"

        # Если ищем по номеру + городам (резервный метод — тоже 1 запрос к /search/)
        elif from_city and to_city:
            s_url = "https://suggests.rasp.yandex.net/all_suggests"
            r1 = (await client.get(s_url, params={"format": "old", "part": from_city, "lang": "ru"})).json()
            r2 = (await client.get(s_url, params={"format": "old", "part": to_city, "lang": "ru"})).json()
            if not (len(r1) > 1 and r1[1] and len(r2) > 1 and r2[1]):
                raise HTTPException(status_code=404, detail="Станции не найдены")

            f_code, t_code = r1[1][0][0], r2[1][0][0]
            resp = await client.get(
                f"{BASE_URL}/search/",
                params={
                    "apikey": YANDEX_API_KEY,
                    "from": f_code,
                    "to": t_code,
                    "date": target_date,
                    "transport_types": "train,suburban"
                }
            )
            if resp.status_code != 200:
                raise HTTPException(status_code=400, detail="Ошибка обращения к API Расписаний")

            segments = resp.json().get("segments", [])
            clean_num = re.sub(r"\D", "", number)
            matched = None
            for seg in segments:
                t_num = seg.get("thread", {}).get("number", "")
                if not number or (clean_num and clean_num in t_num) or (number.lower() in t_num.lower()):
                    matched = seg
                    break

            if not matched:
                raise HTTPException(status_code=404, detail=f"Поезд № {number} не найден на маршруте {from_city} — {to_city}")

            th = matched["thread"]
            uid = th.get("uid", "")
            full_number = th.get("number", "Б/Н")
            route_title = th.get("title", "")
            carrier_title = (th.get("carrier") or {}).get("title", "РЖД")
            subtype_title = (th.get("transport_subtype") or {}).get("title", "Поезд")
            days_str = matched.get("days") or "Ежедневно / по графику"

            dep_station = (matched.get("from") or {}).get("title", from_city)
            arr_station = (matched.get("to") or {}).get("title", to_city)
            dep_time = (matched.get("departure") or "")[11:16]
            arr_time = (matched.get("arrival") or "")[11:16]
            dep_platform = matched.get("departure_platform") or "Назначается за 30–60 мин до отправления"
            arr_platform = matched.get("arrival_platform") or "По факту прибытия"

            dur_min = int((matched.get("duration") or 0) // 60)
            hours, mins = divmod(dur_min, 60)
            duration_str = f"{hours} ч {mins} мин"
        else:
            raise HTTPException(status_code=400, detail="Выберите поезд из подсказок или укажите города маршрута")

        # Сохраняем поезд в локальный индекс для мгновенных подсказок в будущем
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
            "from_cache": False
        }

        set_cached_thread(cache_key, result)
        return result


# Раздача статики (фронтенда)
app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")