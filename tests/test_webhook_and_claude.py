from types import SimpleNamespace

from fastapi.testclient import TestClient
from pydantic import SecretStr

from mcx_agent import webhook
from mcx_agent.integrations import claude
from mcx_agent.state import ClaudeDecision


def _client(make_pipeline, settings, monkeypatch, tmp_path):
    settings.gocharting_webhook_secret = SecretStr("s3cret")
    monkeypatch.setattr(webhook, "get_settings", lambda: settings)
    monkeypatch.setattr(webhook, "PAYLOAD_LOG", tmp_path / "payloads.jsonl")
    ran = []
    pipe = make_pipeline()
    monkeypatch.setattr(pipe, "run", lambda *a, **k: ran.append(a))
    return TestClient(webhook.create_app(pipe)), ran


def test_webhook_rejects_wrong_secret(make_pipeline, settings, monkeypatch, tmp_path):
    client, ran = _client(make_pipeline, settings, monkeypatch, tmp_path)
    assert client.post("/webhook/gocharting/nope", content="LONG|x").status_code == 404
    assert ran == []


def test_webhook_accepts_and_dedupes(make_pipeline, settings, monkeypatch, tmp_path):
    client, ran = _client(make_pipeline, settings, monkeypatch, tmp_path)
    r1 = client.post("/webhook/gocharting/s3cret", content="LONG|delta=42000|cvd=1").json()
    r2 = client.post("/webhook/gocharting/s3cret", content="LONG|delta_imbalance|crudeoil").json()
    assert r1["accepted"] and not r2["accepted"]
    assert len(ran) == 1
    assert (tmp_path / "payloads.jsonl").read_text().count("\n") == 2


def _fake_anthropic(monkeypatch, response):
    fake = SimpleNamespace(messages=SimpleNamespace(parse=lambda **kw: response))
    monkeypatch.setattr(claude, "_client", lambda *a: fake)


def test_claude_refusal_fails_closed(settings, monkeypatch):
    settings.app_mode = "paper"
    settings.anthropic_api_key = SecretStr("x")
    _fake_anthropic(monkeypatch, SimpleNamespace(stop_reason="refusal", parsed_output=None))
    assert claude.call_claude_decide(settings, {})["action"] == "veto"


def test_claude_multiplier_is_clamped(settings, monkeypatch):
    settings.app_mode = "paper"
    settings.anthropic_api_key = SecretStr("x")
    parsed = ClaudeDecision(action="adjust", size_multiplier=3.0, rationale="r")
    resp = SimpleNamespace(stop_reason="end_turn", parsed_output=parsed, model="m",
                           usage=SimpleNamespace(input_tokens=1, output_tokens=1))
    _fake_anthropic(monkeypatch, resp)
    assert claude.call_claude_decide(settings, {})["size_multiplier"] == 1.0
