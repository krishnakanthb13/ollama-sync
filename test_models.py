"""
test_models.py — test every locally installed Ollama model with a "hi" prompt,
then report which are functional, which failed, and which are available to use.

It reuses ollama_sync.py's lifecycle helpers so Ollama is auto-started (and
closed again only if this script started it).

Usage:
  python test_models.py [--only <model>] [--limit N] [--parallel N] [--no-close]
  test_models.bat           (same)
"""

import os
import sys
import time
import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ollama_sync as sync

OLLAMA_API = "http://localhost:11434"
PROMPT = "hi"
# A model that hangs instead of responding should be treated as not working
# rather than blocking the whole test run forever.
REQUEST_TIMEOUT = 120
MODEL_START_TIMEOUT = 300  # first load / cold start can be slow

# ---------------------------------------------------------------------------
# Colored console output (Windows 10+ cmd.exe supports ANSI via VT processing)
# ---------------------------------------------------------------------------
_USE_COLOR = True
try:
    import ctypes
    kernel32 = ctypes.windll.kernel32
    # Enable ANSI escape sequences for this console (ENABLE_VIRTUAL_TERMINAL_PROCESSING)
    kernel32.SetConsoleMode(kernel32.GetStdHandle(-11), 7)
except Exception:
    _USE_COLOR = False

# Force UTF-8 output so ✓/✗/⚠/– symbols survive on Windows consoles regardless
# of the system codepage (cp1252 etc.).
try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

RESET = "\033[0m" if _USE_COLOR else ""
BOLD = "\033[1m" if _USE_COLOR else ""
DIM = "\033[2m" if _USE_COLOR else ""
RED = "\033[91m" if _USE_COLOR else ""
GREEN = "\033[92m" if _USE_COLOR else ""
YELLOW = "\033[93m" if _USE_COLOR else ""
BLUE = "\033[94m" if _USE_COLOR else ""
MAGENTA = "\033[95m" if _USE_COLOR else ""
CYAN = "\033[96m" if _USE_COLOR else ""
GRAY = "\033[90m" if _USE_COLOR else ""

# Status -> (symbol, color)
STATUS_STYLE = {
    "functional": ("✓", GREEN),
    "functional (slow)": ("✓", CYAN),
    "needs subscription (not usable without upgrade)": ("⚠", YELLOW),
    "retired (410 gone, no longer served)": ("✗", GRAY),
    "not working (no response)": ("✗", RED),
    "not working (error)": ("✗", RED),
    "unavailable (not installed)": ("–", MAGENTA),
}


def status_symbol(status):
    """Return (symbol, color) for a status; default to '?' in white."""
    return STATUS_STYLE.get(status, ("?", ""))


def paint(text, color):
    return f"{color}{text}{RESET}" if color else text


def paint_bar(count, total, color):
    """Colored 'x/y' progress chunk."""
    return f"{color}{count}/{total}{RESET}" if color else f"{count}/{total}"


STATUS_OK = "functional"
STATUS_SLOW = "functional (slow)"
STATUS_NO_RESPONSE = "not working (no response)"
STATUS_ERROR = "not working (error)"
STATUS_UNAVAILABLE = "unavailable (not installed)"
STATUS_SUBSCRIPTION = "needs subscription (not usable without upgrade)"
STATUS_RETIRED = "retired (410 gone, no longer served)"


def classify_http_error(status_code, body):
    """Map an HTTP error to a status + human explanation."""
    text = body or ""
    lowered = text.lower()
    if status_code == 410:
        return STATUS_RETIRED, f"HTTP {status_code} (gone)"
    if status_code == 401:
        return STATUS_SUBSCRIPTION, "HTTP 401 — login/authentication required"
    if status_code == 403:
        if "subscription" in lowered or "upgrade" in lowered or "requires" in lowered:
            return STATUS_SUBSCRIPTION, "HTTP 403 — model requires an Ollama subscription"
        return STATUS_ERROR, f"HTTP {status_code} — access denied"
    return STATUS_ERROR, f"HTTP {status_code}: {text[:120]}"


def run_model_test(model, prompt=PROMPT):
    """Send a single prompt to a model; return (status, snippet, elapsed_s, error).

    Uses a generous MODEL_START_TIMEOUT so a slow cold-start isn't misclassified
    as a timeout, then falls back to the faster REQUEST_TIMEOUT for retries.
    elapsed_s is the TOTAL time across attempts (including retries).
    """
    timeout = MODEL_START_TIMEOUT
    total_start = time.time()
    for attempt in range(2):
        try:
            payload = {
                "model": model,
                "prompt": prompt,
                "stream": False,
                "options": {"num_predict": 50, "temperature": 0.0},
            }
            resp = requests.post(f"{OLLAMA_API}/api/generate", json=payload,
                                 timeout=timeout)
            elapsed = time.time() - total_start
            if resp.status_code == 200:
                data = resp.json()
                text = (data.get("response") or "").strip()
                snippet = text[:120].replace("\n", " ")
                if not text:
                    return STATUS_NO_RESPONSE, "", elapsed, "empty response"
                if elapsed > 60:
                    return STATUS_SLOW, snippet, elapsed, None
                return STATUS_OK, snippet, elapsed, None
            status, error = classify_http_error(resp.status_code, resp.text)
            return status, "", elapsed, error
        except requests.exceptions.Timeout:
            if attempt == 0:
                timeout = REQUEST_TIMEOUT  # already waited for cold start; retry briskly
                continue
            elapsed = time.time() - total_start
            return STATUS_NO_RESPONSE, "", elapsed, "timeout"
        except Exception as e:
            elapsed = time.time() - total_start
            return STATUS_ERROR, "", elapsed, str(e)


def run_all_tests(models, parallel=1):
    """Run the 'hi' prompt against every model; return {model: result}.

    With parallel > 1, tests run concurrently in a thread pool (be mindful of
    VRAM — a few parallel tests is safe, many is not).
    """
    results = {}

    def run_one(model):
        status, snippet, elapsed, error = run_model_test(model)
        return model, {"status": status, "snippet": snippet,
                       "elapsed": elapsed, "error": error}

    if parallel <= 1:
        for i, model in enumerate(models, 1):
            print(f"[{paint_bar(i, len(models), BLUE)}] {paint(model, BOLD)}{DIM} ...{RESET}", end="", flush=True)
            model, result = run_one(model)
            results[model] = result
            sym, color = status_symbol(result["status"])
            print(f" {paint(sym, color)} {paint(result['status'], color)} ({format_elapsed(result['elapsed'])})")
            if result["error"]:
                print(f"     -> {paint(result['error'], DIM)}")
        return results

    print(f"Testing {len(models)} model(s) with {parallel} parallel workers...")
    with ThreadPoolExecutor(max_workers=parallel) as pool:
        futures = {pool.submit(run_one, m): m for m in models}
        for i, future in enumerate(as_completed(futures), 1):
            model, result = future.result()
            results[model] = result
            sym, color = status_symbol(result["status"])
            print(f"[{paint_bar(i, len(models), BLUE)}] {paint(model, BOLD)} "
                  f"{paint(sym, color)} {paint(result['status'], color)} "
                  f"({format_elapsed(result['elapsed'])})")
            if result["error"]:
                print(f"     -> {paint(result['error'], DIM)}")
    return results


def format_elapsed(seconds):
    return f"{seconds:6.1f}s"


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        description="Test every installed Ollama model with a 'hi' prompt.")
    parser.add_argument("--only", metavar="MODEL",
                        help="Test a single model (reports 'unavailable' if not installed).")
    parser.add_argument("--limit", type=int, metavar="N",
                        help="Only test the first N models.")
    parser.add_argument("--parallel", type=int, default=1, metavar="N",
                        help="Test N models concurrently (default 1; be mindful of VRAM).")
    parser.add_argument("--no-close", action="store_true",
                        help="Keep Ollama running even if this script started it.")
    args = parser.parse_args(argv)
    only = args.only
    limit = args.limit
    no_close = args.no_close
    parallel = max(1, args.parallel or 1)

    if parallel > 4:
        print(paint("WARNING: --parallel {} is high — large models run concurrently "
                    "and can exhaust VRAM. Consider lowering it.".format(parallel), YELLOW))

    print("=" * 72)
    print("OLLAMA MODEL TEST — one 'hi' prompt per installed model")
    print("=" * 72)

    sync.ensure_logs_dir()

    with sync.manage_ollama(no_close=no_close):
        rows, status = sync.get_local_ollama_models()
        if status == "failed":
            print(paint("\n✗ LOCAL MODEL INVENTORY FAILED", RED))
            print("  Cannot safely run model tests — check that Ollama is running")
            print("  and that `ollama list` works, then re-run.")
            return 1
        installed = {name for name, _, _ in rows}

        if only:
            # Test a single model; if it isn't installed yet, report as unavailable.
            models = [only]
            print(f"\nTesting single model: {only}")
            if only not in installed:
                print("  (not in `ollama list` — reporting as unavailable)")
                results = {only: {"status": STATUS_UNAVAILABLE, "snippet": "",
                                  "elapsed": 0.0, "error": "not installed"}}
            else:
                results = run_all_tests(models, parallel=parallel)
        else:
            models = [name for name, _, _ in rows]
            if limit is not None:
                models = models[:limit]
            print(f"\nFound {len(models)} local model(s) to test.")
            results = run_all_tests(models, parallel=parallel)

    # ---- summary ----
    functional = {m: r for m, r in results.items() if r["status"] == STATUS_OK}
    slow = {m: r for m, r in results.items() if r["status"] == STATUS_SLOW}
    no_resp = {m: r for m, r in results.items() if r["status"] == STATUS_NO_RESPONSE}
    errors = {m: r for m, r in results.items() if r["status"] == STATUS_ERROR}
    subscription = {m: r for m, r in results.items() if r["status"] == STATUS_SUBSCRIPTION}
    retired = {m: r for m, r in results.items() if r["status"] == STATUS_RETIRED}
    unavailable = {m: r for m, r in results.items() if r["status"] == STATUS_UNAVAILABLE}

    print("\n" + "=" * 72)
    print(paint("TEST RESULTS SUMMARY", BOLD))
    print("=" * 72)
    print(f"Functional ready to use    : {paint(str(len(functional)), GREEN)}")
    print(f"Functional (slow)          : {paint(str(len(slow)), CYAN)}")
    print(f"Needs subscription/upgrade : {paint(str(len(subscription)), YELLOW)}")
    print(f"Retired (410 gone)         : {paint(str(len(retired)), GRAY)}")
    print(f"Not working (no response)  : {paint(str(len(no_resp)), RED)}")
    print(f"Not working (error)        : {paint(str(len(errors)), RED)}")
    print(f"Unavailable (not installed): {paint(str(len(unavailable)), MAGENTA)}")
    print(f"Total tested               : {len(results)}")

    def print_group(title, group, color):
        print(f"\n{paint(title, BOLD)} ({paint(str(len(group)), color)}):")
        if not group:
            print("  (none)")
            return
        for m, r in sorted(group.items()):
            sym, status_color = status_symbol(r["status"])
            extra = ""
            if r["status"] == STATUS_SLOW:
                extra = f"  ({format_elapsed(r['elapsed'])})"
            print(f"  {paint(sym, status_color)} {paint(m, status_color)}{extra}")
            if r["snippet"]:
                print(f"      reply: {paint(r['snippet'], DIM)}")

    print_group("FUNCTIONAL & READY TO USE", {**functional, **slow}, GREEN)
    print_group("AVAILABLE BUT NEEDS SUBSCRIPTION/UPGRADE", subscription, YELLOW)
    print_group("RETIRED (no longer served)", retired, GRAY)
    print_group("NOT WORKING", {**no_resp, **errors}, RED)
    print_group("AVAILABLE BUT NOT INSTALLED", unavailable, MAGENTA)

    # ---- write log ----
    log_path = os.path.join(sync.LOGS_DIR, f"model_test_{sync.timestamp_str()}.log")
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("=" * 72 + "\n")
        f.write("OLLAMA MODEL TEST LOG\n")
        f.write(f"Timestamp: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"Prompt   : {PROMPT}\n")
        f.write(f"Parallel : {parallel}\n")
        f.write(f"Models   : {len(results)}\n")
        f.write("=" * 72 + "\n\n")

        f.write("PER-MODEL RESULTS\n")
        f.write("-" * 40 + "\n")
        for m, r in sorted(results.items()):
            f.write(f"{m}\n")
            f.write(f"  status : {r['status']}\n")
            f.write(f"  elapsed: {format_elapsed(r['elapsed'])}\n")
            if r["snippet"]:
                f.write(f"  reply  : {r['snippet']}\n")
            if r["error"]:
                f.write(f"  error  : {r['error']}\n")
            f.write("\n")

        def log_group(title, group, color=None):
            f.write(f"{title} ({len(group)}):\n")
            if not group:
                f.write("  (none)\n")
                return
            for m, r in sorted(group.items()):
                sym, _ = status_symbol(r["status"])
                extra = ""
                if r["status"] == STATUS_SLOW:
                    extra = f"  ({format_elapsed(r['elapsed'])})"
                f.write(f"  {sym} {m}{extra}\n")
                if r["snippet"]:
                    f.write(f"      reply: {r['snippet']}\n")
            f.write("\n")

        log_group("FUNCTIONAL & READY TO USE", {**functional, **slow})
        log_group("AVAILABLE BUT NEEDS SUBSCRIPTION/UPGRADE", subscription)
        log_group("RETIRED (no longer served)", retired)
        log_group("NOT WORKING", {**no_resp, **errors})
        log_group("AVAILABLE BUT NOT INSTALLED", unavailable)

        f.write("=" * 72 + "\n")
        f.write("SUMMARY\n")
        f.write(f"Functional ready to use   : {len(functional)}\n")
        f.write(f"Functional (slow)         : {len(slow)}\n")
        f.write(f"Needs subscription/upgrade: {len(subscription)}\n")
        f.write(f"Retired (410 gone)        : {len(retired)}\n")
        f.write(f"Not working (no response) : {len(no_resp)}\n")
        f.write(f"Not working (error)       : {len(errors)}\n")
        f.write(f"Unavailable (not installed): {len(unavailable)}\n")
        f.write(f"Total tested              : {len(results)}\n")
        f.write("END OF LOG\n")
    print(f"\nWrote test log -> {log_path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main() or 0)
    except KeyboardInterrupt:
        print("\n\nInterrupted by user.")
        sys.exit(130)
    except Exception as e:
        print(f"\n\nUnexpected error: {e}")
        sys.exit(1)
