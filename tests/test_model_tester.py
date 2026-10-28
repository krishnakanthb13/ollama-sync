"""Unit tests for test_models.py (no Ollama server required)."""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_models as tm  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=""):
        self.status_code = status_code
        self._json_data = json_data
        self.text = text

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


def test_reasoning_only_response_is_functional(monkeypatch):
    payload = {
        "response": "",
        "thinking": "Let me think about this greeting...",
        "done_reason": "length",
    }
    monkeypatch.setattr(tm.requests, "post", lambda *a, **k: FakeResponse(json_data=payload))
    status, snippet, elapsed, error = tm.run_model_test("nemotron:cloud")
    assert status == tm.STATUS_OK
    assert snippet.startswith("[reasoning]")
    assert error is None
    assert elapsed >= 0


def test_truncated_empty_response_is_not_functional(monkeypatch):
    payload = {"response": "", "thinking": "", "done_reason": "length"}
    monkeypatch.setattr(tm.requests, "post", lambda *a, **k: FakeResponse(json_data=payload))
    status, _, _, error = tm.run_model_test("m")
    assert status == tm.STATUS_NO_RESPONSE
    assert "done_reason=length" in error


def test_normal_response_is_functional(monkeypatch):
    monkeypatch.setattr(
        tm.requests, "post",
        lambda *a, **k: FakeResponse(json_data={"response": "Hello!\nFriend"}),
    )
    status, snippet, _, error = tm.run_model_test("m")
    assert (status, snippet, error) == (tm.STATUS_OK, "Hello! Friend", None)


def test_invalid_json_is_an_error(monkeypatch):
    monkeypatch.setattr(tm.requests, "post", lambda *a, **k: FakeResponse(json_data=None))
    status, _, _, error = tm.run_model_test("m")
    assert status == tm.STATUS_ERROR
    assert "JSON" in error


def test_404_and_429_are_classified():
    assert tm.classify_http_error(404, "")[0] == tm.STATUS_ERROR
    assert "not found" in tm.classify_http_error(404, "")[1]
    assert "rate limited" in tm.classify_http_error(429, "")[1]


def test_403_subscription_detection():
    assert tm.classify_http_error(403, "upgrade required")[0] == tm.STATUS_SUBSCRIPTION
    assert tm.classify_http_error(403, "nope")[0] == tm.STATUS_ERROR


def test_parallel_worker_exception_is_recorded(monkeypatch):
    def fake_test(model, prompt=tm.PROMPT):
        if model == "boom":
            raise RuntimeError("worker exploded")
        return tm.STATUS_OK, "ok", 0.1, None

    monkeypatch.setattr(tm, "run_model_test", fake_test)
    results = tm.run_all_tests(["good", "boom"], parallel=2)
    assert results["good"]["status"] == tm.STATUS_OK
    assert results["boom"]["status"] == tm.STATUS_ERROR
    assert "worker exploded" in results["boom"]["error"]
