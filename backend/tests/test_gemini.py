"""Google Gemini as DIMORA's AI, against a local stand-in for the Gemini endpoint."""
import sys
import threading
from pathlib import Path

import pytest
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).parent))
import fake_gemini  # noqa: E402
from dimora_core.ai.client import AIClient, AIError  # noqa: E402
from dimora_core.config import settings  # noqa: E402


@pytest.fixture(scope="module")
def gemini_server():
    srv = make_server("127.0.0.1", 11601, fake_gemini.app)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield "http://127.0.0.1:11601/v1beta/openai"
    srv.shutdown()


@pytest.fixture()
def gem(gemini_server, monkeypatch):
    monkeypatch.setattr(settings, "gemini_url", gemini_server)
    monkeypatch.setattr(settings, "gemini_key", "test-gemini")
    monkeypatch.setattr(settings, "gemini_model", "gemini-3.8-flash")
    monkeypatch.setattr(AIClient, "ollama_models", lambda self: None)  # no local Ollama
    fake_gemini.SEEN.clear()
    return fake_gemini.SEEN


def test_gemini_is_first_choice_and_answers(gem):
    ai = AIClient(provider="auto")
    assert ai.order()[0] == "gemini"
    assert ai.label() == "Google Gemini · gemini-3.8-flash"
    assert ai.ask_json("say ok") == {"ok": "DIMORA OK"}
    assert ai.used == "gemini:gemini-3.8-flash"
    assert "response_format" in gem[-1]["keys"] and "reasoning_effort" in gem[-1]["keys"]


def test_gemini_reads_images(gem):
    ai = AIClient(provider="gemini")
    ai.chat([{"role": "user", "text": "say ok", "images": ["aGVsbG8="]}])
    assert gem[-1]["images"] == 1


def test_gemini_retries_without_unsupported_params(gem):
    ai = AIClient(provider="gemini", gemini_model="gemini-strict")
    assert ai.ask_json("say ok") == {"ok": "DIMORA OK"}
    assert "temperature" not in gem[-1]["keys"]


def test_gemini_bad_key_and_model_give_clear_errors(gem):
    with pytest.raises(AIError, match="Gemini API key was rejected"):
        AIClient(provider="gemini", gemini_key="wrong").chat([{"role": "user", "text": "hi"}])
    with pytest.raises(AIError, match="not found"):
        AIClient(provider="gemini", gemini_model="qwen-x").chat([{"role": "user", "text": "hi"}])


def test_assistant_endpoint_runs_on_gemini(gem, client):
    import json
    home = json.dumps({"rooms": [{"id": "r1", "name": "Bedroom", "type": "bedroom"}], "totals": {"estimate_inr": 100000}})
    doc = '{"do":"wall_color","room":"..","color":"#hex","name":".."}'
    r = client.post("/api/assistant", json={"text": "paint the bedroom sage", "home": home, "action_doc": doc})
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["actions"] == [{"do": "wall_color", "room": "r1", "color": "#b5c4a8", "name": "sage"}]
    assert gem and gem[-1]["model"] == "gemini-3.8-flash"


def test_health_reports_gemini(gem, client):
    j = client.get("/api/health").get_json()
    assert j["ai"]["gemini"]["configured"] is True
    assert j["ai"]["active"] == "gemini"
