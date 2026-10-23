"""ИИ-часть без настоящей модели: имитируем ответы провайдера. Запуск: .venv/bin/pytest"""

import json
from types import SimpleNamespace

import pytest

from app import config, db, llm


def tool_call(name, args, call_id="c1"):
    return SimpleNamespace(id=call_id, function=SimpleNamespace(name=name, arguments=json.dumps(args)))


def reply(content=None, tool_calls=None):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=tool_calls))])


class FakeClient:
    """Отдаёт заранее заданные ответы и запоминает, что у него спросили."""

    def __init__(self, replies):
        self.replies, self.calls = list(replies), []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))

    def create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def tmp_db(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "test.sqlite")
    monkeypatch.setattr(llm, "run_tool", lambda conn, name, args: json.dumps({"инструмент": name, "аргументы": args}))
    monkeypatch.setattr(config, "LLM_FOLDER_ID", "folder")
    return tmp_path


def logged_questions():
    conn = db.connect()
    return [dict(r) for r in conn.execute("SELECT * FROM questions")]


def test_question_with_tool_call(tmp_db):
    client = FakeClient([reply(tool_calls=[tool_call("list_groups", {"query": "афро"})]),
                         reply("По данным: Afro — 15,0 чел./занятие.")])
    answer, meta = llm.ask("Как дела у Afro?", client)
    assert answer == "По данным: Afro — 15,0 чел./занятие."
    assert [t["name"] for t in meta["tools"]] == ["list_groups"]
    # результат инструмента ушёл модели вторым запросом
    second = client.calls[1]["messages"]
    assert second[-1]["role"] == "tool" and "list_groups" in second[-1]["content"]
    row = logged_questions()[0]
    assert row["question"] == "Как дела у Afro?" and row["error"] is None and row["rounds"] == 2


def test_rounds_are_limited(tmp_db, monkeypatch):
    monkeypatch.setattr(config, "LLM_MAX_TOOL_ROUNDS", 2)
    loop = reply(tool_calls=[tool_call("list_groups", {})])
    client = FakeClient([loop, loop, reply("Итог.")])
    answer, meta = llm.ask("?", client)
    assert answer == "Итог."
    assert "tools" not in client.calls[-1]           # последний запрос — без инструментов, только ответ


def test_provider_error_gives_friendly_message(tmp_db):
    answer, meta = llm.ask("?", FakeClient([RuntimeError("нет сети")]))
    assert "недоступен" in answer and meta["error"] == "RuntimeError"
    assert logged_questions()[0]["error"] == "RuntimeError"


def test_model_uri_for_yandex(monkeypatch):
    monkeypatch.setattr(config, "LLM_PROVIDER", "yandex")
    monkeypatch.setattr(config, "LLM_FOLDER_ID", "b1gabc")
    monkeypatch.setattr(config, "LLM_MODEL", "aliceai-llm-flash")
    assert llm.model_id() == "gpt://b1gabc/aliceai-llm-flash"


def test_disabled_without_key(monkeypatch):
    monkeypatch.setattr(config, "LLM_API_KEY", "")
    assert not llm.enabled()
    monkeypatch.setattr(config, "LLM_API_KEY", "k")
    monkeypatch.setattr(config, "LLM_PROVIDER", "yandex")
    monkeypatch.setattr(config, "LLM_FOLDER_ID", "")
    assert not llm.enabled()                         # для Яндекса нужен ещё каталог


def test_bad_tool_arguments_are_returned_to_model(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "t.sqlite")
    conn = db.connect()
    assert "ошибка" in json.loads(llm.run_tool(conn, "list_groups", "не json"))
    assert "ошибка" in json.loads(llm.run_tool(conn, "нет_такого", "{}"))
    assert "ошибка" in json.loads(llm.run_tool(conn, "get_group_history", '{"group_id": "abc"}'))


def test_russian_style_names():
    assert llm.normalize_query("Хип-хоп") == "hip hop"
    assert llm.normalize_query("растяжка") == "stretching"
    assert llm.normalize_query("Динара") == "динара"
