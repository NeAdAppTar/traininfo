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
        conn.execute("""
            CREATE TABLE IF NOT EXISTS station_platform_overrides (
                uid TEXT,
                station_title TEXT,
                platform TEXT,
                PRIMARY KEY (uid, station_title)
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS train_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                model TEXT NOT NULL,
                composition TEXT,
                shelves TEXT,
                sockets TEXT,
                match_keywords TEXT DEFAULT '',
                match_numbers TEXT DEFAULT '',
                description TEXT DEFAULT ''
            )
        """)

        # Базовые унифицированные типы поездов и вагонов, если таблица пустая
        count = conn.execute("SELECT COUNT(*) FROM train_types").fetchone()[0]
        if count == 0:
            default_types = [
                (
                    "Скоростной электропоезд «Ласточка-Премиум» ЭС2ГП",
                    "Скоростной электропоезд",
                    "ЭС2ГП постоянного тока",
                    "5 вагонов или сдвоенный состав 10 вагонов",
                    "Комфортный сидячий салон первого и второго классов. Полки для ручной клади над креслами.",
                    "Индивидуальные розетки 220В и порты USB под каждым креслом",
                    "ласточка, премиум, ласточка-премиум, эс2гп",
                    "715-744",
                    "Межрегиональный скоростной экспресс повышенной комфортности постоянного тока"
                ),
                (
                    "Скоростной электропоезд «Ласточка-Премиум» ЭС1П",
                    "Скоростной электропоезд",
                    "ЭС1П двухсистемный",
                    "5 вагонов или сдвоенный состав 10 вагонов",
                    "Сидячий салон бизнес и эконом классов. Вместительные полки для ручной клади и багажные стойки.",
                    "Розетки 220В и разъемы USB у каждого пассажирского места",
                    "эс1п, премиум двухсистемный",
                    "715-744",
                    "Двухсистемный межрегиональный экспресс для участков постоянного и переменного тока"
                ),
                (
                    "Скоростной электропоезд «Ласточка-Стандарт» ЭС2Г",
                    "Скоростной электропоезд",
                    "ЭС2Г стандартной компоновки",
                    "5 вагонов или сдвоенный состав 10 вагонов",
                    "Пригородный сидячий салон со схемой сидений 3 плюс 2. Продольные багажные полки вдоль всего вагона.",
                    "Розетки 220В расположены в торцевых зонах вагонов",
                    "ласточка, эс2г",
                    "701-714, 745-749",
                    "Базовая пригородная и межобластная модификация Ласточки"
                ),
                (
                    "Скоростной электропоезд «Ласточка» ЭС1",
                    "Скоростной электропоезд",
                    "ЭС1 двухсистемная базовая модель",
                    "5 вагонов",
                    "Стандартный сидячий салон немецкого производства с багажными полками над креслами.",
                    "Несколько розеток 220В в начале и конце салона",
                    "эс1",
                    "701-714",
                    "Первое поколение скоростных поездов Ласточка"
                ),
                (
                    "Скоростной электропоезд «Финист» ЭС104",
                    "Скоростной электропоезд нового поколения",
                    "ЭС104 Финист полностью отечественного производства",
                    "5 вагонов",
                    "Новые эргономичные кресла со столиками. Просторные полки для ручной клади над рядами сидений.",
                    "Индивидуальные порты USB Type-C и Type-A у каждого кресла плюс розетки 220В",
                    "финист, эс104",
                    "701-749",
                    "Полностью импортозамещенный скоростной электропоезд завода Уральские локомотивы"
                ),
                (
                    "Высокоскоростной поезд «Сапсан» ЭВС1",
                    "Высокоскоростной поезд",
                    "ЭВС1 Velaro RUS постоянного тока",
                    "10 вагонов или сдвоенный состав 20 вагонов",
                    "Эргономичные кресла с регулировкой наклона. Закрытые багажные полки самолетного типа и центральные багажные стойки.",
                    "Индивидуальные розетки 220В и USB у каждого пассажира",
                    "сапсан, эвс1",
                    "751-788",
                    "Высокоскоростной поезд для линии Москва — Санкт-Петербург"
                ),
                (
                    "Высокоскоростной поезд «Сапсан» ЭВС2",
                    "Высокоскоростной поезд",
                    "ЭВС2 Velaro RUS двухсистемный",
                    "10 вагонов или сдвоенный состав 20 вагонов",
                    "Сидячий салон премиального комфорта. Полки авиационного типа и зоны крупногабаритного багажа.",
                    "Индивидуальные розетки 220В под каждым сиденьем",
                    "сапсан, эвс2",
                    "751-788",
                    "Двухсистемный высокоскоростной поезд для маршрутов до Нижнего Новгорода"
                ),
                (
                    "Скорый двухэтажный поезд купейный ТВЗ 61-4465",
                    "Скорый двухэтажный поезд",
                    "ТВЗ 61-4465 двухэтажный купейный",
                    "От 10 до 15 двухэтажных вагонов",
                    "Четырехместные купе на двух этажах. Две нижние и две верхние полки. Багаж размещается под нижними полками.",
                    "Розетки 220В у столика плюс индивидуальные порты USB у верхних полок",
                    "двухэтажный, 61-4465",
                    "23, 24, 25, 26, 35, 36, 49, 50, 63, 64, 73, 74, 101, 102, 103, 104",
                    "Классический двухэтажный купейный вагон постройки Тверского вагоностроительного завода"
                ),
                (
                    "Скорый двухэтажный поезд сидячий ТВЗ 61-4492",
                    "Скоростной двухэтажный экспресс",
                    "ТВЗ 61-4492 двухэтажный с местами для сидения",
                    "От 8 до 12 двухэтажных вагонов",
                    "Сидячие места первого и второго классов на двух этажах. Полки для ручной клади и багажные модули.",
                    "Индивидуальные розетки 220В и USB разъемы в основании кресел",
                    "двухэтажный сидячий, 61-4492",
                    "737, 738, 739, 740, 741, 742",
                    "Дневной двухэтажный экспресс с комфортными поворотными креслами"
                ),
                (
                    "Двухэтажный вагон СВ повышенной комфортности ТВЗ 61-4465.01",
                    "Фирменный двухэтажный поезд",
                    "ТВЗ 61-4465.01 двухэтажный спальный вагон СВ",
                    "Включается в состав скорых двухэтажных поездов",
                    "Двухместные купе с двумя нижними мягкими диванами. ЖК-мониторы, индивидуальные шкафчики.",
                    "Розетки 220В и быстрые зарядки USB у каждого дивана",
                    "двухэтажный св, св двухэтажный",
                    "101-104, 23-26",
                    "Двухэтажный вагон бизнес-класса с двухместным размещением"
                ),
                (
                    "Фирменный купейный вагон со сцепом и душем ТВЗ 61-4517",
                    "Фирменный скорый поезд",
                    "ТВЗ 61-4517 проект 2019 года двухвагонный сцеп",
                    "От 10 до 16 вагонов нового модельного ряда",
                    "Четырехместные купе-трансформеры. Нижние полки превращаются в мягкие диваны. Душевая комната в каждом сцепе.",
                    "Розетки 220В у столика и индивидуальные USB разъемы у каждого спального места",
                    "сцеп 2019, 61-4517, купе с душем",
                    "001-099",
                    "Современный купейный вагон с улучшенной шумоизоляцией, сейфами и душем"
                ),
                (
                    "Новейший плацкартный вагон со шторками ТВЗ 61-4516",
                    "Фирменный пассажирский поезд",
                    "ТВЗ 61-4516 плацкарт со шторками проект 2019 года",
                    "От 10 до 16 вагонов",
                    "54 спальных места. Индивидуальные плотные шторки у каждого места, диваны с подголовниками, светильники.",
                    "Индивидуальные разъемы USB и розетки 220В у каждой полки включая верхние и боковые",
                    "плацкарт со шторками, 61-4516, шторки",
                    "001-099",
                    "Плацкартный вагон со шторками, создающими личное приватное пространство"
                ),
                (
                    "Классический купейный вагон ТВЗ 61-4440",
                    "Пассажирский поезд дальнего следования",
                    "ТВЗ 61-4440 купейный стандартный",
                    "От 8 до 16 вагонов",
                    "Традиционное четырехместное купе. Две нижние полки и две верхние полки с защитными планками.",
                    "Розетки 220В в каждом купе под столом и в коридоре",
                    "купе, 61-4440",
                    "100-399",
                    "Самый массовый и надежный купейный вагон РЖД с кондиционером и экологическими туалетами"
                ),
                (
                    "Классический плацкартный вагон ТВЗ 61-4447",
                    "Пассажирский поезд дальнего следования",
                    "ТВЗ 61-4447 плацкартный стандартный",
                    "От 9 до 18 вагонов",
                    "54 места открытого типа. Четыре полки в отсеке и две боковые полки. Верхние третьи багажные полки.",
                    "Розетки 220В в отсеках вагонов и в коридорах",
                    "плацкарт, 61-4447",
                    "100-699",
                    "Стандартный цельнометаллический плацкартный вагон с системой кондиционирования"
                ),
                (
                    "Спальный вагон Люкс СВ ТВЗ 61-4444",
                    "Фирменный поезд премиум-класса",
                    "ТВЗ 61-4444 вагон повышенной комфортности СВ",
                    "Включается в составы фирменных скорых поездов",
                    "Двухместные купе. Только две нижние широкие мягкие полки, шкаф для одежды, индивидуальный столик.",
                    "Розетки 220В и USB зарядки у каждого спального места",
                    "св люкс, вагон св, 61-4444",
                    "001-030",
                    "Одноэтажный вагон СВ с увеличенным пространством для двух пассажиров"
                ),
                (
                    "Мягкий VIP-вагон Гранд Де Люкс ТВЗ 61-4445",
                    "Премиальный пассажирский поезд",
                    "ТВЗ 61-4445 вагон люкс с индивидуальными санузлами",
                    "Отдельные вагоны в фирменных поездах",
                    "От 4 до 6 просторных апартаментов. Двуспальный диван, верхняя полка, персональная душевая комната и санузел в купе.",
                    "Полная электрификация: розетки 220В, USB, климат-контроль и аудиосистема",
                    "гранд де люкс, мягкий вагон, vip купе",
                    "001-010",
                    "Максимальный уровень комфорта с собственным санузлом и душевой кабиной в купе"
                ),
                (
                    "Скоростной поезд «Невский Экспресс» ТВЗ 61-4179",
                    "Скоростной поезд локомотивной тяги",
                    "ТВЗ 61-4179 сидячие вагоны со скоростью до 200 км в час",
                    "До 12 вагонов",
                    "Шестиместные купе с мягкими креслами либо открытый салон. Полки для ручной клади.",
                    "Розетки 220В в каждом купе",
                    "невский экспресс, 61-4179",
                    "747, 748",
                    "Скоростной поезд Санкт-Петербург — Москва с вагонами локомотивной тяги"
                ),
                (
                    "Дизель-поезд РА-3 «Орлан» двухвагонный",
                    "Рельсовый автобус",
                    "РА-3 Орлан двухвагонной составности",
                    "2 вагона",
                    "Сидячий пригородный салон. Мягкие сиденья с подлокотниками, верхние решетчатые полки для вещей.",
                    "USB разъемы для подзарядки мобильных телефонов у сидений",
                    "орлан 2 вагона, ра-3, ра3",
                    "",
                    "Автономный дизель-поезд для участков без контактной сети"
                ),
                (
                    "Дизель-поезд РА-3 «Орлан» трехвагонный",
                    "Рельсовый автобус повышенной вместимости",
                    "РА-3 Орлан трехвагонной составности",
                    "3 вагона",
                    "Сидячий салон с увеличенной площадью для багажа и велосипедов. Багажные полки вдоль стен.",
                    "USB порты у блоков кресел",
                    "орлан 3 вагона, ра-3 трехвагонный",
                    "",
                    "Трехвагонный рельсовый автобус для загруженных пригородных неэлектрифицированных линий"
                ),
                (
                    "Городской электропоезд МЦД «Иволга 4.0» ЭГЭ2Тв",
                    "Городской электропоезд МЦД",
                    "ЭГЭ2Тв Иволга четвертого поколения",
                    "11 вагонов бестамбурной компоновки",
                    "Салон с тремя дверьми на вагон для быстрой посадки. Подвесные полки и стойки для велосипедов и самокатов.",
                    "Беспроводные зарядные панели на столиках плюс разъемы Type-A и Type-C у каждого сиденья",
                    "иволга 4, иволга 4.0, эгэ2тв",
                    "",
                    "Новейший поезд для линий Московских центральных диаметров с тремя дверями"
                ),
                (
                    "Городской электропоезд МЦД «Иволга 3.0» ЭГЭ2Тв",
                    "Городской электропоезд МЦД",
                    "ЭГЭ2Тв Иволга третьего поколения",
                    "11 вагонов",
                    "Городской салон без тамбуров с мягкими креслами с поддержкой. Полки для ручной клади.",
                    "USB разъемы Type-A и Type-C в каждом блоке кресел",
                    "иволга 3, иволга 3.0",
                    "",
                    "Скоростной городской электропоезд МЦД с повышенным ускорением"
                ),
                (
                    "Пригородный электропоезд ЭП2Д постоянного тока",
                    "Пригородный электропоезд",
                    "ЭП2Д производства Демиховского машиностроительного завода",
                    "От 4 до 11 вагонов",
                    "Классический пригородный салон с диванами 3 плюс 3 или креслами 2 плюс 2. Верхние продольные полки для багажа.",
                    "Розетки 220В и USB разъемы в головных и моторных вагонах",
                    "эп2д",
                    "",
                    "Современный пригородный поезд постоянного тока с плавным ходом и климат-контролем"
                ),
                (
                    "Пригородный электропоезд ЭП3Д переменного тока",
                    "Пригородный электропоезд",
                    "ЭП3Д переменного тока производства ДМЗ",
                    "От 4 до 8 вагонов",
                    "Удобный салон с мягкими сиденьями, подлокотниками и багажными полками.",
                    "USB разъемы и розетки 220В в салоне",
                    "эп3д",
                    "",
                    "Пригородный электропоезд для регионов с электрификацией переменным током"
                ),
                (
                    "Ретропоезд «Рускеальский экспресс»",
                    "Туристический ретропоезд на паровозной тяге",
                    "Аммендорф в стилистике Николаевского экспресса конца XIX века",
                    "От 5 до 8 тематических вагонов",
                    "Винтажные купе с бархатной обивкой диванов, латунными светильниками и деревянной отделкой.",
                    "Скрытые розетки 220В и USB стилизованные под винтаж",
                    "рускеальский экспресс, ретропоезд, рускеала",
                    "921, 922, 923, 924",
                    "Единственный в России ежедневный поезд на паровозной тяге Сортавала — Горный парк Рускеала"
                ),
                (
                    "Фирменный поезд «Красная Стрела»",
                    "Исторический фирменный скорый поезд",
                    "ТВЗ эксклюзивной комплектации в красной ливрее",
                    "16 вагонов купе, СВ и вагон-ресторан",
                    "Купе и СВ премиальной отделки с ковровым покрытием, индивидуальными диванами и постельным бельем высшего качества.",
                    "Индивидуальные розетки 220В и USB порты у каждого спального места",
                    "красная стрела",
                    "001, 002",
                    "Легендарный фирменный поезд номер 1 Москва — Санкт-Петербург"
                ),
                (
                    "Фирменный поезд «Гранд Экспресс»",
                    "Частный поезд-отель гранд-люкс",
                    "Эксклюзивные вагоны Гранд Сервис Экспресс",
                    "12 вагонов категорий Гранд, Премиум, СВ и Купе",
                    "Купе-номера с широкими диванами, индивидуальным кондиционером, душевыми кабинами и халатами.",
                    "Розетки 220В, USB зарядки, Wi-Fi и цифровая панель управления климатом",
                    "гранд экспресс, частный поезд",
                    "053, 054",
                    "Комфортабельный поезд-отель между Москвой и Санкт-Петербургом"
                )
            ]
            conn.executemany("""
                INSERT INTO train_types (name, category, model, composition, shelves, sockets, match_keywords, match_numbers, description)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, default_types)

init_db()


def get_cached_thread(key: str, ttl_seconds: int = 21600) -> dict | None:
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
            if row[0]:
                data["category"] = row[0]
            if row[1]:
                data["model"] = row[1]
            if row[2]:
                data["composition"] = row[2]
            if row[3]:
                data["shelves"] = row[3]
            if row[4]:
                data["sockets"] = row[4]
            if row[5]:
                data["dep_platform"] = row[5]
            if row[6]:
                data["arr_platform"] = row[6]
            data["is_override"] = True

        plat_rows = conn.execute(
            "SELECT station_title, platform FROM station_platform_overrides WHERE uid = ?",
            (uid,)
        ).fetchall()
        plat_map = {r[0]: r[1] for r in plat_rows}

        if plat_map and "stops" in data:
            for st in data["stops"]:
                if st["station"] in plat_map:
                    st["platform"] = plat_map[st["station"]]

    return data


def parse_number_ranges(rule_str: str) -> list[tuple[int, int]]:
    ranges = []
    for part in (rule_str or "").split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            sub = part.split("-", 1)
            try:
                s_digits = re.sub(r"\D", "", sub[0])
                e_digits = re.sub(r"\D", "", sub[1])
                if s_digits and e_digits:
                    s, e = int(s_digits), int(e_digits)
                    ranges.append((min(s, e), max(s, e)))
            except ValueError:
                pass
        else:
            try:
                v_digits = re.sub(r"\D", "", part)
                if v_digits:
                    val = int(v_digits)
                    ranges.append((val, val))
            except ValueError:
                pass
    return ranges


def detect_rolling_stock(number: str, title: str, carrier: str, subtype: str) -> dict:
    num_digits = re.sub(r"\D", "", number)
    num_int = int(num_digits) if num_digits else 0
    title_l = (title or "").lower()
    carrier_l = (carrier or "").lower()
    subtype_l = (subtype or "").lower()

    # Загружаем унифицированные типы из базы данных
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        types = conn.execute("SELECT * FROM train_types ORDER BY id ASC").fetchall()

    # 1. Сначала проверка: совпадение и по номерам, и по ключевым словам (максимально точное)
    for t in types:
        match_kw = [k.strip().lower() for k in (t["match_keywords"] or "").split(",") if k.strip()]
        kw_matched = any(kw in title_l or kw in carrier_l or kw in subtype_l for kw in match_kw) if match_kw else False

        num_ranges = parse_number_ranges(t["match_numbers"])
        num_matched = any(start <= num_int <= end for start, end in num_ranges) if (num_ranges and num_int > 0) else False

        if match_kw and num_ranges and kw_matched and num_matched:
            return {
                "category": t["category"],
                "model": t["model"],
                "composition": t["composition"] or "",
                "shelves": t["shelves"] or "",
                "sockets": t["sockets"] or ""
            }

    # 2. Совпадение по диапазону / списку номеров
    for t in types:
        num_ranges = parse_number_ranges(t["match_numbers"])
        if num_ranges and num_int > 0 and any(start <= num_int <= end for start, end in num_ranges):
            return {
                "category": t["category"],
                "model": t["model"],
                "composition": t["composition"] or "",
                "shelves": t["shelves"] or "",
                "sockets": t["sockets"] or ""
            }

    # 3. Совпадение по ключевым словам
    for t in types:
        match_kw = [k.strip().lower() for k in (t["match_keywords"] or "").split(",") if k.strip()]
        if match_kw and any(kw in title_l or kw in carrier_l or kw in subtype_l for kw in match_kw):
            return {
                "category": t["category"],
                "model": t["model"],
                "composition": t["composition"] or "",
                "shelves": t["shelves"] or "",
                "sockets": t["sockets"] or ""
            }

    # 4. Резервный дефолтный тип
    for t in types:
        name_l = (t["name"] or "").lower()
        if "стандартный" in name_l or "пассажирский" in name_l:
            return {
                "category": t["category"] or subtype or "Пассажирский поезд",
                "model": t["model"],
                "composition": t["composition"] or "От 9 до 16 вагонов по сезону",
                "shelves": t["shelves"] or "Купе: 2 нижние и 2 верхние полки. Плацкарт: 2 плюс 2 в отсеке и 2 боковые полки.",
                "sockets": t["sockets"] or "220В и USB-порты у каждого места"
            }

    return {
        "category": subtype or "Пассажирский поезд",
        "model": "Вагоны ТВЗ стандартного модельного ряда",
        "composition": "От 9 до 16 вагонов по сезону",
        "shelves": "Купе: 2 нижние и 2 верхние полки. Плацкарт: 2 плюс 2 в отсеке и 2 боковые полки.",
        "sockets": "220В и USB-порты у каждого места"
    }


# ==========================================================
# API ЭНДПОИНТЫ: СПРАВОЧНИК ТИПОВ ПОЕЗДОВ И ВАГОНОВ
# ==========================================================

@app.get("/api/train-types")
async def get_train_types():
    with sqlite3.connect(DB_PATH) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM train_types ORDER BY id ASC").fetchall()
        return [dict(r) for r in rows]


@app.post("/api/admin/train-types")
async def create_train_type(payload: dict = Body(...), x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    name = (payload.get("name") or "").strip()
    category = (payload.get("category") or "").strip()
    model = (payload.get("model") or "").strip()

    if not name or not category or not model:
        raise HTTPException(status_code=400, detail="Заполните обязательные поля: Название, Категория, Модель")

    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute("""
            INSERT INTO train_types (name, category, model, composition, shelves, sockets, match_keywords, match_numbers, description)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            name,
            category,
            model,
            payload.get("composition", "").strip(),
            payload.get("shelves", "").strip(),
            payload.get("sockets", "").strip(),
            payload.get("match_keywords", "").strip(),
            payload.get("match_numbers", "").strip(),
            payload.get("description", "").strip()
        ))
        new_id = cur.lastrowid
        # Очищаем кэш запросов, чтобы переопределения сразу применились
        conn.execute("DELETE FROM thread_cache")

    return {"status": "ok", "id": new_id}


@app.put("/api/admin/train-types/{type_id}")
async def update_train_type(type_id: int, payload: dict = Body(...), x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    name = (payload.get("name") or "").strip()
    category = (payload.get("category") or "").strip()
    model = (payload.get("model") or "").strip()

    if not name or not category or not model:
        raise HTTPException(status_code=400, detail="Заполните обязательные поля: Название, Категория, Модель")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            UPDATE train_types
            SET name = ?, category = ?, model = ?, composition = ?, shelves = ?, sockets = ?,
                match_keywords = ?, match_numbers = ?, description = ?
            WHERE id = ?
        """, (
            name,
            category,
            model,
            payload.get("composition", "").strip(),
            payload.get("shelves", "").strip(),
            payload.get("sockets", "").strip(),
            payload.get("match_keywords", "").strip(),
            payload.get("match_numbers", "").strip(),
            payload.get("description", "").strip(),
            type_id
        ))
        # Очищаем кэш запросов
        conn.execute("DELETE FROM thread_cache")

    return {"status": "ok"}


@app.delete("/api/admin/train-types/{type_id}")
async def delete_train_type(type_id: int, x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM train_types WHERE id = ?", (type_id,))
        conn.execute("DELETE FROM thread_cache")

    return {"status": "ok"}


# ==========================================================
# ОСНОВНЫЕ ЭНДПОИНТЫ ПОИСКА И ИНФОРМАЦИИ
# ==========================================================

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
            resp = await client.get(
                "https://rasp.yandex.ru/search/train",
                params={"number": clean_q},
                headers={"User-Agent": "Mozilla/5.0"}
            )
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
    from_city: str = Query(""),
    to_city: str = Query(""),
    date: str = Query("")
):
    if not YANDEX_API_KEY:
        raise HTTPException(status_code=500, detail="Не задан YANDEX_API_KEY")

    target_date = date or datetime.now().strftime("%Y-%m-%d")
    cache_key = f"{uid}_{from_city}_{to_city}_{number}_{target_date}"

    cached = get_cached_thread(cache_key)
    if cached:
        cached = apply_overrides(cached.get("uid", ""), cached)
        cached["from_cache"] = True
        return cached

    async with httpx.AsyncClient(timeout=15.0) as client:
        if not uid and from_city and to_city:
            s_url = "https://suggests.rasp.yandex.net/all_suggests"
            r1 = (await client.get(s_url, params={"format": "old", "part": from_city, "lang": "ru"})).json()
            r2 = (await client.get(s_url, params={"format": "old", "part": to_city, "lang": "ru"})).json()
            if len(r1) > 1 and r1[1] and len(r2) > 1 and r2[1]:
                f_code, t_code = r1[1][0][0], r2[1][0][0]
                s_resp = await client.get(
                    f"{BASE_URL}/search/",
                    params={"apikey": YANDEX_API_KEY, "from": f_code, "to": t_code, "date": target_date, "transport_types": "train,suburban"}
                )
                if s_resp.status_code == 200:
                    for seg in s_resp.json().get("segments", []):
                        th = seg.get("thread", {})
                        t_num = th.get("number", "")
                        if number.lower() in t_num.lower():
                            uid = th.get("uid")
                            break

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
            stops_raw = t_data.get("stops", [])

            formatted_stops = []
            for st in stops_raw:
                st_obj = st.get("station", {})
                arr = st.get("arrival")
                dep = st.get("departure")
                stop_sec = st.get("stop_time")
                stop_min = int(stop_sec // 60) if stop_sec else 0

                formatted_stops.append({
                    "station": st_obj.get("title", ""),
                    "arrival": arr[11:16] if arr else "—",
                    "departure": dep[11:16] if dep else "—",
                    "stop_min": f"{stop_min} мин" if stop_min else "—",
                    "platform": st.get("platform") or "—"
                })

            first_stop = stops_raw[0] if stops_raw else {}
            last_stop = stops_raw[-1] if stops_raw else {}

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
            raise HTTPException(status_code=400, detail="Укажите корректный номер или станции маршрута")

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
            "stops": formatted_stops,
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
    raise HTTPException(status_code=403, detail="Неверный ключ")


@app.post("/api/admin/update")
async def admin_update_train(payload: dict = Body(...), x_admin_key: str = Header(default="")):
    if x_admin_key != ADMIN_SECRET_KEY:
        raise HTTPException(status_code=403, detail="Доступ запрещен")

    uid = payload.get("uid")
    if not uid:
        raise HTTPException(status_code=400, detail="Не указан UID")

    category = payload.get("category")
    model = payload.get("model")
    composition = payload.get("composition")
    shelves = payload.get("shelves")
    sockets = payload.get("sockets")
    dep_platform = payload.get("dep_platform")
    arr_platform = payload.get("arr_platform")
    station_platforms = payload.get("station_platforms", {})

    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("""
            INSERT OR REPLACE INTO train_overrides (uid, category, model, composition, shelves, sockets, dep_platform, arr_platform)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (uid, category, model, composition, shelves, sockets, dep_platform, arr_platform))

        for st_name, plat in station_platforms.items():
            conn.execute("""
                INSERT OR REPLACE INTO station_platform_overrides (uid, station_title, platform)
                VALUES (?, ?, ?)
            """, (uid, st_name, plat))

        # Полная очистка старого кэша для этого UID, чтобы новые данные подтягивались сразу
        conn.execute("DELETE FROM thread_cache WHERE cache_key LIKE ?", (f"{uid}%",))

    return {"status": "success"}


app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_index():
    return FileResponse("static/index.html")


@app.get("/admin")
async def serve_admin():
    return FileResponse("static/admin.html")
