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
  python ollama_sync.py [--web-only] [--no-close] [--pull-only]
  run_sync.bat            (same, then points you at the log file)
"""

import os
import re
import time
import json
import shutil
import datetime
import subprocess
from contextlib import contextmanager

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://ollama.com"
SEARCH_URL = f"{BASE_URL}/search?c=cloud&o=newest"
HEADERS = {"User-Agent": "Mozilla/5.0"}
REQUEST_TIMEOUT = 30
MAX_SEARCH_PAGES = 50  # safety cap for search-catalog pagination

# Regex for a cloud tag, e.g. "glm-5.2:cloud", "deepseek-v4-flash:0731-cloud",
# "gemma4:31b-cloud". Mirrors the reference implementation's pattern.
CLOUD_TAG_RE = re.compile(r"^([\w\d.+-]+):([\w\d.+-]*cloud)$", re.IGNORECASE)

HERE = os.path.dirname(os.path.abspath(__file__))
OUTPUTS_DIR = os.path.join(HERE, "outputs")
LOGS_DIR = os.path.join(HERE, "logs")
RUN_SCRIPT_PATH = os.path.join(HERE, "run_cloud_models.bat")

CLOUD_TXT = os.path.join(OUTPUTS_DIR, "web_cloud_models.txt")
LOCAL_TXT = os.path.join(OUTPUTS_DIR, "local_models.txt")


def timestamp_str():
    """Compact timestamp for file names, e.g. 20260808_143512."""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def format_size(size):
    """Human-readable size from a byte count, e.g. 5.4 GB."""
    try:
        size = float(size)
    except (TypeError, ValueError):
        return str(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} {unit}"
        size /= 1024
    return str(size)


# =============================================================================
# Ollama lifecycle (mirrors core_value_providers.manage_ollama)
# =============================================================================
def ollama_on_path():
    """Return True if the `ollama` CLI is available on PATH."""
    return shutil.which("ollama") is not None


def is_ollama_running():
    """Return True if an Ollama process is currently running."""
    try:
        tasklist_output = subprocess.check_output(
            ["tasklist"], creationflags=subprocess.CREATE_NO_WINDOW
        ).decode("utf-8", errors="ignore").lower()
        return "ollama.exe" in tasklist_output or "ollama app.exe" in tasklist_output
    except Exception as e:
        print(f"Could not check if Ollama is running: {e}")
        return False


def start_ollama():
    """Start `ollama serve` and wait until it responds (or ~30s timeout)."""
    print("Ollama is not running. Starting Ollama...")
    proc = subprocess.Popen(["ollama", "serve"], creationflags=subprocess.CREATE_NO_WINDOW)
    print("Waiting for Ollama to be ready...", end="", flush=True)
    for _ in range(30):
        try:
            resp = requests.get("http://localhost:11434/api/tags", timeout=2)
            if resp.status_code == 200:
                print(" Ready!")
                return proc.pid
        except requests.RequestException:
            pass
        print(".", end="", flush=True)
        time.sleep(1)
    print(" Timeout waiting for Ollama. Query might fail.")
    return proc.pid


def stop_ollama(pid=None):
    """Close Ollama; if a PID is given, only that process is terminated."""
    print("Closing Ollama..." if pid else "Closing Ollama (all instances)...")
    targets = [f"/PID", str(pid)] if pid else ["/IM", "ollama.exe"]
    try:
        subprocess.run(["taskkill", "/F", *targets],
                       creationflags=subprocess.CREATE_NO_WINDOW,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if not pid:
            subprocess.run(["taskkill", "/F", "/IM", "ollama app.exe"],
                           creationflags=subprocess.CREATE_NO_WINDOW,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception as e:
        print(f"Could not close Ollama automatically: {e}")


@contextmanager
def manage_ollama(no_close=False):
    """Ensure Ollama is running during the block; stop it after if this script started it."""
    started_pid = None
    try:
        if not ollama_on_path():
            print("Error: `ollama` command not found on PATH. Install Ollama or add it to PATH.")
        elif not is_ollama_running():
            started_pid = start_ollama()
        else:
            print("Ollama is already running.")
    except Exception as e:
        print(f"Could not check or start Ollama automatically: {e}")

    try:
        yield
    finally:
        if started_pid and not no_close:
            stop_ollama(pid=started_pid)
        elif started_pid and no_close:
            print("--no-close: leaving the Ollama instance this script started running.")


def get_local_ollama_models():
    """Return (name, size, modified, status) rows from the Ollama API / CLI.

    status is one of "ok", "empty" (inventory obtained, no models) or "failed"
    (inventory could not be obtained). Never collapse a failure into an empty
    list.

    Order of preference:
      1. Ollama REST API (`/api/tags`) — most reliable, gives real size and
         modified_at values.
      2. `ollama list --format json` (newer Ollama CLIs).
      3. `ollama list` text parsing (older CLIs like 0.32.x).
    """
    if not ollama_on_path():
        print("Error: `ollama` command not found on PATH. Install Ollama or add it to PATH.")
        return [], "failed"

    # 1) REST API — no subprocess, no column parsing.
    try:
        resp = requests.get("http://localhost:11434/api/tags", timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            rows = []
            for model in data.get("models", []):
                name = model.get("name") or "?"
                size = model.get("size") or "?"
                if isinstance(size, (int, float)):
                    size = format_size(size)
                modified = model.get("modified_at") or "?"
                if isinstance(modified, str) and "T" in modified:
                    # ISO timestamp -> "YYYY-MM-DD HH:MM" (drop subseconds/tz)
                    modified = modified[:10] + " " + modified[11:16]
                rows.append((name, size, modified))
            rows.sort(key=lambda r: r[0])
            status = "ok" if rows else "empty"
            return rows, status
    except (requests.RequestException, ValueError) as e:
        print(f"Note: REST API unavailable ({e}); falling back to `ollama list`.")

    # 2) Machine-readable JSON form (newer CLIs).
    try:
        result = subprocess.run(["ollama", "list", "--format", "json"],
                                capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode == 0:
            data = json.loads(result.stdout or "{}")
            rows = []
            for model in data.get("models", []):
                name = model.get("name") or "?"
                size = model.get("size") or "?"
                if isinstance(size, (int, float)):
                    size = format_size(size)
                modified = model.get("modified_at") or "?"
                rows.append((name, size, modified))
            rows.sort(key=lambda r: r[0])
            status = "ok" if rows else "empty"
            return rows, status
    except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as e:
        print(f"Note: `ollama list --format json` unavailable ({e}); "
              "falling back to text parsing.")

    # Fallback: parse `ollama list` text output. Columns are NAME ID SIZE
    # MODIFIED, but SIZE ("4.7 GB") and MODIFIED ("2 days ago") are multi-token
    # — so take the first token as the name and treat everything after it as
    # opaque size/modified detail.
    try:
        result = subprocess.run(["ollama", "list"], capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode != 0:
            print(f"Error: `ollama list` failed (exit {result.returncode}).")
            return [], "failed"
        rows = []
        for line in result.stdout.splitlines()[1:]:  # skip header
            stripped = line.strip()
            if not stripped:
                continue
            parts = stripped.split()
            name = parts[0]
            detail = " ".join(parts[1:]) or "?"
            rows.append((name, detail, "?"))
        rows.sort(key=lambda r: r[0])
        status = "ok" if rows else "empty"
        return rows, status
    except Exception as e:
        print(f"Error checking local ollama: {e}")
        return [], "failed"


# =============================================================================
# Web scraper (based on unified-chat/unified_model_loading.py)
# =============================================================================
def http_get_with_backoff(url, *, params=None, max_retries=4, base_delay=1.5):
    """GET a URL with retries and exponential backoff on transient errors/429.

    Returns a requests.Response, or None if all attempts failed.
    """
    for attempt in range(max_retries):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            if resp.status_code == 429:
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else base_delay * (2 ** attempt)
                except ValueError:
                    wait = base_delay * (2 ** attempt)
                print(f"    HTTP 429 (rate limited); retrying in {wait:.0f}s "
                      f"(attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            if resp.status_code >= 500:
                wait = base_delay * (2 ** attempt)
                print(f"    HTTP {resp.status_code}; retrying in {wait:.0f}s "
                      f"(attempt {attempt + 1}/{max_retries})")
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp
        except requests.Timeout:
            wait = base_delay * (2 ** attempt)
            print(f"    request timed out; retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
        except requests.RequestException as e:
            if attempt == max_retries - 1:
                raise
            wait = base_delay * (2 ** attempt)
            print(f"    request error: {e}; retrying in {wait:.0f}s "
                  f"(attempt {attempt + 1}/{max_retries})")
            time.sleep(wait)
    return None


def get_base_models():
    """Get the list of base model names from the cloud search pages.

    Walks the catalog page-by-page (`p=1, 2, ...`) until a page returns no new
    models or MAX_SEARCH_PAGES is reached. Returns (models, status) where status
    is "ok", "empty" or "failed".
    """
    base_models = set()
    pages_fetched = 0
    pages_failed = 0
    try:
        # "newest" order first, then the default order — both share pagination.
        for order in ("newest", None):
            page = 1
            while page <= MAX_SEARCH_PAGES:
                params = {"c": "cloud"}
                if order:
                    params["o"] = order
                if page > 1:
                    params["p"] = page
                url = f"{BASE_URL}/search"
                print(f"  Fetching: {url}?{'&'.join(f'{k}={v}' for k, v in params.items())}"
                      if page > 1 else f"  Fetching: {SEARCH_URL}")
                try:
                    response = http_get_with_backoff(url, params=params)
                    if response is None:
                        pages_failed += 1
                        print("    giving up on this page after retries")
                        break
                    pages_fetched += 1
                    soup = BeautifulSoup(response.text, "html.parser")
                    found = 0
                    for link in soup.find_all("a", href=re.compile(r"^/library/")):
                        model_name = link.get("href", "").replace("/library/", "")
                        if model_name and model_name not in base_models:
                            base_models.add(model_name)
                            found += 1
                    print(f"    page {page}: {found} new base model(s) "
                          f"({len(base_models)} total)")
                    if found == 0:
                        break  # no new models on this page — end of catalog
                    time.sleep(0.8)
                    page += 1
                except Exception as e:
                    pages_failed += 1
                    print(f"    Warning fetching page {page}: {e}")
                    break
            if base_models:
                break  # newest order already gave us the catalog
    except Exception as e:
        print(f"Error fetching base models: {e}")
        return [], "failed", pages_fetched, pages_failed

    if not base_models and pages_failed > 0:
        return [], "failed", pages_fetched, pages_failed
    status = "ok" if base_models else "empty"
    return list(base_models), status, pages_fetched, pages_failed


def get_cloud_tags_for_model(model_name):
    """Get all cloud tags (ending with :cloud) for a specific model."""
    for attempt in range(3):
        try:
            url = f"{BASE_URL}/library/{model_name}/tags"
            response = http_get_with_backoff(url)
            if response is None:
                print(f"    giving up on tags for {model_name}")
                return None  # distinguish "no cloud tags" from "page failed"
            soup = BeautifulSoup(response.text, "html.parser")

            cloud_tags = set()

            # Prefer the hidden <input class="command" value="model:tag"> rows,
            # which list every tag for the model.
            for elem in soup.select('input[class~="command"]'):
                value = elem.get("value", "").strip()
                if value and CLOUD_TAG_RE.match(value) and value.startswith(model_name.lower()):
                    cloud_tags.add(value)

            # Fall back to scraping tag text/anchors if the input rows were absent.
            if not cloud_tags:
                page_text = soup.get_text()
                patterns = [
                    rf"{re.escape(model_name)}:[\w\d.-]*cloud",
                    rf"{re.escape(model_name)}:cloud",
                    r"[\w\d.-]+:[\w\d.-]*cloud",
                ]
                for pattern in patterns:
                    for match in re.findall(pattern, page_text, re.IGNORECASE):
                        if match.lower().startswith(model_name.lower()) and match.lower().endswith("cloud"):
                            cloud_tags.add(match)

            # A tags page with no command rows at all is suspicious; retry once.
            if not soup.select('input[class~="command"]'):
                raise RuntimeError("tags page returned no tag rows")
            return sorted(cloud_tags)
        except Exception as e:
            print(f"  Warning fetching tags for {model_name} (attempt {attempt + 1}/3): {e}")
            time.sleep(1.5 * (attempt + 1))
    return None


def scrape_web_cloud_models():
    """Return (models, status, stats) — deduplicated sorted list of web :cloud tags.

    status is "ok", "empty" or "failed"; stats carries integrity counters.
    """
    print("\n--- Scraping web cloud models from ollama.com ---")
    print("Step 1: Fetching base cloud models...")
    base_models, status, pages_fetched, pages_failed = get_base_models()
    if status == "failed":
        print("FAILED to fetch base models (network/HTTP errors) — treating this "
              "as an inventory failure, not an empty list.")
        return [], "failed", {"pages_fetched": pages_fetched, "pages_failed": pages_failed,
                              "base_models": 0, "duplicates": 0,
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
    final = sorted(set(t.lower() for t in all_cloud_models))
    if len(final) != len(all_cloud_models):
        duplicate_count += len(all_cloud_models) - len(final)
        print(f"  (collapsed {len(all_cloud_models) - len(final)} case-variant duplicate(s))")

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
    with open(path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(item + "\n")


def write_run_log(local_rows, local_status, web_models, web_status, web_stats, log_path):
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
    if web_status == "failed":
        problems.append("WEB inventory FAILED — results may be incomplete")
    if local_status == "failed":
        problems.append("LOCAL inventory FAILED — results may be incomplete")
    if not problems:
        problems.append("OK — both inventories obtained")
    for p in problems:
        lines.append(f"  {p}")

    lines.append("")
    lines.append("END OF LOG")

    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")


def write_run_script(extra_models, pull_only=False):
    """Write run_cloud_models.bat that runs/pulls every web-only model locally."""
    command = "pull" if pull_only else "run"
    lines = [
        "@echo off",
        f"REM One `ollama {command}` per model that is available on the web as :cloud",
        "REM but is not installed locally. Each line pulls the model if needed",
        "REM and starts a chat session." if not pull_only else
        "REM and downloads it. No chat session is started.",
        "REM",
        "REM Usage: run_cloud_models.bat",
        "REM To only pull without starting a chat, change `run` to `pull`.",
        "",
    ]
    for model in extra_models:
        lines.append(f"ollama {command} {model}")
    lines.append("")
    lines.append("REM All done. Press any key to close...")
    lines.append("pause")
    with open(RUN_SCRIPT_PATH, "w", encoding="utf-8") as f:
        f.write("\r\n".join(lines) + "\r\n")


def print_diff(local_rows, local_status, web_models, web_status):
    """Print the extra models in each direction."""
    local_set = {name for name, _, _ in local_rows}
    web_set = set(web_models)

    extra_web = sorted(web_set - local_set)
    extra_local = sorted(local_set - web_set)
    common = sorted(local_set & web_set)

    print("\n" + "=" * 72)
    print("MODEL COMPARISON — web (:cloud) vs local (ollama list)")
    print("=" * 72)
    print(f"Total web cloud models : {len(web_set)}")
    print(f"Total local models     : {len(local_set)}")
    print(f"Common (already local) : {len(common)}")
    print(f"Extra on web           : {len(extra_web)}")
    print(f"Extra on local         : {len(extra_local)}")

    if web_status == "failed":
        print("\n  WARNING: WEB inventory FAILED — the web-only list above may be")
        print("  incomplete. Check the network and re-run before trusting this diff.")
    if local_status == "failed":
        print("\n  WARNING: LOCAL inventory FAILED — `ollama list` could not be read.")
        print("  The local side is treated as unknown, not empty.")

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


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Compare web :cloud models vs locally installed Ollama models.")
    parser.add_argument("--web-only", action="store_true",
                        help="Skip local Ollama management/list (local side is empty).")
    parser.add_argument("--no-close", action="store_true",
                        help="Keep Ollama running even if this script started it.")
    parser.add_argument("--pull-only", action="store_true",
                        help="Generate run_cloud_models.bat with `ollama pull` "
                             "instead of `ollama run`.")
    args = parser.parse_args(argv)

    print("=" * 72)
    print("OLLAMA SYNC — web :cloud models vs local install")
    print("=" * 72)

    ensure_outputs_dir()
    ensure_logs_dir()

    if args.web_only:
        print("\n[web-only mode] Skipping local Ollama management/list.")
        local_rows = []
        local_status = "empty"
    else:
        with manage_ollama(no_close=args.no_close):
            print("\n--- Reading local models (ollama list) ---")
            local_rows, local_status = get_local_ollama_models()
            print(f"Found {len(local_rows)} local model(s) "
                  f"(inventory: {local_status}).")

    web_models, web_status, web_stats = scrape_web_cloud_models()
    print(f"\nFound {len(web_models)} web cloud model(s) (inventory: {web_status}).")

    write_list_file(CLOUD_TXT, web_models)
    write_list_file(LOCAL_TXT, [name for name, _, _ in local_rows])
    print(f"\nSaved web cloud models -> {CLOUD_TXT}")
    print(f"Saved local models     -> {LOCAL_TXT}")

    extra_web = print_diff(local_rows, local_status, web_models, web_status)

    write_run_script(extra_web, pull_only=args.pull_only)
    print(f"\nWrote run script -> {RUN_SCRIPT_PATH} ({len(extra_web)} model(s))"
          f"{' [pull-only]' if args.pull_only else ''}")

    log_path = os.path.join(LOGS_DIR, f"ollama_sync_{timestamp_str()}.log")
    write_run_log(local_rows, local_status, web_models, web_status, web_stats, log_path)
    print(f"Wrote run log      -> {log_path}")

    print("\nDone.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
