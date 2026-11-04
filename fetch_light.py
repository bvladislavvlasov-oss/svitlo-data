"""Забирає графік відключень ДТЕК КРЕМ для однієї черги і зберігає light.json."""
import asyncio
import datetime as dt
import json
import os
from zoneinfo import ZoneInfo

from curl_cffi.requests import AsyncSession
from dtek_client import DtekClient
from dtek_client.browser_auth import get_cleared_cookies
from dtek_client.exceptions import DtekDataError
from dtek_client.models import FactSchedule

BASE = "https://www.dtek-krem.com.ua"
PAGE = f"{BASE}/ua/shutdowns"
CITY = os.environ["DTEK_CITY"]          # напр. "с. Чабани" — як на сайті ДТЕК
STREET = os.environ["DTEK_STREET"]      # напр. "вул. ..." — як на сайті ДТЕК
GROUP = os.environ.get("DTEK_GROUP", "GPV2.2")
PUMP_GROUP = os.environ.get("DTEK_PUMP_GROUP", "GPV1.2")  # насосна — від неї залежить вода
KYIV = ZoneInfo("Europe/Kyiv")



def half_hours(slots: dict) -> list[str]:
    """48 значень на добу: on / off / maybe / unknown (кожне — 30 хв)."""
    keys = sorted(slots, key=int)
    values = [slots[k].value for k in keys]
    out: list[str] = []
    if len(values) == 48:
        for v in values:
            out.append("off" if v in ("no", "first", "second") else
                       "maybe" if v in ("maybe", "mfirst", "msecond") else
                       "on" if v == "yes" else "unknown")
        return out
    for v in values:  # 24 погодинні клітинки з половинками
        first = {"no": "off", "first": "off", "maybe": "maybe", "mfirst": "maybe",
                 "yes": "on", "second": "on", "msecond": "on"}.get(v, "unknown")
        second = {"no": "off", "second": "off", "maybe": "maybe", "msecond": "maybe",
                  "yes": "on", "first": "on", "mfirst": "on"}.get(v, "unknown")
        out += [first, second]
    return out


def intervals(hh: list[str]) -> list[dict]:
    """Склеює півгодинки у проміжки відключень."""
    res, i = [], 0
    while i < len(hh):
        if hh[i] in ("off", "maybe"):
            j = i
            while j < len(hh) and hh[j] == hh[i]:
                j += 1
            fmt = lambda n: f"{n // 2:02d}:{30 * (n % 2):02d}"
            res.append({"start": fmt(i), "end": fmt(j), "maybe": hh[i] == "maybe"})
            i = j
        else:
            i += 1
    return res


async def main() -> None:
    cookies, csrf = await get_cleared_cookies(PAGE)
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "uk,en;q=0.9",
        "Origin": BASE,
        "Referer": PAGE,
        "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    }
    if csrf:
        headers["X-CSRF-Token"] = csrf
    session = AsyncSession(timeout=20.0, headers=headers, cookies=cookies, impersonate="chrome120")

    fact = None
    async with DtekClient("krem", ajax_url=f"{BASE}/ua/ajax", session=session) as client:
        now = dt.datetime.now(KYIV).strftime("%d.%m.%Y %H:%M")
        # Спроба 1: за адресою (як робить сайт), з updateFact
        try:
            resp = await client.get_home_num(CITY, STREET, update_fact=now)
            fact = resp.fact
            print("getHomeNum OK, будинків:", len(resp.houses))
        except DtekDataError as exc:
            print("getHomeNum помилка:", str(getattr(exc, "raw", ""))[:800])
            # Діагностика: як сайт пише населений пункт
            try:
                raw = await client._post({"method": "getStreets"})
                cities = raw.get("streets", {}) if isinstance(raw, dict) else {}
                similar = [c for c in cities if "Чабан" in c]
                print("Схожі нас. пункти:", similar)
                for c in similar:
                    print(c, "→ вулиці з 'Машин':", [s for s in cities[c] if "Машин" in s])
            except Exception as e2:  # noqa: BLE001
                print("getStreets помилка:", e2)
        # Спроба 2: загальний графік без адреси
        if fact is None:
            raw = await client._post({"method": "checkDisconUpdate", "update": "01.01.2000 00:00"})
            print("checkDisconUpdate ключі:", list(raw.keys()) if isinstance(raw, dict) else type(raw))
            if isinstance(raw, dict) and isinstance(raw.get("fact"), dict):
                fact = FactSchedule.model_validate(raw["fact"])

    def build_days(group: str) -> list[dict]:
        out = []
        if not fact:
            return out
        for ts, groups in sorted(fact.days.items(), key=lambda x: int(x[0])):
            slots = groups.get(group)
            if not slots:
                continue
            hh = half_hours(slots)
            out.append({
                "date": dt.datetime.fromtimestamp(int(ts), KYIV).date().isoformat(),
                "halfHours": hh,
                "outages": intervals(hh),
            })
        return out

    days = build_days(GROUP)
    pump_days = build_days(PUMP_GROUP)

    data = {
        "group": GROUP,
        "sourceUpdated": fact.update if fact else None,
        "days": days,
        "pump": {"group": PUMP_GROUP, "days": pump_days},
    }
    with open("light.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    print(json.dumps(data, ensure_ascii=False)[:500])


if __name__ == "__main__":
    asyncio.run(main())
