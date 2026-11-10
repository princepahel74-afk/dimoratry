"""A stand-in for Google's Gemini OpenAI-compatible endpoint, for end-to-end tests.

    python tests/fake_gemini.py      # serves http://127.0.0.1:11600/v1beta/openai
    GEMINI_URL=http://127.0.0.1:11600/v1beta/openai GEMINI_API_KEY=test-gemini python app.py
"""
import base64

import cv2
import numpy as np
from flask import Flask, jsonify, request

from fake_openrouter import answer  # same canned answers

app = Flask(__name__)
KEY = "test-gemini"
SEEN = []
IMAGES = []  # requests to the image models (AI photo renders)
IMAGE_MODELS = {"gemini-nano-banana-2.1", "gemini-3.1-flash-image", "gemini-3-pro-image", "gemini-2.5-flash-image", "gemini-legacy-image"}


def _png(w=8, h=6):
    ok, buf = cv2.imencode(".png", np.full((h, w, 3), (47, 83, 181), np.uint8))
    return base64.b64encode(buf.tobytes()).decode()


@app.post("/v1beta/openai/chat/completions")
def chat():
    if request.headers.get("Authorization") != f"Bearer {KEY}":
        return jsonify([{"error": {"code": 401, "message": "API key not valid. Please pass a valid API key.", "status": "INVALID_ARGUMENT"}}]), 401
    b = request.get_json()
    if not str(b.get("model", "")).startswith("gemini-"):
        return jsonify([{"error": {"code": 404, "message": f"models/{b.get('model')} is not found", "status": "NOT_FOUND"}}]), 404
    if "temperature" in b and b.get("model") == "gemini-strict":  # a model that refuses temperature, like some Gemini 3 models
        return jsonify([{"error": {"code": 400, "message": "temperature is not supported", "status": "INVALID_ARGUMENT"}}]), 400
    msgs = b.get("messages") or []
    last = msgs[-1]["content"]
    prompt = last if isinstance(last, str) else " ".join(p.get("text", "") for p in last if p.get("type") == "text")
    images = 0 if isinstance(last, str) else sum(p.get("type") == "image_url" for p in last)
    SEEN.append({"model": b.get("model"), "images": images, "keys": sorted(b)})
    return jsonify(choices=[{"index": 0, "message": {"role": "assistant", "content": answer(prompt, len(msgs))}, "finish_reason": "stop"}], model=b.get("model"))


@app.post("/v1beta/models/<model>:generateContent")
def generate(model):
    """Gemini's native endpoint, as used by the image models ("Nano Banana")."""
    if request.headers.get("x-goog-api-key") != KEY:
        return jsonify({"error": {"code": 401, "message": "API key not valid.", "status": "UNAUTHENTICATED"}}), 401
    if model not in IMAGE_MODELS:
        return jsonify({"error": {"code": 404, "message": f"models/{model} is not found", "status": "NOT_FOUND"}}), 404
    b = request.get_json()
    parts = b["contents"][0]["parts"]
    prompt = " ".join(p.get("text", "") for p in parts)
    gen = b.get("generationConfig") or {}
    img = (gen.get("imageConfig") or {})
    if model == "gemini-legacy-image" and "imageConfig" in gen:  # a model with no image options at all
        return jsonify({"error": {"code": 400, "message": "Invalid JSON payload received. Unknown name \"imageConfig\" at 'generation_config'", "status": "INVALID_ARGUMENT"}}), 400
    if model == "gemini-2.5-flash-image" and "imageSize" in img:  # an older model without size control
        return jsonify({"error": {"code": 400, "message": "Unknown name \"imageSize\" at 'generation_config.image_config'", "status": "INVALID_ARGUMENT"}}), 400
    IMAGES.append({"model": model, "prompt": prompt, "images": sum("inlineData" in p for p in parts),
                   "aspect": img.get("aspectRatio"), "size": img.get("imageSize"), "modalities": gen.get("responseModalities")})
    if "BLOCKME" in prompt:
        return jsonify(candidates=[{"content": {"parts": [{"text": "I can't make that image."}]}, "finishReason": "IMAGE_SAFETY"}])
    out = [{"text": "Here is your render."}, {"inlineData": {"mimeType": "image/png", "data": _png()}}]
    if model == "gemini-3-pro-image":  # thinking model: draft images are marked as thoughts
        out.insert(0, {"inlineData": {"mimeType": "image/png", "data": _png(2, 2)}, "thought": True})
    return jsonify(candidates=[{"content": {"role": "model", "parts": out}, "finishReason": "STOP"}])


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=11600)
