"""Клиент Impulse CRM: вход, сессия, разрешённые эндпоинты, повторы.

API неофициальный (внутренний JSON-API веб-интерфейса), поэтому:
- вызывать можно только эндпоинты из ALLOWED_ENDPOINTS (только чтение);
- пароль и id сессии никогда не попадают в логи и сообщения об ошибках.
"""

import calendar
import logging
import time
from datetime import date

import httpx

from app import config

log = logging.getLogger(__name__)

# httpx на уровне INFO пишет в лог полный URL, а в нём ?PHPSESSID=... — глушим.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

LOGIN_ENDPOINT = "user/login"

# Единственные эндпоинты, которые бот имеет право вызывать.
# user/list и всё про пользователей, кассу, продажи, клиентов — запрещено.
ALLOWED_ENDPOINTS = {
    "schedule/list",
    "schedule/places",
    "schedule/changes",
    "check_visits/visits",
    "hall/list",
    "branch/list",
}

AUTH_REQUIRED_MESSAGE = "Требуется аутентификация"


class CrmError(Exception):
    """Ошибка выгрузки из CRM. Текст безопасно показывать пользователю."""

    def __init__(self, endpoint: str, problem: str):
        self.endpoint = endpoint
        self.problem = problem
        super().__init__(f"Выгрузка из CRM сломалась: {endpoint}, {problem}")


def day_ts(d: date) -> int:
    """Дата → unix-время полуночи UTC этого дня (так CRM задаёт даты)."""
    return calendar.timegm(d.timetuple())


def week_range(monday: date, sunday: date) -> dict:
    """Тело запроса для schedule/list, schedule/places, schedule/changes."""
    return {
        "range": {"from": day_ts(monday), "to": day_ts(sunday)},
        "filters": {
            "branch_id": [config.BRANCH_ID],
            "groups": True,
            "individuals": False,
            "rents": False,
            "scale": "week",
        },
    }


class CrmClient:
    def __init__(self):
        if not config.IMPULSE_LOGIN or not config.IMPULSE_PASSWORD:
            raise CrmError(LOGIN_ENDPOINT, "в .env не заданы IMPULSE_LOGIN / IMPULSE_PASSWORD")
        self._http = httpx.Client(timeout=config.HTTP_TIMEOUT_SEC)
        self._session_id = None

    def close(self):
        self._http.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _url(self, endpoint: str) -> str:
        return f"{config.IMPULSE_BASE_URL}/api/public/{endpoint}"

    def _send(self, endpoint: str, **kwargs) -> httpx.Response:
        """Один HTTP-запрос с повторами при сетевых сбоях и ошибках сервера."""
        last_problem = ""
        for attempt in range(1, config.HTTP_RETRIES + 1):
            time.sleep(config.REQUEST_PAUSE_SEC)
            try:
                resp = self._http.post(self._url(endpoint), **kwargs)
                if resp.status_code < 500:
                    return resp
                last_problem = f"сервер ответил {resp.status_code}"
            except httpx.HTTPError as e:
                last_problem = f"сетевая ошибка ({type(e).__name__})"
            log.warning("CRM %s: попытка %d/%d не удалась: %s",
                        endpoint, attempt, config.HTTP_RETRIES, last_problem)
            if attempt < config.HTTP_RETRIES:
                time.sleep(config.HTTP_RETRY_PAUSE_SEC)
        raise CrmError(endpoint, last_problem)

    def login(self):
        # Форма входа CRM отправляет username и password как обычную веб-форму.
        resp = self._send(LOGIN_ENDPOINT, data={
            "username": config.IMPULSE_LOGIN,
            "password": config.IMPULSE_PASSWORD,
        })
        try:
            data = resp.json()
        except ValueError:
            raise CrmError(LOGIN_ENDPOINT, f"ответ не JSON (HTTP {resp.status_code})")
        if not isinstance(data, dict) or not data.get("success"):
            message = data.get("message") if isinstance(data, dict) else None
            raise CrmError(LOGIN_ENDPOINT, f"вход не удался: {message or 'нет сообщения'}")
        session = data.get("session")
        if not isinstance(session, str) or len(session) <= 10:
            raise CrmError(LOGIN_ENDPOINT, "в ответе нет id сессии")
        self._session_id = session
        log.info("CRM: вход выполнен")

    def call(self, endpoint: str, body: dict):
        """Вызов разрешённого эндпоинта. При истёкшей сессии — один повторный вход."""
        if endpoint not in ALLOWED_ENDPOINTS:
            raise CrmError(endpoint, "эндпоинт не входит в разрешённый список")
        if self._session_id is None:
            self.login()

        for relogin_done in (False, True):
            resp = self._send(endpoint, json=body, params={"PHPSESSID": self._session_id})
            try:
                data = resp.json()
            except ValueError:
                raise CrmError(endpoint, f"ответ не JSON (HTTP {resp.status_code})")
            if isinstance(data, dict) and data.get("message") == AUTH_REQUIRED_MESSAGE:
                if relogin_done:
                    raise CrmError(endpoint, "сессия не принимается даже после повторного входа")
                log.info("CRM: сессия истекла, вхожу заново")
                self.login()
                continue
            if resp.status_code >= 400:
                raise CrmError(endpoint, f"HTTP {resp.status_code}")
            return data
