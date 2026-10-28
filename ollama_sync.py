"""
ollama_sync.py — compare web :cloud models vs your locally installed Ollama models.

Sources:
  * Local  — `ollama list` on this PC.
  * Web    — the :cloud model tags from https://ollama.com (based on the
             approach in unified-chat/unified_model_loading.py).

Steps:
  1. Ensure Ollama is running (start it only if it isn't), then read `ollama list`.
  2. Scrape the cloud model catalog from ollama.com (search page + per-model tags).
  3. Show what is extra on the web vs local, and extra locally vs the web.
  4. Write run_cloud_models.bat so the web-only models can be pulled/run locally.
  5. Write a timestamped log to logs\\ollama_sync_<timestamp>.log containing
     all-in-web, all-in-local, new-in-web and new-in-local.

Usage:
  python ollama_sync.py [--web-only] [--no-close] [--pull-only] [--version]
  run_sync.bat            (same, then points you at the log file)
"""

import os
import re
import sys
import time
import json
import math
import random
import shutil
import tempfile
import datetime
import subprocess
from contextlib import contextmanager
from email.utils import parsedate_to_datetime
from urllib.parse import quote, unquote

import requests

__version__ = "0.0.8"

BASE_URL = "https://ollama.com"
SEARCH_URL = f"{BASE_URL}/search?c=cloud&o=newest"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; ollama-sync/0.0.8)",
    "Accept": "text/html,application/xhtml+xml",
}
REQUEST_TIMEOUT = 30
MAX_SEARCH_PAGES = 50  # safety cap for search-catalog pagination
MAX_CRAWL_SECONDS = 600
MAX_RETRY_WAIT = 60.0
MAX_BACKOFF = 30.0
LOCAL_REQUEST_TIMEOUT = 10
SUBPROCESS_TIMEOUT = 30
OLLAMA_HOST = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip()
if "://" not in OLLAMA_HOST:
    OLLAMA_HOST = "http://" + OLLAMA_HOST
OLLAMA_HOST = OLLAMA_HOST.rstrip("/")
OLLAMA_TAGS_URL = f"{OLLAMA_HOST}/api/tags"

STATUS_OK = "ok"
STATUS_EMPTY = "empty"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_TRUNCATED = "truncated"

# Regex for a cloud tag, e.g. "glm-5.2:cloud", "deepseek-v4-flash:0731-cloud",
# "gemma4:31b-cloud". Accept only ASCII model/tag characters so scraped names
# can never inject commands into the generated .bat file.
CLOUD_TAG_RE = re.compile(
    r"^([A-Za-z0-9][A-Za-z0-9._+-]*):([A-Za-z0-9._+-]*cloud)$",
    re.IGNORECASE | re.ASCII,
)
BASE_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$", re.ASCII)
MODEL_NAME_RE = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9._+-]*(?::[A-Za-z0-9][A-Za-z0-9._+-]*)?$",
    re.ASCII,
)

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS_DIR = os.path.join(HERE, "outputs")
LOGS_DIR = os.path.join(HERE, "logs")
RUN_SCRIPT_PATH = os.path.join(HERE, "run_cloud_models.bat")

CLOUD_TXT = os.path.join(OUTPUTS_DIR, "web_cloud_models.txt")
LOCAL_TXT = os.path.join(OUTPUTS_DIR, "local_models.txt")


def _creation_flags():
    """Windows-only process creation flag, safe to call on other platforms."""
    return getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _soup(html):
    """Parse HTML, importing BeautifulSoup lazily so model tests don't need it."""
    try:
        from bs4 import BeautifulSoup
    except ImportError as exc:  # pragma: no cover - dependency failure
        raise RuntimeError(
            "beautifulsoup4 is required for web scraping "
            "(pip install requests beautifulsoup4)"
        ) from exc
    return BeautifulSoup(html, "html.parser")


def is_safe_model_name(name, allow_tag=True):
    """Return True if *name* is safe to put in a URL/batch file."""
    if not isinstance(name, str) or not name or len(name) > 200:
        return False
    pattern = MODEL_NAME_RE if allow_tag else BASE_MODEL_RE
    return bool(pattern.fullmatch(name))


def tag_belongs_to_model(tag, model_name):
    """Return True only when the tag's base is exactly *model_name*."""
    return tag.split(":", 1)[0].casefold() == model_name.casefold()


def atomic_write_text(path, text, *, encoding="utf-8", newline=None):
    """Write *text* atomically via a temporary file in the same directory."""
    directory = os.path.dirname(os.path.abspath(path)) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp-", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline=newline) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def unique_path(path):
    """Return *path*, or a numbered variant if it already exists."""
    if not os.path.exists(path):
        return path
    stem, ext = os.path.splitext(path)
    for i in range(1, 10000):
        candidate = f"{stem}_{i}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{stem}_{int(time.time() * 1000)}{ext}"


def timestamp_str():
    """Compact timestamp for file names, e.g. 20260808_143512_123456."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")


def format_size(size):
    """Human-readable size from a byte count, e.g. 5.4 GB."""
    if size is None:
        return "?"
    try:
        size = float(size)
    except (TypeError, ValueError):
        return str(size)
    if not math.isfinite(size) or size < 0:
        return "?"
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return "?"


# =============================================================================
# Ollama lifecycle (mirrors core_value_providers.manage_ollama)
# =============================================================================
def ollama_on_path():
    """Return True if the `ollama` CLI is available on PATH."""
    return shutil.which("ollama") is not None


def _ollama_api_ready(timeout=2.0):
    """Return True if the Ollama HTTP API answers successfully."""
    try:
        resp = requests.get(OLLAMA_TAGS_URL, timeout=timeout)
        return resp.status_code == 200
    except requests.RequestException:
        return False


def is_ollama_running():
    """Return True if the Ollama API is ready or an Ollama process exists."""
    if _ollama_api_ready():
        return True
    try:
        tasklist_output = subprocess.check_output(
            ["tasklist"],
            creationflags=_creation_flags(),
            timeout=10,
        ).decode("utf-8", errors="ignore").lower()
        return "ollama.exe" in tasklist_output or "ollama app.exe" in tasklist_output
    except Exception as e:
        print(f"Could not check if Ollama is running: {e}")
        return False


def wait_for_ollama(timeout_seconds=30.0):
    """Wait until the Ollama API responds, returning True on success."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _ollama_api_ready(timeout=2.0):
            return True
        time.sleep(0.5)
    return False


def start_ollama():
    """Start `ollama serve` and wait until it responds (or ~30s timeout)."""
    if not ollama_on_path():
        print("Error: `ollama` command not found on PATH. Cannot start Ollama.")
        return None
    print("Ollama is not running. Starting Ollama...")
    try:
        proc = subprocess.Popen(
            ["ollama", "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=_creation_flags(),
        )
    except OSError as e:
        print(f"Could not start Ollama: {e}")
        return None
    print("Waiting for Ollama to be ready...", end="", flush=True)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if _ollama_api_ready(timeout=2.0):
            print(" Ready!")
            return proc.pid
        if proc.poll() is not None:
            print(" Ollama exited before becoming ready.")
            return None
        print(".", end="", flush=True)
        time.sleep(0.5)
    print(" Timeout waiting for Ollama. Query might fail.")
    return proc.pid


def stop_ollama(pid=None):
    """Close Ollama; if a PID is given, only that process tree is terminated."""
    print("Closing Ollama..." if pid else "Closing Ollama (all instances)...")
    targets = ["/PID", str(pid)] if pid else ["/IM", "ollama.exe"]
    try:
        result = subprocess.run(
            ["taskkill", "/F", "/T", *targets],
            creationflags=_creation_flags(),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if not pid:
            subprocess.run(
                ["taskkill", "/F", "/T", "/IM", "ollama app.exe"],
                creationflags=_creation_flags(),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
            )
        if result.returncode not in (0, 128):  # 128 = process already gone
            print(f"Warning: taskkill returned exit code {result.returncode}.")
    except Exception as e:
        print(f"Could not close Ollama automatically: {e}")


@contextmanager
def manage_ollama(no_close=False):
    """Ensure Ollama is running during the block; stop it after if this script started it."""
    started_pid = None
    try:
        if _ollama_api_ready():
            print("Ollama is already running.")
        elif is_ollama_running():
            print("Ollama process found; waiting for its API...")
            if wait_for_ollama(30.0):
                print("Ollama is already running.")
            else:
                print("Ollama process is not responding; attempting to start it...")
                started_pid = start_ollama()
        elif not ollama_on_path():
            print("Error: `ollama` command not found on PATH. Install Ollama or add it to PATH.")
        else:
            started_pid = start_ollama()
    except Exception as e:
        print(f"Could not check or start Ollama automatically: {e}")

    try:
        yield
    finally:
        if started_pid and not no_close:
            stop_ollama(pid=started_pid)
        elif started_pid and no_close:
            print("--no-close: leaving the Ollama instance this script started running.")


def _format_modified(value):
    """Normalise an Ollama modified_at value to 'YYYY-MM-DD HH:MM'."""
    if not isinstance(value, str):
        return "?" if value is None else str(value)
    text = value.strip()
    if not text:
        return "?"
    # ISO-8601, e.g. "2026-08-08T14:35:12.123456789Z".
    if len(text) >= 16 and text[4] == "-" and text[7] == "-" and text[10] in "T ":
        return f"{text[:10]} {text[11:16]}"
    return text


def _rows_from_models(models):
    """Convert a list of Ollama model dicts into sorted inventory rows."""
    if not isinstance(models, list):
        raise ValueError("model list is not a list")
    rows = []
    for model in models:
        if not isinstance(model, dict):
            raise ValueError("model entry is not an object")
        name = model.get("name") or model.get("model")
        if not isinstance(name, str) or not name.strip():
            raise ValueError("model entry has no name")
        name = name.strip()
        size = model.get("size")
        if size is None:
            size = "?"
        elif isinstance(size, (int, float)) and not isinstance(size, bool):
            size = format_size(size)
        else:
            size = str(size)
        rows.append((str(name), size, _format_modified(model.get("modified_at"))))
    rows.sort(key=lambda r: r[0])
    return rows


def _parse_models_payload(payload):
    """Validate an /api/tags or `ollama list --format json` payload.

    Accepts both {"models": [...]} and a bare [...] list. Raises ValueError for
    anything else, so a malformed payload is a failure rather than an empty
    inventory.
    """
    if isinstance(payload, dict):
        models = payload.get("models", [])
    elif isinstance(payload, list):
        models = payload
    else:
        raise ValueError("unexpected JSON payload type")
    return _rows_from_models(models)


SIZE_AT_START_RE = re.compile(
    r"^(\d+(?:\.\d+)?\s*(?:B|KB|MB|GB|TB))\b", re.IGNORECASE
)


def parse_ollama_list_text(text):
    """Parse the human-readable `ollama list` table into inventory rows.

    Columns are NAME, ID, SIZE, MODIFIED. SIZE ("4.7 GB" or "-") and MODIFIED
    ("2 days ago") contain spaces, so the digest is skipped and the size is
    matched at the start of the remaining text.
    """
    rows = []
    lines = (text or "").splitlines()
    # Skip a header row if present ("NAME  ID  SIZE  MODIFIED").
    if lines and "NAME" in lines[0].upper() and "SIZE" in lines[0].upper():
        lines = lines[1:]
    for line in lines:
        stripped = line.strip()
        if not stripped:
            continue
        parts = stripped.split()
        if not parts or parts[0].upper() == "NAME":
            continue
        name = parts[0]
        if not is_safe_model_name(name):
            continue
        # parts[1] is the digest for normal rows; skip it when present.
        rest = parts[2:] if len(parts) >= 3 else parts[1:]
        joined = " ".join(rest).strip()
        size = "?"
        modified = "?"
        if rest and rest[0] == "-":
            size = "-"
            modified = " ".join(rest[1:]).strip() or "?"
        else:
            match = SIZE_AT_START_RE.match(joined)
            if match:
                size = re.sub(r"\s+", " ", match.group(1)).strip()
                modified = joined[match.end():].strip() or "?"
            elif joined:
                # Unknown layout: keep the tail as opaque detail.
                modified = joined
        rows.append((name, size, modified))
    rows.sort(key=lambda r: r[0])
    return rows


def _run_ollama(args):
    """Run an ollama CLI command, returning stdout or None on any failure."""
    try:
        result = subprocess.run(
            ["ollama", *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=SUBPROCESS_TIMEOUT,
            creationflags=_creation_flags(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Note: `ollama {' '.join(args)}` failed ({exc}).")
        return None
    if result.returncode != 0:
        detail = (result.stderr or "").strip().splitlines()
        suffix = f": {detail[0]}" if detail else ""
        print(f"Note: `ollama {' '.join(args)}` exited {result.returncode}{suffix}")
        return None
    return result.stdout or ""


def get_local_ollama_models():
    """Return (rows, status) for locally installed models.

    rows are (name, size, modified) tuples; status is "ok", "empty" or
    "failed". A failure is never collapsed into an empty list.

    Order of preference:
      1. Ollama REST API (`/api/tags`) — no subprocess, structured fields.
      2. `ollama list --format json` (newer CLIs).
      3. `ollama list` text parsing (older CLIs such as 0.40.x).
    """
    # 1) REST API. This works even when the CLI is not on PATH.
    try:
        resp = requests.get(OLLAMA_TAGS_URL, timeout=LOCAL_REQUEST_TIMEOUT)
        if resp.status_code == 200:
            return _inventory_result(_parse_models_payload(resp.json()), "REST API")
        print(f"Note: Ollama API returned HTTP {resp.status_code}; "
              "falling back to `ollama list`.")
    except (requests.RequestException, ValueError) as exc:
        print(f"Note: REST API unavailable ({exc}); falling back to `ollama list`.")

    if not ollama_on_path():
        print("Error: `ollama` command not found on PATH. Install Ollama or add it to PATH.")
        return [], STATUS_FAILED

    # 2) Machine-readable JSON (newer CLIs). Older CLIs reject --format.
    output = _run_ollama(["list", "--format", "json"])
    if output is not None:
        try:
            rows = _parse_models_payload(json.loads(output or "{}"))
            return _inventory_result(rows, "ollama list --format json")
        except (json.JSONDecodeError, ValueError) as exc:
            print(f"Note: invalid JSON from `ollama list --format json` ({exc}); "
                  "falling back to text parsing.")

    # 3) Text parsing for older CLIs.
    output = _run_ollama(["list"])
    if output is None:
        return [], STATUS_FAILED
    return _inventory_result(parse_ollama_list_text(output), "ollama list")


def _inventory_result(rows, source):
    """Build a (rows, status) pair, logging where the inventory came from."""
    status = STATUS_OK if rows else STATUS_EMPTY
    print(f"  local inventory source: {source}")
    return rows, status


# =============================================================================
# Web scraper (based on unified-chat/unified_model_loading.py)
# =============================================================================
def _retry_after_seconds(value):
    """Parse a Retry-After header (seconds or HTTP-date) into seconds."""
    if not value:
        return None
    value = value.strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if when is None:
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, (when - datetime.datetime.now(datetime.timezone.utc)).total_seconds())


def _backoff_delay(attempt, base_delay):
    """Exponential backoff with jitter, capped at MAX_BACKOFF."""
    return min(MAX_BACKOFF, base_delay * (2 ** (attempt - 1))) + random.uniform(0, 0.5)


def http_get_with_backoff(url, *, params=None, headers=None, max_retries=4,
                          base_delay=1.5, deadline=None):
    """GET a URL with retries for transient failures.

    Retries only timeouts, connection errors, HTTP 429 and 5xx. Other 4xx
    responses (notably 404) are returned immediately so callers can tell a
    missing page from a network failure. Returns None when retries are
    exhausted or *deadline* (a time.monotonic() value) is reached.
    """
    request_headers = dict(HEADERS)
    if headers:
        request_headers.update(headers)

    for attempt in range(1, max_retries + 1):
        if deadline is not None and time.monotonic() >= deadline:
            print("    crawl deadline reached; stopping")
            return None
        try:
            resp = requests.get(
                url,
                params=params,
                headers=request_headers,
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            if attempt >= max_retries:
                print(f"    request failed after {max_retries} attempt(s): {exc}")
                return None
            wait = _backoff_delay(attempt, base_delay)
            print(f"    request error: {exc}; retrying in {wait:.1f}s "
                  f"(attempt {attempt}/{max_retries})")
            if deadline is not None and time.monotonic() + wait >= deadline:
                print("    crawl deadline would be exceeded; stopping")
                return None
            time.sleep(wait)
            continue

        code = resp.status_code
        if code < 400:
            return resp
        if code == 429 or code >= 500:
            if attempt >= max_retries:
                print(f"    HTTP {code} after {max_retries} attempt(s); giving up")
                return resp
            retry_after = _retry_after_seconds(resp.headers.get("Retry-After"))
            if retry_after is not None:
                wait = min(retry_after, MAX_RETRY_WAIT)
            else:
                wait = _backoff_delay(attempt, base_delay)
            print(f"    HTTP {code}; retrying in {wait:.1f}s "
                  f"(attempt {attempt}/{max_retries})")
            if deadline is not None and time.monotonic() + wait >= deadline:
                print("    crawl deadline would be exceeded; stopping")
                return None
            time.sleep(wait)
            continue
        # Other client errors are permanent (e.g. 404 missing page).
        print(f"    HTTP {code} for {url}")
        return resp
    return None


def _extract_base_models(soup):
    """Return the set of model names linked from a search results page."""
    names = set()
    for link in soup.select('a[href^="/library/"]'):
        href = (link.get("href") or "").split("?", 1)[0].split("#", 1)[0].rstrip("/")
        if not href.startswith("/library/"):
            continue
        name = unquote(href[len("/library/"):])
        # Skip tag pages (`/library/model:tag`) and any nested paths.
        if "/" in name or ":" in name:
            continue
        if is_safe_model_name(name, allow_tag=False):
            names.add(name)
    return names


def _page_has_more(soup):
    """Return True when the results page advertises a next page."""
    marker = soup.find(attrs={"data-has-more": True})
    if marker is None:
        return False
    return str(marker.get("data-has-more", "")).strip().casefold() == "true"


def _crawl_cloud_catalog(order):
    """Crawl one ordered cloud catalog, following page=N pagination.

    Returns (models, pages_fetched, pages_failed, truncated).
    """
    models = set()
    pages_fetched = 0
    pages_failed = 0
    truncated = False
    has_more = False
    page = 1
    deadline = time.monotonic() + MAX_CRAWL_SECONDS

    while page <= MAX_SEARCH_PAGES:
        if time.monotonic() >= deadline:
            print("    crawl deadline reached; catalog may be incomplete")
            truncated = True
            break
        params = {"c": "cloud"}
        if order:
            params["o"] = order
        if page > 1:
            params["page"] = page
        headers = {"HX-Request": "true"} if page > 1 else None
        print(f"  Fetching: {BASE_URL}/search?"
              + "&".join(f"{k}={v}" for k, v in params.items()))
        response = http_get_with_backoff(
            f"{BASE_URL}/search", params=params, headers=headers, deadline=deadline
        )
        if response is None or response.status_code != 200:
            if response is None and time.monotonic() >= deadline:
                truncated = True
                print(f"    crawl deadline reached on page {page}; catalog may be incomplete")
            else:
                pages_failed += 1
                code = "no response" if response is None else f"HTTP {response.status_code}"
                print(f"    page {page} failed ({code})")
            break

        pages_fetched += 1
        found = _extract_base_models(_soup(response.text))
        new = found - models
        models |= found
        has_more = _page_has_more(_soup(response.text))
        print(f"    page {page}: {len(found)} model(s), {len(new)} new "
              f"({len(models)} total, more: {'yes' if has_more else 'no'})")

        if not has_more:
            break
        if not found:
            # Claims more pages but returned nothing: stop rather than loop.
            print("    page advertised more results but contained no models; stopping")
            truncated = True
            break
        page += 1
        time.sleep(0.5)
    else:
        # Exhausted MAX_SEARCH_PAGES while a next page was still advertised.
        if has_more:
            truncated = True
            print(f"    stopped after {MAX_SEARCH_PAGES} pages; catalog may be incomplete")

    return models, pages_fetched, pages_failed, truncated


def get_base_models():
    """Get the list of base model names from the cloud search pages.

    Follows the site's `page=N` pagination (with the `HX-Request` header it
    expects) and stops when `data-has-more` is absent. Returns
    (models, status, pages_fetched, pages_failed) where status is "ok",
    "empty", "truncated" or "failed".

    A page failure makes the inventory incomplete, so status is "failed" —
    never "ok" — whenever pages_failed > 0.
    """
    try:
        models, pages_fetched, pages_failed, truncated = _crawl_cloud_catalog("newest")
        if not models and pages_failed == 0 and not truncated:
            # Fall back to the default ordering if "newest" was empty.
            print("  No models in newest order; trying the default order...")
            fallback = _crawl_cloud_catalog(None)
            models2, pages2, failed2, truncated2 = fallback
            models |= models2
            pages_fetched += pages2
            pages_failed += failed2
            truncated = truncated or truncated2
    except Exception as e:
        print(f"Error fetching base models: {e}")
        return [], STATUS_FAILED, 0, 1

    if pages_failed > 0:
        status = STATUS_FAILED
    elif truncated:
        status = STATUS_TRUNCATED
    elif models:
        status = STATUS_OK
    else:
        status = STATUS_EMPTY
    return sorted(models), status, pages_fetched, pages_failed


def get_cloud_tags_for_model(model_name):
    """Return this model's :cloud tags, [] if it has none, or None on failure.

    Only tags whose base name exactly matches *model_name* are accepted, so
    tags from a similarly named model (e.g. `foo-bar` vs `foo`) can never leak
    into the inventory.
    """
    if not is_safe_model_name(model_name, allow_tag=False):
        print(f"  skipping unsafe model name: {model_name!r}")
        return None

    url = f"{BASE_URL}/library/{quote(model_name)}/tags"
    for attempt in (1, 2):
        response = http_get_with_backoff(url, max_retries=3)
        if response is None:
            print(f"    giving up on tags for {model_name}")
            return None
        if response.status_code == 404:
            # No tags page for this model: it simply has no cloud variants.
            print("  no tags page (HTTP 404)")
            return []
        if response.status_code != 200:
            print(f"  tags page HTTP {response.status_code}")
            if attempt == 1:
                time.sleep(1.0)
                continue
            return None

        soup = _soup(response.text)
        command_rows = soup.select('input[class~="command"]')
        cloud_tags = set()
        for elem in command_rows:
            value = (elem.get("value") or "").strip()
            if CLOUD_TAG_RE.match(value) and tag_belongs_to_model(value, model_name):
                cloud_tags.add(value)

        if not command_rows:
            # Layout fallback: look for tag anchors/text mentioning this model.
            pattern = re.compile(
                rf"(?<![A-Za-z0-9._+-]){re.escape(model_name)}:"
                r"[A-Za-z0-9._+-]*cloud",
                re.IGNORECASE | re.ASCII,
            )
            for match in pattern.findall(soup.get_text(" ")):
                if CLOUD_TAG_RE.match(match) and tag_belongs_to_model(match, model_name):
                    cloud_tags.add(match)
            if not cloud_tags and attempt == 1:
                print("  tags page had no tag rows; retrying once")
                time.sleep(1.0)
                continue
        return sorted(cloud_tags)
    return None


def scrape_web_cloud_models():
    """Return (models, status, stats) — deduplicated sorted list of web :cloud tags.

    status is "ok", "empty", "truncated" or "failed"; stats carries integrity
    counters.
    """
    print("\n--- Scraping web cloud models from ollama.com ---")
    print("Step 1: Fetching base cloud models...")
    base_models, status, pages_fetched, pages_failed = get_base_models()
    if status in (STATUS_FAILED, STATUS_TRUNCATED):
        print(f"FAILED to fetch the complete base-model catalog (status: {status}) — "
              "treating this as an inventory failure, not an empty list.")
        return [], status, {"pages_fetched": pages_fetched, "pages_failed": pages_failed,
                            "base_models": len(base_models), "duplicates": 0,
                            "tags_found": 0, "failed_pages": pages_failed}

    print(f"Step 2: Found {len(base_models)} base models. Fetching cloud tags...\n")
    all_cloud_models = set()  # set => no duplicates can ever be added
    seen_tags = {}
    duplicate_count = 0
    failed_models = 0
    for i, model in enumerate(sorted(base_models), 1):
        print(f"[{i}/{len(base_models)}] Checking {model}...", end="", flush=True)
        cloud_tags = get_cloud_tags_for_model(model)
        if cloud_tags is None:
            failed_models += 1
            print("  tags page FAILED (no result)")
            time.sleep(0.5)
            continue
        for tag in cloud_tags:
            if tag in seen_tags and seen_tags[tag] != model:
                duplicate_count += 1
                print(f"  (duplicate tag {tag} also under {seen_tags[tag]}, keeping once)")
            seen_tags[tag] = model
            all_cloud_models.add(tag)
        if cloud_tags:
            print(f"  +{len(cloud_tags)} cloud variant(s): {', '.join(cloud_tags)}")
        else:
            print("  no cloud tags")
        time.sleep(0.5)

    # Final safety net: deduplicate case-insensitively (e.g. Cloud vs cloud).
    final = sorted(set(t.casefold() for t in all_cloud_models))
    if len(final) != len(all_cloud_models):
        duplicate_count += len(all_cloud_models) - len(final)
        print(f"  (collapsed {len(all_cloud_models) - len(final)} case-variant duplicate(s))")

    # A failed tags page means we couldn't read that model's full tag list, so
    # the inventory is incomplete — report FAILED, not ok.
    if failed_models > 0:
        status = STATUS_FAILED
        print(f"  {failed_models} model tag page(s) FAILED — inventory is incomplete.")
    elif final:
        status = STATUS_OK
    else:
        status = STATUS_EMPTY
        print("  No cloud tags found (inventory is empty).")

    stats = {
        "pages_fetched": pages_fetched,
        "pages_failed": pages_failed,
        "base_models": len(base_models),
        "duplicates": duplicate_count,
        "tags_found": len(final),
        "failed_pages": failed_models,
    }
    return final, status, stats


# =============================================================================
# Diff & helpers
# =============================================================================
def ensure_outputs_dir():
    os.makedirs(OUTPUTS_DIR, exist_ok=True)


def ensure_logs_dir():
    os.makedirs(LOGS_DIR, exist_ok=True)


def write_list_file(path, items):
    """Atomically write one item per line (UTF-8, LF)."""
    text = "".join(f"{item}\n" for item in items)
    atomic_write_text(path, text, encoding="utf-8", newline="\n")


def write_run_log(local_rows, local_status, web_models, web_status, web_stats, log_path,
                  script_written=None):
    """Write a structured, timestamped log of the run."""
    local_names = [name for name, _, _ in local_rows]
    local_set = set(local_names)
    web_set = set(web_models)

    new_in_web = sorted(web_set - local_set)
    new_in_local = sorted(local_set - web_set)
    common = sorted(local_set & web_set)

    lines = []
    lines.append("=" * 72)
    lines.append("OLLAMA SYNC RUN LOG")
    lines.append(f"Timestamp          : {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    lines.append(f"Web cloud models   : {len(web_set)}")
    lines.append(f"Local models       : {len(local_set)}")
    lines.append(f"Common             : {len(common)}")
    lines.append(f"New in web         : {len(new_in_web)}")
    lines.append(f"New in local       : {len(new_in_local)}")
    if script_written is True:
        lines.append(f"Run script updated : yes ({len(new_in_web)} model(s))")
    elif script_written is False:
        lines.append("Run script updated : no (user declined / not regenerated)")
    lines.append("=" * 72)

    lines.append("")
    lines.append("WEB INVENTORY")
    lines.append("-" * 40)
    if web_stats:
        lines.append(f"  Inventory status  : {web_status}")
        lines.append(f"  Pages scanned     : {web_stats.get('pages_fetched', 0)}")
        lines.append(f"  Pages failed      : {web_stats.get('pages_failed', 0)}")
        lines.append(f"  Base models found : {web_stats.get('base_models', 0)}")
        lines.append(f"  Cloud tags found  : {web_stats.get('tags_found', 0)}")
        lines.append(f"  Duplicates removed: {web_stats.get('duplicates', 0)}")
        lines.append(f"  Failed model pages: {web_stats.get('failed_pages', 0)}")
    else:
        lines.append(f"  Inventory status  : {web_status}")
    lines.append("")
    lines.append("LOCAL INVENTORY")
    lines.append("-" * 40)
    lines.append(f"  Models found      : {len(local_set)}")
    lines.append(f"  Inventory status  : {local_status}")

    lines.append("")
    lines.append("ALL IN WEB (deduplicated):")
    lines.append("-" * 40)
    for m in web_models:
        lines.append(f"  {m}")

    lines.append("")
    lines.append("ALL IN LOCAL:")
    lines.append("-" * 40)
    for name, size, modified in local_rows:
        lines.append(f"  {name:<30} {size:>10}  {modified}")

    lines.append("")
    lines.append("NEW IN WEB (not installed locally):")
    lines.append("-" * 40)
    for m in new_in_web:
        lines.append(f"  {m}")

    lines.append("")
    lines.append("NEW IN LOCAL (not on the web cloud list):")
    lines.append("-" * 40)
    for m in new_in_local:
        lines.append(f"  {m}")

    lines.append("")
    lines.append("COMMON (installed locally AND on web):")
    lines.append("-" * 40)
    for m in common:
        lines.append(f"  {m}")

    lines.append("")
    lines.append("INTEGRITY CHECK")
    lines.append("-" * 40)
    problems = []
    if web_status == STATUS_FAILED:
        problems.append("WEB inventory FAILED — results may be incomplete")
    if web_status == STATUS_TRUNCATED:
        problems.append("WEB inventory TRUNCATED — results are incomplete")
    if web_status == STATUS_EMPTY:
        problems.append("WEB inventory EMPTY — no cloud models found")
    if local_status == STATUS_FAILED:
        problems.append("LOCAL inventory FAILED — results may be incomplete")
    if local_status == STATUS_TRUNCATED:
        problems.append("LOCAL inventory TRUNCATED — results are incomplete")
    if local_status == STATUS_SKIPPED:
        problems.append("LOCAL inventory SKIPPED (--web-only) — local side is unknown")
    if not problems:
        problems.append("OK — both inventories obtained")
    for p in problems:
        lines.append(f"  {p}")

    lines.append("")
    lines.append("END OF LOG")

    atomic_write_text(log_path, "\n".join(lines) + "\n", encoding="utf-8")


def write_run_script(extra_models, pull_only=False):
    """Write run_cloud_models.bat that runs/pulls every web-only model locally.

    Returns True if the file was (re)written, False if it was skipped. An
    empty model list never overwrites an existing script.
    """
    safe_models = [m for m in extra_models if is_safe_model_name(m)]
    skipped = len(extra_models) - len(safe_models)
    if skipped:
        print(f"  WARNING: skipped {skipped} unsafe model name(s) when writing the script.")
    if not safe_models:
        print("  No web-only models to write; leaving the existing run script unchanged.")
        return False

    command = "pull" if pull_only else "run"
    action_comment = ("REM and starts a chat session." if not pull_only
                      else "REM and downloads it. No chat session is started.")
    switch_comment = ("REM To only pull without starting a chat, change `run` to `pull`."
                      if not pull_only
                      else "REM This script pulls only — no chat is started.")
    lines = [
        "@echo off",
        f"REM One `ollama {command}` per model that is available on the web as :cloud",
        "REM but is not installed locally. Each line pulls the model if needed",
        action_comment,
        "REM",
        "REM Usage: run_cloud_models.bat",
        switch_comment,
        "",
    ]
    for model in safe_models:
        lines.append(f"ollama {command} {model}")
    lines.append("")
    lines.append("REM All done. Press any key to close...")
    lines.append("pause")
    # newline="" disables Windows text-mode translation, so the explicit \r\n
    # stays as a single CRLF instead of becoming \r\r\n (which renders as a
    # blank line after every line in cmd.exe).
    atomic_write_text(
        RUN_SCRIPT_PATH,
        "\r\n".join(lines) + "\r\n",
        encoding="utf-8",
        newline="",
    )
    return True


def print_diff(local_rows, local_status, web_models, web_status):
    """Print the extra models in each direction."""
    local_set = {name for name, _, _ in local_rows}
    web_set = set(web_models)

    extra_web = sorted(web_set - local_set)
    extra_local = sorted(local_set - web_set)
    common = sorted(local_set & web_set)

    print("\n" + "=" * 72)
    print("MODEL COMPARISON — web (:cloud) vs local inventory")
    print("=" * 72)
    print(f"Total web cloud models : {len(web_set)}")
    print(f"Total local models     : {len(local_set)}")
    print(f"Common (already local) : {len(common)}")
    print(f"Extra on web           : {len(extra_web)}")
    print(f"Extra on local         : {len(extra_local)}")

    if web_status == STATUS_FAILED:
        print("\n  WARNING: WEB inventory FAILED — the web-only list above may be")
        print("  incomplete. Check the network and re-run before trusting this diff.")
    elif web_status == STATUS_TRUNCATED:
        print("\n  WARNING: WEB inventory was TRUNCATED (page/time limit) — the")
        print("  web-only list above is incomplete. Re-run before trusting this diff.")
    elif web_status == STATUS_EMPTY:
        print("\n  WARNING: WEB inventory is EMPTY — no cloud models were found.")
    if local_status == STATUS_FAILED:
        print("\n  WARNING: LOCAL inventory FAILED — `ollama list` could not be read.")
        print("  The local side is treated as unknown, not empty.")
    elif local_status == STATUS_TRUNCATED:
        print("\n  WARNING: LOCAL inventory was TRUNCATED — the local side is incomplete.")
    elif local_status == STATUS_SKIPPED:
        print("\n  NOTE: LOCAL inventory SKIPPED (--web-only) — every web model is")
        print("  shown as 'extra on web' because the local side is unknown.")

    print("\n--- Extra on WEB (not installed locally) ---")
    if extra_web:
        for m in extra_web:
            print(f"  {m}")
    else:
        print("  (none)")

    print("\n--- Extra on LOCAL (not on the web :cloud list) ---")
    if extra_local:
        for m in extra_local:
            print(f"  {m}")
    else:
        print("  (none)")

    print("\n--- Common models (installed locally AND on web :cloud) ---")
    if common:
        for m in common:
            print(f"  {m}")
    else:
        print("  (none)")

    # Show SIZE/MODIFIED columns for locally installed extra models (informational).
    if extra_local:
        print("\n  Local extra model details:")
        for name, size, modified in local_rows:
            if name in extra_local:
                print(f"    {name:<30} {size:>10}  {modified}")

    return extra_web


def _inventory_is_trusted(status):
    """True when an inventory was obtained completely (ok or genuinely empty)."""
    return status in (STATUS_OK, STATUS_EMPTY)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Compare web :cloud models vs locally installed Ollama models.")
    parser.add_argument("--web-only", action="store_true",
                        help="Skip local Ollama management/list (local side is skipped).")
    parser.add_argument("--no-close", action="store_true",
                        help="Keep Ollama running even if this script started it.")
    parser.add_argument("--pull-only", action="store_true",
                        help="Generate run_cloud_models.bat with `ollama pull` "
                             "instead of `ollama run`.")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    args = parser.parse_args(argv)

    print("=" * 72)
    print(f"OLLAMA SYNC {__version__} — web :cloud models vs local install")
    print("=" * 72)

    ensure_outputs_dir()
    ensure_logs_dir()

    if args.web_only:
        print("\n[web-only mode] Skipping local Ollama management/list.")
        local_rows = []
        local_status = STATUS_SKIPPED  # deliberately not checked, not "empty"
    else:
        with manage_ollama(no_close=args.no_close):
            print("\n--- Reading local models ---")
            local_rows, local_status = get_local_ollama_models()
            print(f"Found {len(local_rows)} local model(s) "
                  f"(inventory: {local_status}).")

    web_models, web_status, web_stats = scrape_web_cloud_models()
    print(f"\nFound {len(web_models)} web cloud model(s) (inventory: {web_status}).")

    if _inventory_is_trusted(web_status):
        write_list_file(CLOUD_TXT, web_models)
        print(f"\nSaved web cloud models -> {CLOUD_TXT}")
    else:
        print(f"\nLeft {CLOUD_TXT} unchanged (web inventory: {web_status}).")

    if _inventory_is_trusted(local_status):
        write_list_file(LOCAL_TXT, [name for name, _, _ in local_rows])
        print(f"Saved local models     -> {LOCAL_TXT}")
    elif local_status == STATUS_SKIPPED:
        print(f"Left {LOCAL_TXT} unchanged (local inventory skipped).")
    else:
        print(f"Left {LOCAL_TXT} unchanged (local inventory: {local_status}).")

    extra_web = print_diff(local_rows, local_status, web_models, web_status)

    # Only generate a script from two complete inventories. A failed, truncated
    # or skipped inventory would otherwise produce a wrong (possibly huge) list.
    inventories_ready = web_status == STATUS_OK and local_status in (STATUS_OK, STATUS_EMPTY)
    if not inventories_ready:
        print("\nSkipping run_cloud_models.bat update: both inventories must be "
              "complete (web: ok, local: ok/empty).")
        wrote_script = False
    elif args.pull_only:
        # --pull-only is an explicit "regenerate as pull" request.
        wrote_script = write_run_script(extra_web, pull_only=True)
        if wrote_script:
            print(f"\nWrote run script -> {RUN_SCRIPT_PATH} "
                  f"({len(extra_web)} model(s)) [pull-only]")
    else:
        # Show exactly which models will be added, then ask before overwriting.
        print("\n" + "=" * 72)
        print("PLANNED run_cloud_models.bat update")
        print("=" * 72)
        if extra_web:
            for m in extra_web:
                print(f"  + {m}")
        else:
            print("  (no new web-only models — the script will not be changed)")
        try:
            answer = input(f"\nRegenerate run_cloud_models.bat with these "
                           f"{len(extra_web)} web-only model(s)? "
                           f"[y/N] (Enter = No) ").strip().lower()
        except EOFError:  # non-interactive stdin (piped input, CI, etc.)
            answer = "n"
        if answer in ("y", "yes"):
            wrote_script = write_run_script(extra_web, pull_only=False)
            if wrote_script:
                print(f"Wrote run script -> {RUN_SCRIPT_PATH} ({len(extra_web)} model(s))")
        else:
            wrote_script = False
            print(f"Skipped updating {RUN_SCRIPT_PATH} (existing script left as-is).")

    log_path = unique_path(os.path.join(LOGS_DIR, f"ollama_sync_{timestamp_str()}.log"))
    write_run_log(local_rows, local_status, web_models, web_status, web_stats, log_path,
                  script_written=wrote_script)
    print(f"Wrote run log      -> {log_path}")

    # Exit code: 0 = complete run, 1 = incomplete/unusable inventory.
    if web_status not in (STATUS_OK, STATUS_EMPTY):
        print("\nFinished with an incomplete WEB inventory.")
        return 1
    if local_status not in (STATUS_OK, STATUS_EMPTY, STATUS_SKIPPED):
        print("\nFinished with an incomplete LOCAL inventory.")
        return 1
    print("\nDone.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        sys.exit(1)
