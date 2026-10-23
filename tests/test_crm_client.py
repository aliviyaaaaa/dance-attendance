"""Вход в CRM по ключу и защита запросов. Запуск: .venv/bin/pytest"""

import httpx
import pytest

from app import config
from app import crm_client
from app.crm_client import CrmClient, CrmError


@pytest.fixture
def key_client(monkeypatch):
    monkeypatch.setattr(config, "IMPULSE_API_KEY", "secret-key")
    monkeypatch.setattr(config, "IMPULSE_BASE_URL", "https://crm.test")
    monkeypatch.setattr(config, "REQUEST_PAUSE_SEC", 0)
    monkeypatch.setattr(config, "HTTP_RETRY_PAUSE_SEC", 0)
    seen = []

    def make(handler):
        def wrapped(request):
            seen.append(request)
            return handler(request)
        client = CrmClient()
        client._http = httpx.Client(transport=httpx.MockTransport(wrapped), headers=client._http.headers)
        return client
    return make, seen


def test_key_is_sent_as_basic_header_without_login(key_client):
    make, seen = key_client
    crm = make(lambda r: httpx.Response(200, json={"items": []}))
    assert crm.call("hall/list", {}) == {"items": []}
    assert len(seen) == 1                                   # без запроса входа user/login
    assert seen[0].headers["Authorization"] == "Basic secret-key"
    assert "PHPSESSID" not in str(seen[0].url)


def test_rejected_key_gives_clear_error(key_client):
    make, _ = key_client
    crm = make(lambda r: httpx.Response(401, json={"message": "Требуется аутентификация"}))
    with pytest.raises(CrmError, match="API-ключ не принят"):
        crm.call("schedule/list", {})


def test_only_allowed_endpoints(key_client):
    make, seen = key_client
    crm = make(lambda r: httpx.Response(200, json={}))
    with pytest.raises(CrmError, match="разрешённый список"):
        crm.call("user/list", {})                           # отдаёт хэши паролей — запрещено
    assert not seen


def test_key_not_in_error_text(key_client):
    make, _ = key_client
    crm = make(lambda r: httpx.Response(500))
    with pytest.raises(CrmError) as e:
        crm.call("hall/list", {})
    assert "secret-key" not in str(e.value)
