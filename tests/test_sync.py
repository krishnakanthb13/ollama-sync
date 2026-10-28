"""Unit tests for ollama_sync.py (no network access)."""

import datetime
import os
import sys
from contextlib import contextmanager

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ollama_sync as sync  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, text="", json_data=None, headers=None):
        self.status_code = status_code
        self.text = text
        self._json_data = json_data
        self.headers = headers or {}

    def json(self):
        if self._json_data is None:
            raise ValueError("no json")
        return self._json_data


# ---------------------------------------------------------------------------
# HTTP retry rules
# ---------------------------------------------------------------------------
def test_404_is_not_retried(monkeypatch):
    calls = []

    def fake_get(*args, **kwargs):
        calls.append(kwargs)
        return FakeResponse(404, "not found")

    monkeypatch.setattr(sync.requests, "get", fake_get)
    resp = sync.http_get_with_backoff("https://example.test/x")
    assert resp.status_code == 404
    assert len(calls) == 1


def test_400_is_not_retried(monkeypatch):
    calls = []
    monkeypatch.setattr(
        sync.requests, "get",
        lambda *a, **k: calls.append(1) or FakeResponse(400),
    )
    resp = sync.http_get_with_backoff("https://example.test/x")
    assert resp.status_code == 400
    assert len(calls) == 1


def test_500_is_retried_then_succeeds(monkeypatch):
    responses = [FakeResponse(500), FakeResponse(503), FakeResponse(200, "ok")]
    sleeps = []
    monkeypatch.setattr(sync.requests, "get", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(sync.time, "sleep", lambda s: sleeps.append(s))
    resp = sync.http_get_with_backoff("https://example.test/x", max_retries=4)
    assert resp.status_code == 200
    assert len(sleeps) == 2
    assert all(s <= sync.MAX_BACKOFF + 0.5 for s in sleeps)


def test_429_honours_retry_after(monkeypatch):
    responses = [
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(200, "ok"),
    ]
    sleeps = []
    monkeypatch.setattr(sync.requests, "get", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(sync.time, "sleep", lambda s: sleeps.append(s))
    resp = sync.http_get_with_backoff("https://example.test/x")
    assert resp.status_code == 200
    assert sleeps == [7.0]


def test_retry_after_is_capped(monkeypatch):
    responses = [
        FakeResponse(429, headers={"Retry-After": "9999"}),
        FakeResponse(200, "ok"),
    ]
    sleeps = []
    monkeypatch.setattr(sync.requests, "get", lambda *a, **k: responses.pop(0))
    monkeypatch.setattr(sync.time, "sleep", lambda s: sleeps.append(s))
    sync.http_get_with_backoff("https://example.test/x")
    assert sleeps == [sync.MAX_RETRY_WAIT]


def test_retry_after_http_date(monkeypatch):
    now = datetime.datetime(2026, 8, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)

    class FixedDateTime(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    monkeypatch.setattr(sync.datetime, "datetime", FixedDateTime)
    value = "Sat, 08 Aug 2026 12:00:30 GMT"
    assert sync._retry_after_seconds(value) == 30.0
    assert sync._retry_after_seconds("nonsense") is None
    assert sync._retry_after_seconds(None) is None


def test_5xx_exhaustion_returns_response(monkeypatch):
    monkeypatch.setattr(sync.requests, "get", lambda *a, **k: FakeResponse(502))
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    resp = sync.http_get_with_backoff("https://example.test/x", max_retries=2)
    assert resp.status_code == 502


def test_deadline_stops_before_request(monkeypatch):
    called = []
    monkeypatch.setattr(sync.requests, "get", lambda *a, **k: called.append(1))
    resp = sync.http_get_with_backoff(
        "https://example.test/x", deadline=sync.time.monotonic() - 1
    )
    assert resp is None
    assert called == []


# ---------------------------------------------------------------------------
# Catalog pagination
# ---------------------------------------------------------------------------
def _page(*names, more=False):
    links = "".join(f'<a href="/library/{name}">x</a>' for name in names)
    marker = '<div data-has-more="true"></div>' if more else ""
    return f"<html>{links}{marker}</html>"


def test_crawl_uses_page_param_and_hx_header(monkeypatch):
    calls = []

    def fake_get(url, *, params=None, headers=None, **kwargs):
        calls.append((url, dict(params or {}), dict(headers or {})))
        if params.get("page") == 2:
            return FakeResponse(200, _page("b"))
        return FakeResponse(200, _page("a", more=True))

    monkeypatch.setattr(sync, "http_get_with_backoff", fake_get)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)

    models, fetched, failed, truncated = sync._crawl_cloud_catalog("newest")
    assert models == {"a", "b"}
    assert (fetched, failed, truncated) == (2, 0, False)
    assert calls[0][1] == {"c": "cloud", "o": "newest"}
    assert calls[1][1] == {"c": "cloud", "o": "newest", "page": 2}
    assert calls[1][2].get("HX-Request") == "true"


def test_crawl_failure_marks_inventory_failed(monkeypatch):
    monkeypatch.setattr(sync, "http_get_with_backoff", lambda *a, **k: None)
    models, status, fetched, failed = sync.get_base_models()
    assert models == []
    assert status == sync.STATUS_FAILED
    assert failed == 1


def test_crawl_truncated_at_page_cap(monkeypatch):
    monkeypatch.setattr(sync, "MAX_SEARCH_PAGES", 1)
    monkeypatch.setattr(sync.time, "sleep", lambda s: None)
    monkeypatch.setattr(
        sync, "http_get_with_backoff",
        lambda *a, **k: FakeResponse(200, _page("a", more=True)),
    )
    models, status, fetched, failed = sync.get_base_models()
    assert models == ["a"]
    assert status == sync.STATUS_TRUNCATED
    assert (fetched, failed) == (1, 0)


def test_crawl_truncated_at_deadline(monkeypatch):
    monkeypatch.setattr(sync, "MAX_CRAWL_SECONDS", 0)
    called = []
    monkeypatch.setattr(
        sync, "http_get_with_backoff",
        lambda *a, **k: called.append(1) or FakeResponse(200, _page("a")),
    )
    models, status, fetched, failed = sync.get_base_models()
    assert models == []
    assert status == sync.STATUS_TRUNCATED
    assert (fetched, failed) == (0, 0)
    assert called == []


def test_extract_base_models_skips_tag_and_nested_links():
    soup = sync._soup(
        '<a href="/library/foo">x</a>'
        '<a href="/library/foo:cloud">x</a>'
        '<a href="/library/bar/baz">x</a>'
        '<a href="/library/good-name">x</a>'
    )
    assert sync._extract_base_models(soup) == {"foo", "good-name"}


# ---------------------------------------------------------------------------
# Tag matching
# ---------------------------------------------------------------------------
def test_tag_must_match_exact_base():
    assert sync.tag_belongs_to_model("foo:cloud", "foo")
    assert sync.tag_belongs_to_model("Foo:cloud", "foo")
    assert not sync.tag_belongs_to_model("foo-bar:cloud", "foo")
    assert not sync.tag_belongs_to_model("foo2:cloud", "foo")


def test_tags_page_404_is_empty_not_failure(monkeypatch):
    monkeypatch.setattr(
        sync, "http_get_with_backoff", lambda *a, **k: FakeResponse(404)
    )
    assert sync.get_cloud_tags_for_model("foo") == []


def test_tags_are_filtered_by_exact_base(monkeypatch):
    html = (
        '<input class="command" value="foo:cloud">'
        '<input class="command" value="foo-bar:cloud">'
        '<input class="command" value="other:cloud">'
    )
    monkeypatch.setattr(
        sync, "http_get_with_backoff", lambda *a, **k: FakeResponse(200, html)
    )
    assert sync.get_cloud_tags_for_model("foo") == ["foo:cloud"]


# ---------------------------------------------------------------------------
# Local inventory parsing
# ---------------------------------------------------------------------------
def test_parse_payload_rejects_null_models():
    with pytest.raises(ValueError):
        sync._parse_models_payload({"models": None})


def test_parse_payload_rejects_non_object_entries():
    with pytest.raises(ValueError):
        sync._parse_models_payload({"models": ["nope"]})
    with pytest.raises(ValueError):
        sync._parse_models_payload({"models": [{"size": 1}]})


def test_parse_payload_accepts_bare_list():
    rows = sync._parse_models_payload([{"name": "b", "size": 2048,
                                        "modified_at": "2026-08-08T14:35:12Z"}])
    assert rows == [("b", "2.0 KB", "2026-08-08 14:35")]


def test_parse_ollama_list_text():
    text = (
        "NAME                  ID              SIZE      MODIFIED\n"
        "glm-5.3:cloud         8477dab3e25b    4.7 GB    2 days ago\n"
        "gemma4:cloud          ef09f235533c    5.1 GB    About a minute ago\n"
        "tiny:cloud            0123456789ab    -         3 weeks ago\n"
        "\n"
    )
    rows = sync.parse_ollama_list_text(text)
    assert rows == [
        ("gemma4:cloud", "5.1 GB", "About a minute ago"),
        ("glm-5.3:cloud", "4.7 GB", "2 days ago"),
        ("tiny:cloud", "-", "3 weeks ago"),
    ]


def test_local_rest_inventory_works_without_cli(monkeypatch):
    payload = {"models": [{"name": "a:cloud", "size": 1073741824,
                           "modified_at": "2026-08-08T14:35:00Z"}]}
    monkeypatch.setattr(sync, "ollama_on_path", lambda: False)
    monkeypatch.setattr(
        sync.requests, "get", lambda *a, **k: FakeResponse(200, json_data=payload)
    )
    rows, status = sync.get_local_ollama_models()
    assert status == sync.STATUS_OK
    assert rows == [("a:cloud", "1.0 GB", "2026-08-08 14:35")]


def test_local_bad_schema_falls_back_to_cli(monkeypatch):
    outputs = iter(['{"models": null}', "NAME ID SIZE MODIFIED\nfoo:cloud abc 1 GB now\n"])

    def fake_run(args):
        return next(outputs)

    monkeypatch.setattr(sync, "ollama_on_path", lambda: True)
    monkeypatch.setattr(
        sync.requests, "get", lambda *a, **k: FakeResponse(200, json_data={"models": None})
    )
    monkeypatch.setattr(sync, "_run_ollama", fake_run)
    rows, status = sync.get_local_ollama_models()
    assert status == sync.STATUS_OK
    assert rows == [("foo:cloud", "1 GB", "now")]


def test_local_inventory_failure_is_not_empty(monkeypatch):
    def raise_conn(*a, **k):
        raise sync.requests.ConnectionError("down")

    monkeypatch.setattr(sync.requests, "get", raise_conn)
    monkeypatch.setattr(sync, "ollama_on_path", lambda: True)
    monkeypatch.setattr(sync, "_run_ollama", lambda args: None)
    rows, status = sync.get_local_ollama_models()
    assert rows == []
    assert status == sync.STATUS_FAILED


# ---------------------------------------------------------------------------
# Output writing / script generation
# ---------------------------------------------------------------------------
def test_atomic_write_text_replaces_and_leaves_no_temp(tmp_path):
    target = tmp_path / "out.txt"
    target.write_text("old", encoding="utf-8")
    sync.atomic_write_text(str(target), "new\n", encoding="utf-8")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["out.txt"]


def test_write_run_script_refuses_empty_list(monkeypatch, tmp_path):
    script = tmp_path / "run_cloud_models.bat"
    script.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(sync, "RUN_SCRIPT_PATH", str(script))
    assert sync.write_run_script([]) is False
    assert script.read_text(encoding="utf-8") == "keep me"


def test_write_run_script_rejects_unsafe_names(monkeypatch, tmp_path):
    script = tmp_path / "run_cloud_models.bat"
    script.write_text("keep me", encoding="utf-8")
    monkeypatch.setattr(sync, "RUN_SCRIPT_PATH", str(script))
    assert sync.write_run_script(["evil & del /q *"]) is False
    assert script.read_text(encoding="utf-8") == "keep me"


def test_write_run_script_writes_crlf_without_bom(monkeypatch, tmp_path):
    script = tmp_path / "run_cloud_models.bat"
    monkeypatch.setattr(sync, "RUN_SCRIPT_PATH", str(script))
    assert sync.write_run_script(["foo:cloud"]) is True
    data = script.read_bytes()
    assert not data.startswith(b"\xef\xbb\xbf")
    assert b"ollama run foo:cloud\r\n" in data
    assert b"\r\r\n" not in data


# ---------------------------------------------------------------------------
# main() gating and exit codes
# ---------------------------------------------------------------------------
@contextmanager
def _no_ollama(no_close=False):
    yield


def _prepare_main(monkeypatch, tmp_path, web_models, web_status, web_stats=None):
    monkeypatch.setattr(sync, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sync, "LOGS_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(sync, "CLOUD_TXT", str(tmp_path / "outputs" / "web.txt"))
    monkeypatch.setattr(sync, "LOCAL_TXT", str(tmp_path / "outputs" / "local.txt"))
    monkeypatch.setattr(sync, "RUN_SCRIPT_PATH", str(tmp_path / "run_cloud_models.bat"))
    monkeypatch.setattr(sync, "manage_ollama", _no_ollama)
    monkeypatch.setattr(
        sync, "get_local_ollama_models",
        lambda: ([("foo:cloud", "1 GB", "now")], sync.STATUS_OK),
    )
    monkeypatch.setattr(
        sync, "scrape_web_cloud_models",
        lambda: (web_models, web_status,
                 web_stats or {"pages_fetched": 1, "pages_failed": 0,
                               "base_models": 1, "duplicates": 0,
                               "tags_found": len(web_models), "failed_pages": 0}),
    )
    (tmp_path / "outputs").mkdir()
    (tmp_path / "logs").mkdir()


def test_main_failed_web_inventory_keeps_outputs_and_returns_1(monkeypatch, tmp_path):
    _prepare_main(monkeypatch, tmp_path, [], sync.STATUS_FAILED)
    cloud = tmp_path / "outputs" / "web.txt"
    local = tmp_path / "outputs" / "local.txt"
    cloud.write_text("old-web\n", encoding="utf-8")
    local.write_text("old-local\n", encoding="utf-8")

    assert sync.main([]) == 1
    assert cloud.read_text(encoding="utf-8") == "old-web\n"
    assert local.read_text(encoding="utf-8") == "foo:cloud\n"
    assert not (tmp_path / "run_cloud_models.bat").exists()
    assert list((tmp_path / "logs").glob("ollama_sync_*.log"))


def test_main_truncated_web_does_not_generate_script(monkeypatch, tmp_path):
    _prepare_main(monkeypatch, tmp_path, ["a:cloud"], sync.STATUS_TRUNCATED)
    assert sync.main([]) == 1
    assert not (tmp_path / "run_cloud_models.bat").exists()
    assert not (tmp_path / "outputs" / "web.txt").exists()


def test_main_writes_trusted_outputs(monkeypatch, tmp_path):
    _prepare_main(monkeypatch, tmp_path, ["a:cloud"], sync.STATUS_OK)
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert sync.main([]) == 0
    assert (tmp_path / "outputs" / "web.txt").read_text(encoding="utf-8") == "a:cloud\n"
    assert (tmp_path / "outputs" / "local.txt").read_text(encoding="utf-8") == "foo:cloud\n"
