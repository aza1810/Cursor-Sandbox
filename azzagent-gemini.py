#!/usr/local/bin/python3.9
"""
AzzAgent Gemini 1.4
Tiny 32-bit-friendly coding/system agent for Python 3.9.

- Reads may inspect the whole Linux filesystem.
- Normal writes stay inside the project root.
- Project AND system approval prompts default to YES when left blank.
- API key is entered once and saved locally.
- /model lists all generateContent models available to the API key.
- The last successfully used model is remembered across restarts.
- Retries/fallbacks use short timeouts so busy models do not look frozen.
- Clear live status: Sent, Thinking, Received, Reply, Tool, Command and Done.
"""

import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-3.6-flash"
MODEL = DEFAULT_MODEL
PROJECT_ROOT = Path.cwd().resolve()
AUTO_APPROVE = False
MAX_TOOL_STEPS = 16
MAX_OUTPUT_CHARS = 30000
HISTORY = []
API_KEY = None

KEY_FILE = Path.home() / ".azzagent_gemini_key"
MODEL_FILE = Path.home() / ".azzagent_model"
TRANSIENT_HTTP_CODES = (429, 500, 502, 503, 504)
PRIMARY_TIMEOUT = 25
FALLBACK_TIMEOUT = 10
MODEL_LIST_TIMEOUT = 20
HEARTBEAT_SECONDS = 3


class GeminiHTTPError(Exception):
    def __init__(self, code, body):
        self.code = int(code)
        self.body = body
        Exception.__init__(self, "Gemini API HTTP %s: %s" % (self.code, body[:2000]))


class GeminiTimeoutError(Exception):
    pass


def load_or_create_key():
    if KEY_FILE.exists():
        key = KEY_FILE.read_text(encoding="utf-8").strip()
        if key:
            return key
    key = input("Gemini API key (shown while typing, saved after this): ").strip()
    if not key:
        return None
    KEY_FILE.write_text(key + "\n", encoding="utf-8")
    try:
        os.chmod(str(KEY_FILE), 0o600)
    except Exception:
        pass
    print("[Saved] API key -> %s" % KEY_FILE)
    return key


def load_saved_model():
    if MODEL_FILE.exists():
        try:
            value = MODEL_FILE.read_text(encoding="utf-8").strip()
            if value:
                return value
        except Exception:
            pass
    return DEFAULT_MODEL


def save_model(model):
    try:
        MODEL_FILE.write_text(model.strip() + "\n", encoding="utf-8")
        try:
            os.chmod(str(MODEL_FILE), 0o600)
        except Exception:
            pass
    except Exception as e:
        print("[Warning] Could not save model preference: %s" % e)


def resolve_read_path(value):
    p = Path(value)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    return p.resolve()


def resolve_project_write_path(value):
    p = Path(value)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    if p != PROJECT_ROOT and PROJECT_ROOT not in p.parents:
        raise ValueError("Write path is outside the project root")
    return p


def normal_approve(message):
    if AUTO_APPROVE:
        print("[Approved] project auto-approve")
        return True
    answer = input("\n%s\nApprove? [Y/n] " % message).strip().lower()
    return answer in ("", "y", "yes")


def system_approve(message):
    answer = input("\nSYSTEM ACTION: %s\nApprove? [Y/n] " % message).strip().lower()
    return answer in ("", "y", "yes")


def list_files(args):
    base = resolve_read_path(args.get("path", "."))
    recursive = bool(args.get("recursive", False))
    out = []
    if base.is_file():
        return str(base)
    if not base.exists():
        return "ERROR: path does not exist: %s" % base
    try:
        if recursive:
            for current, dirs, files in os.walk(str(base)):
                dirs[:] = sorted(d for d in dirs if d not in (".git", "node_modules", "__pycache__"))
                cur = Path(current)
                for d in dirs:
                    out.append(str(cur / d) + "/")
                    if len(out) >= 300:
                        return "\n".join(out) + "\n... truncated"
                for f in sorted(files):
                    out.append(str(cur / f))
                    if len(out) >= 300:
                        return "\n".join(out) + "\n... truncated"
        else:
            for item in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                out.append(str(item) + ("/" if item.is_dir() else ""))
                if len(out) >= 300:
                    out.append("... truncated")
                    break
    except (PermissionError, OSError) as e:
        return "ERROR: %s" % e
    return "\n".join(out)


def read_file(args):
    p = resolve_read_path(args["path"])
    if not p.is_file():
        return "ERROR: not a readable file: %s" % p
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except (PermissionError, OSError) as e:
        return "ERROR: %s" % e
    start = max(1, int(args.get("start_line", 1)))
    end = int(args.get("end_line", 0)) or min(len(lines), start + 399)
    text = "\n".join("%d: %s" % (i, line) for i, line in enumerate(lines[start - 1:end], start))
    return text[:MAX_OUTPUT_CHARS]


def write_file(args):
    p = resolve_project_write_path(args["path"])
    content = args.get("content", "")
    if not normal_approve("WRITE %s" % p):
        return "DENIED"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return "WROTE %s" % p


def replace_text(args):
    p = resolve_project_write_path(args["path"])
    old = args["old"]
    new = args["new"]
    count = int(args.get("count", 1))
    text = p.read_text(encoding="utf-8")
    if old not in text:
        return "ERROR: old text not found"
    if not normal_approve("EDIT %s" % p):
        return "DENIED"
    p.write_text(text.replace(old, new, count), encoding="utf-8")
    return "EDITED %s" % p


def system_write_file(args):
    p = resolve_read_path(args["path"])
    content = args.get("content", "")
    if not system_approve("WRITE SYSTEM FILE %s" % p):
        return "DENIED"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return "SYSTEM WRITE COMPLETE: %s" % p


def system_replace_text(args):
    p = resolve_read_path(args["path"])
    old = args["old"]
    new = args["new"]
    count = int(args.get("count", 1))
    text = p.read_text(encoding="utf-8")
    if old not in text:
        return "ERROR: old text not found"
    if not system_approve("EDIT SYSTEM FILE %s" % p):
        return "DENIED"
    p.write_text(text.replace(old, new, count), encoding="utf-8")
    return "SYSTEM EDIT COMPLETE: %s" % p


def _run_shell(command, cwd, approval_message, system=False):
    approve = system_approve if system else normal_approve
    if not approve(approval_message):
        return "DENIED"
    started = time.time()
    print("[Command] %s" % command)
    print("[Running] cwd=%s" % cwd)
    try:
        result = subprocess.run(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=120,
        )
        elapsed = time.time() - started
        print("[Done] exit=%d | %.1fs" % (result.returncode, elapsed))
        return ("exit=%d\n%s" % (result.returncode, result.stdout or ""))[:MAX_OUTPUT_CHARS]
    except subprocess.TimeoutExpired:
        print("[Done] TIMEOUT after 120s")
        return "ERROR: command timed out after 120 seconds"


def shell(args):
    command = args["command"]
    return _run_shell(command, PROJECT_ROOT, "RUN PROJECT SHELL: %s" % command, False)


def system_shell(args):
    command = args["command"]
    return _run_shell(command, "/", "RUN SYSTEM SHELL: %s" % command, True)


TOOLS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "replace_text": replace_text,
    "system_write_file": system_write_file,
    "system_replace_text": system_replace_text,
    "shell": shell,
    "system_shell": system_shell,
}


def instructions():
    return """You are AzzAgent, a coding and Linux system agent on a small 32-bit Linux machine.
Project root: %s
System root: /

You may READ anywhere on the filesystem using list_files/read_file.
Normal write_file/replace_text are restricted to the project root.
For changes outside the project root, use system_write_file/system_replace_text or system_shell.
Both project and system approval prompts default to YES when the user presses Enter, but system actions still show a SYSTEM ACTION prompt.
You are allowed to inspect and modify AzzAgent's own script when the user explicitly asks you to improve or change AzzAgent.
Inspect before editing. Do not invent file contents. Prefer read-only inspection before system changes.
Return EXACTLY one JSON object and no markdown.

Tool examples:
{"type":"tool","tool":"list_files","args":{"path":"/etc","recursive":false}}
{"type":"tool","tool":"read_file","args":{"path":"/etc/os-release","start_line":1,"end_line":100}}
{"type":"tool","tool":"write_file","args":{"path":"main.py","content":"..."}}
{"type":"tool","tool":"replace_text","args":{"path":"main.py","old":"x","new":"y","count":1}}
{"type":"tool","tool":"shell","args":{"command":"python3.9 test.py"}}
{"type":"tool","tool":"system_shell","args":{"command":"uname -a"}}
{"type":"tool","tool":"system_write_file","args":{"path":"/etc/example.conf","content":"..."}}
{"type":"tool","tool":"system_replace_text","args":{"path":"/etc/example.conf","old":"a","new":"b","count":1}}

When finished:
{"type":"message","text":"your response"}
Never claim an action succeeded until the tool result confirms it.
""" % PROJECT_ROOT


def _raw_model_request(model, prompt, timeout_seconds):
    url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
    payload = {
        "systemInstruction": {"parts": [{"text": instructions()}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": API_KEY,
            "User-Agent": "AzzAgent-Gemini/1.4",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(e.code, body)
    except (socket.timeout, TimeoutError):
        raise GeminiTimeoutError("%s timed out after %ss" % (model, timeout_seconds))
    except urllib.error.URLError as e:
        if isinstance(getattr(e, "reason", None), socket.timeout):
            raise GeminiTimeoutError("%s timed out after %ss" % (model, timeout_seconds))
        raise RuntimeError("Network error: %s" % e)

    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts)
    if not text:
        raise RuntimeError("Gemini returned no text")
    return text


def _model_request(model, prompt, timeout_seconds):
    started = time.time()
    stop = threading.Event()

    print("[Sent] model=%s | timeout=%ss" % (model, timeout_seconds))
    print("[Thinking] waiting for Gemini...")

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            elapsed = int(time.time() - started)
            print("[Thinking] %s | %ss elapsed" % (model, elapsed))

    thread = threading.Thread(target=heartbeat)
    thread.daemon = True
    thread.start()

    try:
        text = _raw_model_request(model, prompt, timeout_seconds)
        elapsed = time.time() - started
        print("[Received] %s | %.1fs" % (model, elapsed))
        return text
    finally:
        stop.set()


def available_models():
    print("[Status] Fetching available Gemini models...")
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
    request = urllib.request.Request(
        url,
        headers={"x-goog-api-key": API_KEY, "User-Agent": "AzzAgent-Gemini/1.4"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_LIST_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(e.code, body)
    except (socket.timeout, TimeoutError):
        raise GeminiTimeoutError("Model list timed out after %ss" % MODEL_LIST_TIMEOUT)
    except urllib.error.URLError as e:
        raise RuntimeError("Network error while listing models: %s" % e)

    result = []
    for item in data.get("models", []):
        if "generateContent" not in item.get("supportedGenerationMethods", []):
            continue
        name = item.get("name", "")
        if name.startswith("models/"):
            name = name[7:]
        if name:
            result.append(name)
    return sorted(set(result), key=lambda s: s.lower())


def _model_score(name):
    low = name.lower()
    if "gemini" not in low or "flash" not in low:
        return -100000
    if any(bad in low for bad in ("embedding", "image", "tts", "live")):
        return -100000
    score = 0
    if not any(tag in low for tag in ("preview", "experimental", "exp")):
        score += 10000
    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", low)
    if match:
        score += int(match.group(1)) * 1000 + int(match.group(2) or 0) * 100
    return score


def fallback_models(current):
    models = [m for m in available_models() if m != current and _model_score(m) > -100000]
    def fallback_key(name):
        lite_bonus = 100000 if "lite" in name.lower() else 0
        return lite_bonus + _model_score(name)
    models.sort(key=fallback_key, reverse=True)
    return models


def call_gemini(prompt):
    global MODEL
    last_error = None

    for attempt in range(3):
        try:
            text = _model_request(MODEL, prompt, PRIMARY_TIMEOUT)
            save_model(MODEL)
            return text
        except GeminiTimeoutError as e:
            last_error = e
            print("[Timeout] %s" % e)
            break
        except GeminiHTTPError as e:
            last_error = e
            if e.code == 404:
                break
            if e.code not in TRANSIENT_HTTP_CODES:
                raise
            if attempt < 2:
                delay = (1, 2)[attempt]
                print("[Retry] %s HTTP %s | waiting %ss" % (MODEL, e.code, delay))
                time.sleep(delay)

    print("[Fallback] Looking for another available Flash model...")
    try:
        alternatives = fallback_models(MODEL)
    except Exception as discovery_error:
        print("[Fallback] Could not list models: %s" % discovery_error)
        if last_error:
            raise last_error
        raise

    if not alternatives:
        if last_error:
            raise last_error
        raise RuntimeError("No fallback Flash models available")

    for alternative in alternatives[:10]:
        print("[Fallback] Trying %s" % alternative)
        try:
            text = _model_request(alternative, prompt, FALLBACK_TIMEOUT)
            MODEL = alternative
            save_model(MODEL)
            print("[Model] Switched to %s and saved as default" % MODEL)
            return text
        except GeminiTimeoutError:
            print("[Fallback] %s timed out, skipping" % alternative)
        except GeminiHTTPError as e:
            last_error = e
            if e.code in TRANSIENT_HTTP_CODES or e.code == 404:
                print("[Fallback] %s HTTP %s, skipping" % (alternative, e.code))
                continue
            raise
        except RuntimeError as e:
            print("[Fallback] %s failed: %s" % (alternative, e))

    if last_error:
        raise last_error
    raise RuntimeError("No available Flash model responded")


def parse_action(raw):
    text = raw.strip()
    if text.startswith("```"):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        left = text.find("{")
        right = text.rfind("}")
        if left >= 0 and right > left:
            return json.loads(text[left:right + 1])
        raise


def build_prompt(user_text):
    top = []
    try:
        for item in sorted(PROJECT_ROOT.iterdir(), key=lambda x: x.name.lower())[:80]:
            top.append(item.name + ("/" if item.is_dir() else ""))
    except Exception:
        pass
    recent = "\n".join("%s: %s" % (entry["role"].upper(), entry["text"]) for entry in HISTORY[-24:])
    return "Project files:\n%s\n\nRecent session:\n%s\n\nUSER: %s" % ("\n".join(top), recent, user_text)


def describe_tool(name, args):
    if name in ("shell", "system_shell"):
        return "%s: %s" % (name, args.get("command", ""))
    if "path" in args:
        return "%s: %s" % (name, args.get("path"))
    return name


def run_turn(user_text):
    HISTORY.append({"role": "user", "text": user_text})
    prompt = build_prompt(user_text)

    for step in range(1, MAX_TOOL_STEPS + 1):
        raw = call_gemini(prompt)
        action = parse_action(raw)

        if action.get("type") == "message":
            text = action.get("text", "")
            print("\n[Reply]")
            print(text)
            HISTORY.append({"role": "assistant", "text": text})
            return

        if action.get("type") != "tool":
            print("[Error] Unknown action: %r" % action)
            return

        name = action.get("tool")
        args = action.get("args", {})
        print("\n[Tool %d/%d] %s" % (step, MAX_TOOL_STEPS, describe_tool(name, args)))

        if name not in TOOLS:
            result = "ERROR: unknown tool %s" % name
        else:
            try:
                started = time.time()
                result = TOOLS[name](args)
                if name not in ("shell", "system_shell"):
                    print("[Done] %s | %.1fs" % (name, time.time() - started))
            except Exception as e:
                result = "ERROR: %s" % e

        print(result[:4000])
        HISTORY.append({"role": "tool", "text": result})
        prompt = build_prompt("Continue the task. Latest tool result:\n" + result)

    print("[Stopped] Reached %d tool steps" % MAX_TOOL_STEPS)


def show_models():
    try:
        models = available_models()
    except Exception as e:
        print("Could not list models: %s" % e)
        return
    if not models:
        print("No generateContent models returned for this API key.")
        return
    print("Available models (%d):" % len(models))
    for name in models:
        marker = "  < current" if name == MODEL else ""
        print("  %s%s" % (name, marker))
    print("\nSwitch with: /model MODEL_ID")


def main():
    global MODEL, AUTO_APPROVE, API_KEY
    MODEL = load_saved_model()

    print("AzzAgent Gemini 1.4")
    print("Project root: %s" % PROJECT_ROOT)
    print("System read: /")
    print("Model: %s" % MODEL)
    print("Project approval default: YES")
    print("System approval default: YES")
    print("Primary timeout: %ss | fallback: %ss" % (PRIMARY_TIMEOUT, FALLBACK_TIMEOUT))
    print("Commands: /model, /model ID, /yes, /no, /forget-key, /quit")

    API_KEY = load_or_create_key()
    if not API_KEY:
        print("No Gemini API key supplied.")
        return 1

    while True:
        try:
            user_text = input("\nYou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_text:
            continue
        if user_text in ("/quit", "/exit", "quit", "exit"):
            break
        if user_text in ("/model", "/models"):
            show_models()
            continue
        if user_text.startswith("/model "):
            MODEL = user_text.split(None, 1)[1].strip()
            save_model(MODEL)
            print("[Model] %s (saved as default)" % MODEL)
            continue
        if user_text == "/yes":
            AUTO_APPROVE = True
            print("[Approval] Project auto-approve ON. System prompts remain, default YES.")
            continue
        if user_text == "/no":
            AUTO_APPROVE = False
            print("[Approval] Project auto-approve OFF. Project/system blank approval = YES.")
            continue
        if user_text == "/forget-key":
            try:
                KEY_FILE.unlink()
                print("Saved Gemini API key removed. Restart to enter a new one.")
            except FileNotFoundError:
                print("No saved Gemini API key found.")
            continue

        print("[You] %s" % user_text)
        try:
            run_turn(user_text)
        except Exception as e:
            print("[ERROR] %s" % e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
