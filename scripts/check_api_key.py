"""Проверка входа в CRM по API-ключу: работают ли нужные боту запросы.

Сравнивает ответы по ключу с ответами при входе по логину и паролю за неделю 05–11.10.2026.
Ключ никуда не выводится. Вызываются только разрешённые запросы (только чтение).

Запуск:  .venv/bin/python -m scripts.check_api_key
"""

import base64
import json
import os
import time
from datetime import date, timedelta

import httpx

from app import config
from app.crm_client import ALLOWED_ENDPOINTS, CrmClient, day_ts, week_range

MONDAY = date(2026, 10, 5)


def summary(data) -> str:
    """Короткое описание ответа без значений: тип, число записей, ключи."""
    if isinstance(data, list):
        return f"список, {len(data)} шт."
    if isinstance(data, dict):
        if "items" in data:
            return f"items: {len(data['items'])} шт."
        if "group" in data:
            return f"записи по {len(data['group'] or {})} дням, всего {sum(sum(d.values()) for d in (data['group'] or {}).values())}"
        return "объект: " + ", ".join(list(data)[:6])
    return type(data).__name__


def main():
    key = os.getenv("IMPULSE_API_KEY", "").strip()
    if not key:
        raise SystemExit("В .env не заполнен IMPULSE_API_KEY")
    body = week_range(MONDAY, MONDAY + timedelta(days=6))
    requests = {
        "branch/list": {}, "hall/list": {}, "schedule/list": body, "schedule/places": body,
        "schedule/changes": body, "check_visits/visits": {"date": day_ts(MONDAY), "branch_id": [1]},
    }
    assert set(requests) <= ALLOWED_ENDPOINTS

    print("По логину и паролю (как сейчас):")
    expected = {}
    with CrmClient() as crm:
        for endpoint, payload in requests.items():
            expected[endpoint] = summary(crm.call(endpoint, payload))
            print(f"  {endpoint:<22} {expected[endpoint]}")

    variants = {
        "Basic <ключ>": f"Basic {key}",
        "Basic base64(ключ:)": "Basic " + base64.b64encode(f"{key}:".encode()).decode(),
    }
    with httpx.Client(timeout=config.HTTP_TIMEOUT_SEC) as http:
        for name, header in variants.items():
            print(f"\nПо API-ключу, вариант «{name}»:")
            ok = 0
            for endpoint, payload in requests.items():
                time.sleep(config.REQUEST_PAUSE_SEC)
                resp = http.post(f"{config.IMPULSE_BASE_URL}/api/public/{endpoint}",
                                 json=payload, headers={"Authorization": header})
                try:
                    got = summary(resp.json())
                except json.JSONDecodeError:
                    got = f"не JSON (HTTP {resp.status_code})"
                same = got == expected[endpoint]
                ok += same
                print(f"  {'✅' if same else '❌'} {endpoint:<22} HTTP {resp.status_code}: {got}")
            print(f"  совпало {ok} из {len(requests)}")


if __name__ == "__main__":
    main()
