"""A stand-in for Google's Gemini OpenAI-compatible endpoint, for end-to-end tests.

    python tests/fake_gemini.py      # serves http://127.0.0.1:11600/v1beta/openai
    GEMINI_URL=http://127.0.0.1:11600/v1beta/openai GEMINI_API_KEY=test-gemini python app.py
"""
from flask import Flask, jsonify, request

from fake_openrouter import answer  # same canned answers

app = Flask(__name__)
KEY = "test-gemini"
SEEN = []


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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=11600)
