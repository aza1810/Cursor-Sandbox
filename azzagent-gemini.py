#!/usr/local/bin/python3.9
"""
AzzAgent Gemini 1.2
32-bit-friendly coding/system agent for Python 3.9.

- Reads may inspect the whole Linux filesystem.
- Normal file writes stay inside the project root.
- System writes and system shell commands always require explicit approval.
- Project actions default to YES when the approval prompt is left blank.
- API key is entered once, shown while typing, then saved locally.
- /model lists every generateContent model available to the current API key.
- The last successfully selected/used model is remembered across restarts.
- Gemini 429/5xx errors are retried and may fall back to another Flash model.
"""

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

DEFAULT_MODEL = "gemini-3.6-flash"
MODEL = DEFAULT_MODEL
PROJECT_ROOT = Path.cwd().resolve()
SYSTEM_ROOT = Path("/")
AUTO_APPROVE = False
MAX_TOOL_STEPS = 16
MAX_OUTPUT_CHARS = 30000
HISTORY = []
API_KEY = None

KEY_FILE = Path.home() / ".azzagent_gemini_key"
MODEL_FILE = Path.home() / ".azzagent_model"
TRANSIENT_HTTP_CODES = (429, 500, 502, 503, 504)


class GeminiHTTPError(Exception):
    def __init__(self, code, body):
        self.code = int(code)
        self.body = body
        Exception.__init__(self, "Gemini API HTTP %s: %s" % (self.code, body[:2000]))


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
    print("API key saved to %s" % KEY_FILE)
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
        print("[AzzAgent] Warning: could not save model preference: %s" % e)


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
        return True
    answer = input("\n%s\nApprove? [Y/n] " % message).strip().lower()
    return answer in ("", "y", "yes")


def system_approve(message):
    answer = input("\nSYSTEM ACTION: %s\nExplicitly approve? [y/N] " % message).strip().lower()
    return answer in ("y", "yes")


def list_files(args):
    base = resolve_read_path(args.get("path", "."))
    recursive = bool(args.get("recursive", False))
    out = []
    if base.is_file():
        return str(base)
    if not base.exists():
        return "ERROR: path does not exist"
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
    except PermissionError:
        return "ERROR: permission denied: %s" % base
    except OSError as e:
        return "ERROR: %s" % e
    return "\n".join(out)


def read_file(args):
    p = resolve_read_path(args["path"])
    if not p.is_file():
        return "ERROR: not a readable file: %s" % p
    try:
        lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    except PermissionError:
        return "ERROR: permission denied: %s" % p
    except OSError as e:
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


def shell(args):
    command = args["command"]
    if not normal_approve("RUN PROJECT SHELL: %s" % command):
        return "DENIED"
    try:
        result = subprocess.run(
            command,
            cwd=str(PROJECT_ROOT),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=120,
        )
        return ("exit=%d\n%s" % (result.returncode, result.stdout or ""))[:MAX_OUTPUT_CHARS]
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out"


def system_shell(args):
    command = args["command"]
    if not system_approve("RUN SYSTEM SHELL: %s" % command):
        return "DENIED"
    try:
        result = subprocess.run(
            command,
            cwd="/",
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            universal_newlines=True,
            timeout=120,
        )
        return ("exit=%d\n%s" % (result.returncode, result.stdout or ""))[:MAX_OUTPUT_CHARS]
    except subprocess.TimeoutExpired:
        return "ERROR: command timed out"


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
For changes outside the project root, use system_write_file/system_replace_text or system_shell. These require explicit user approval.
Project shell/file actions may be approved by the user's configured default.
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


def _model_request(model, prompt):
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
            "User-Agent": "AzzAgent-Gemini/1.2",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(e.code, body)
    except urllib.error.URLError as e:
        raise RuntimeError("Network error: %s" % e)
    candidates = data.get("candidates", [])
    if not candidates:
        raise RuntimeError("Gemini returned no candidates")
    parts = candidates[0].get("content", {}).get("parts", [])
    text = "".join(part.get("text", "") for part in parts)
    if not text:
        raise RuntimeError("Gemini returned no text")
    return text


def available_models():
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
    request = urllib.request.Request(
        url,
        headers={
            "x-goog-api-key": API_KEY,
            "User-Agent": "AzzAgent-Gemini/1.2",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise GeminiHTTPError(e.code, body)
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
    if "lite" in low:
        score -= 50
    return score


def fallback_models(current):
    models = [m for m in available_models() if m != current and _model_score(m) > -100000]
    models.sort(key=_model_score, reverse=True)
    return models


def call_gemini(prompt):
    global MODEL
    last_error = None

    for attempt in range(3):
        try:
            text = _model_request(MODEL, prompt)
            save_model(MODEL)
            return text
        except GeminiHTTPError as e:
            last_error = e
            if e.code == 404:
                break
            if e.code not in TRANSIENT_HTTP_CODES:
                raise
            if attempt < 2:
                delay = (2, 5)[attempt]
                print("\n[Gemini] %s unavailable (HTTP %s). Retrying in %ss..." % (MODEL, e.code, delay))
                time.sleep(delay)

    if last_error and (last_error.code == 404 or last_error.code in TRANSIENT_HTTP_CODES):
        print("\n[Gemini] Looking for another available Flash model...")
        try:
            alternatives = fallback_models(MODEL)
        except Exception as discovery_error:
            print("[Gemini] Could not list fallback models: %s" % discovery_error)
            raise last_error

        for alternative in alternatives[:8]:
            print("[Gemini] Trying %s..." % alternative)
            try:
                text = _model_request(alternative, prompt)
                MODEL = alternative
                save_model(MODEL)
                print("[Gemini] Switched to %s (saved as default)" % MODEL)
                return text
            except GeminiHTTPError as e:
                last_error = e
                if e.code in TRANSIENT_HTTP_CODES or e.code == 404:
                    continue
                raise
            except RuntimeError:
                continue

    if last_error:
        raise last_error
    raise RuntimeError("Gemini request failed")


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


def run_turn(user_text):
    HISTORY.append({"role": "user", "text": user_text})
    prompt = build_prompt(user_text)

    for _ in range(MAX_TOOL_STEPS):
        action = parse_action(call_gemini(prompt))

        if action.get("type") == "message":
            text = action.get("text", "")
            print("\nAgent: " + text)
            HISTORY.append({"role": "assistant", "text": text})
            return

        if action.get("type") != "tool":
            print("\nAgent error: unknown action: %r" % action)
            return

        name = action.get("tool")
        args = action.get("args", {})
        print("\n[" + str(name) + "]")

        if name not in TOOLS:
            result = "ERROR: unknown tool %s" % name
        else:
            try:
                result = TOOLS[name](args)
            except Exception as e:
                result = "ERROR: %s" % e

        print(result[:4000])
        HISTORY.append({"role": "tool", "text": result})
        prompt = build_prompt("Continue the task. Latest tool result:\n" + result)

    print("\nStopped after %d tool steps." % MAX_TOOL_STEPS)


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

    print("AzzAgent Gemini 1.2")
    print("Project root: %s" % PROJECT_ROOT)
    print("System read: /")
    print("Model: %s" % MODEL)
    print("Project approval default: YES")
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
            print("Model: %s (saved as default)" % MODEL)
            continue

        if user_text == "/yes":
            AUTO_APPROVE = True
            print("Project auto-approve ON. System actions still require approval.")
            continue

        if user_text == "/no":
            AUTO_APPROVE = False
            print("Project auto-approve OFF. Blank approval still means YES.")
            continue

        if user_text == "/forget-key":
            try:
                KEY_FILE.unlink()
                print("Saved Gemini API key removed. Restart to enter a new one.")
            except FileNotFoundError:
                print("No saved Gemini API key found.")
            continue

        try:
            run_turn(user_text)
        except Exception as e:
            print("\nERROR: %s" % e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
