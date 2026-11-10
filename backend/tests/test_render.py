"""AI photo renders: a Gemini image model repaints a screenshot of the 3D model."""
import base64
import sys
import threading
from pathlib import Path

import cv2
import numpy as np
import pytest
from werkzeug.serving import make_server

sys.path.insert(0, str(Path(__file__).parent))
import fake_gemini  # noqa: E402
from app import create_app  # noqa: E402
from dimora_core.ai.client import AIClient, AIError  # noqa: E402
from dimora_core.ai.services import render_prompt  # noqa: E402
from dimora_core.config import settings  # noqa: E402
from dimora_core.store import Store  # noqa: E402


def shot(w=64, h=48):
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), 200, np.uint8))
    return "data:image/jpeg;base64," + base64.b64encode(buf.tobytes()).decode()


FLOORS = [{"name": "Ground Floor", "rooms": ["Living Room", "Kitchen"]}, {"name": "First Floor", "rooms": ["Bedroom", "Terrace"]}]


@pytest.fixture(scope="module")
def server():
    srv = make_server("127.0.0.1", 11602, fake_gemini.app)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield "http://127.0.0.1:11602/v1beta/openai"
    srv.shutdown()


@pytest.fixture()
def gem(server, monkeypatch):
    monkeypatch.setattr(settings, "gemini_url", server)
    monkeypatch.setattr(settings, "gemini_key", "test-gemini")
    monkeypatch.setattr(settings, "gemini_image_model", "gemini-nano-banana-2.1")
    monkeypatch.setattr(settings, "gemini_image_model_hd", "gemini-3-pro-image")
    monkeypatch.setattr(settings, "gemini_image_fallbacks", ["gemini-3.1-flash-image", "gemini-2.5-flash-image"])
    monkeypatch.setattr(AIClient, "ollama_models", lambda self: None)
    fake_gemini.IMAGES.clear()
    return fake_gemini.IMAGES


def test_render_returns_a_photo_of_the_users_model(gem, client):
    r = client.post("/api/render", json={"image": shot(), "style": "mediterranean", "light": "golden", "floors": FLOORS, "aspect": "4:3"})
    assert r.status_code == 200, r.get_json()
    j = r.get_json()
    assert j["image"].startswith("data:image/png;base64,") and j["model"] == "gemini-nano-banana-2.1"
    assert cv2.imdecode(np.frombuffer(base64.b64decode(j["image"].split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR).shape == (6, 8, 3)
    seen = gem[-1]
    assert seen["images"] == 1 and seen["aspect"] == "4:3" and seen["size"] == "1K" and "IMAGE" in seen["modalities"]
    assert "KEEP EXACTLY" in seen["prompt"] and "terracotta" in seen["prompt"] and "2 storeys" in seen["prompt"]
    assert "First Floor: Bedroom, Terrace" in seen["prompt"]
    assert "not a video game" in seen["prompt"]


def test_hd_uses_the_pro_model_and_skips_draft_images(gem, client):
    j = client.post("/api/render", json={"image": shot(), "hd": True}).get_json()
    assert j["model"] == "gemini-3-pro-image" and gem[-1]["size"] == "2K"
    img = cv2.imdecode(np.frombuffer(base64.b64decode(j["image"].split(",", 1)[1]), np.uint8), cv2.IMREAD_COLOR)
    assert img.shape == (6, 8, 3)  # the final image, not the 2×2 "thought" draft


def test_unknown_model_falls_back_to_the_next_one(gem, client, monkeypatch):
    monkeypatch.setattr(settings, "gemini_image_model", "gemini-banana-99")
    j = client.post("/api/render", json={"image": shot()}).get_json()
    assert j["model"] == "gemini-3.1-flash-image"


def test_older_model_retries_without_image_size(gem):
    ai = AIClient(provider="gemini")
    ai.image_models = lambda hd=False: ["gemini-2.5-flash-image"]
    out = ai.render_image("make it real", base64.b64encode(b"x").decode(), aspect="1:1")
    assert out["model"] == "gemini-2.5-flash-image" and gem[-1]["size"] is None and gem[-1]["aspect"] == "1:1"


def test_model_without_image_options_retries_plainly(gem):
    ai = AIClient(provider="gemini")
    ai.image_models = lambda hd=False: ["gemini-legacy-image"]
    out = ai.render_image("make it real", base64.b64encode(b"x").decode())
    assert out["model"] == "gemini-legacy-image" and gem[-1]["aspect"] is None and gem[-1]["size"] is None


def test_no_model_found_gives_a_clear_error(gem):
    ai = AIClient(provider="gemini")
    ai.image_models = lambda hd=False: ["nope-1", "nope-2"]
    with pytest.raises(AIError, match="not found.*nope-1, nope-2"):
        ai.render_image("x", "aGVsbG8=")


def test_blocked_render_explains_why(gem, client):
    r = client.post("/api/render", json={"image": shot(), "notes": "BLOCKME"})
    assert r.status_code == 502
    assert "IMAGE_SAFETY" in r.get_json()["message"]


def test_bad_key_and_missing_key(gem, client, monkeypatch):
    r = client.post("/api/render", json={"image": shot()}, headers={"X-Gemini-Key": "wrong"})
    assert r.status_code == 502 and "key was rejected" in r.get_json()["message"]
    monkeypatch.setattr(settings, "gemini_key", "")
    r = client.post("/api/render", json={"image": shot()})
    assert r.status_code == 503 and r.get_json()["error"] == "ai_unavailable"


def test_render_needs_an_image(gem, client):
    assert client.post("/api/render", json={}).status_code == 400
    assert client.post("/api/render", json={"image": "data:image/png;base64,bm90IGFuIGltYWdl"}).status_code == 400


def test_public_site_limits_renders_but_not_own_keys(gem, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "public", True)
    monkeypatch.setattr(settings, "render_per_hour", 3)
    c = create_app(Store(url="", path=str(tmp_path / "db.sqlite"))).test_client()
    j = c.post("/api/render", json={"image": shot()}).get_json()
    assert j["remaining"] == 2
    assert c.post("/api/render", json={"image": shot(), "hd": True}).status_code == 503  # HD needs 3, only 2 left
    assert c.post("/api/render", json={"image": shot()}).status_code == 200
    assert c.post("/api/render", json={"image": shot()}).status_code == 200
    r = c.post("/api/render", json={"image": shot()})
    assert r.status_code == 503 and "free AI renders" in r.get_json()["message"]
    assert c.post("/api/render", json={"image": shot()}, headers={"X-Gemini-Key": "test-gemini"}).status_code == 200


def test_health_reports_render(gem, client):
    j = client.get("/api/health").get_json()
    assert j["ai"]["render"] == {"available": True, "model": "gemini-nano-banana-2.1", "hd_model": "gemini-3-pro-image"}


def test_prompt_options():
    p = render_prompt({"style": "kerala", "light": "evening", "setting": "garden", "notes": "x" * 999})
    assert "Mangalore" in p and "dusk" in p and "lawn" in p and "x" * 300 in p and "x" * 301 not in p
    assert "Mediterranean" in render_prompt({"style": "nonsense"})  # unknown style → default
    d = render_prompt({"mode": "dollhouse", "theme": "Japandi", "style": "kerala"})
    assert "cutaway" in d and "Japandi" in d and "Mangalore" not in d
