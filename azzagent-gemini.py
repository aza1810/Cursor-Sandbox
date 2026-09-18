#!/usr/local/bin/python3.9
"""
AzzAgent Gemini 1.6
Tiny 32-bit-friendly coding/system agent for Python 3.9.

Features:
- Read anywhere on the Linux filesystem.
- Normal writes stay inside the project root.
- Project AND system approval prompts default to YES when left blank.
- Shell/system-shell output streams live while commands run.
- Clear status for Sent, Thinking, Received, Tool, Command, Recover and Reply.
- /model lists all generateContent models available to the API key.
- Last successfully used model is remembered across restarts.
- Malformed Gemini tool JSON is automatically repaired and retried.
- Tool/command failures are fed back to Gemini so it can diagnose and fix them.
"""

import json
import os
from pathlib import Path
import re
import select
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
MAX_PROTOCOL_REPAIRS = 3
MAX_OUTPUT_CHARS = 30000
HISTORY = []
API_KEY = None

KEY_FILE = Path.home() / ".azzagent_gemini_key"
MODEL_FILE = Path.home() / ".azzagent_model"
TRANSIENT_HTTP_CODES = (429, 500, 502, 503, 504)

PRIMARY_TIMEOUT = 25
FALLBACK_TIMEOUT = 10
MODEL_LIST_TIMEOUT = 20
COMMAND_TIMEOUT = 120
HEARTBEAT_SECONDS = 3
COMMAND_HEARTBEAT_SECONDS = 5


class GeminiHTTPError(Exception):
    def __init__(self, code, body):
        self.code = int(code)
        self.body = body
        Exception.__init__(self, "Gemini API HTTP %s: %s" % (self.code, body[:2000]))


class GeminiTimeoutError(Exception):
    pass


class ProtocolError(Exception):
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


def _terminate_process(proc):
    try:
        proc.terminate()
        proc.wait(timeout=2)
        return
    except Exception:
        pass
    try:
        proc.kill()
    except Exception:
        pass


def _run_shell(command, cwd, approval_message, system=False):
    approve = system_approve if system else normal_approve
    if not approve(approval_message):
        return "DENIED"

    started = time.time()
    last_output = started
    next_heartbeat = started + COMMAND_HEARTBEAT_SECONDS
    captured = []
    captured_chars = 0

    print("[Command] %s" % command)
    print("[Running] cwd=%s | live output follows" % cwd)
    sys.stdout.flush()

    try:
        proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
        )
    except Exception as e:
        print("[Done] failed to start: %s" % e)
        return "ERROR: could not start command: %s" % e

    fd = proc.stdout.fileno()
    try:
        while True:
            elapsed = time.time() - started
            if elapsed >= COMMAND_TIMEOUT:
                _terminate_process(proc)
                print("\n[Done] TIMEOUT after %ss" % COMMAND_TIMEOUT)
                return "ERROR: command timed out after %s seconds\n%s" % (
                    COMMAND_TIMEOUT, "".join(captured)[:MAX_OUTPUT_CHARS])

            ready, _, _ = select.select([fd], [], [], 0.5)
            if ready:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    text = chunk.decode("utf-8", errors="replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    last_output = time.time()
                    if captured_chars < MAX_OUTPUT_CHARS:
                        remaining = MAX_OUTPUT_CHARS - captured_chars
                        piece = text[:remaining]
                        captured.append(piece)
                        captured_chars += len(piece)
                    continue

            if proc.poll() is not None:
                while True:
                    try:
                        ready, _, _ = select.select([fd], [], [], 0)
                        if not ready:
                            break
                        chunk = os.read(fd, 4096)
                    except OSError:
                        break
                    if not chunk:
                        break
                    text = chunk.decode("utf-8", errors="replace")
                    sys.stdout.write(text)
                    sys.stdout.flush()
                    if captured_chars < MAX_OUTPUT_CHARS:
                        remaining = MAX_OUTPUT_CHARS - captured_chars
                        piece = text[:remaining]
                        captured.append(piece)
                        captured_chars += len(piece)
                break

            now = time.time()
            if now >= next_heartbeat:
                quiet_for = int(now - last_output)
                print("\n[Running] %.0fs elapsed | no output for %ss" % (now - started, quiet_for))
                sys.stdout.flush()
                next_heartbeat = now + COMMAND_HEARTBEAT_SECONDS

    except KeyboardInterrupt:
        _terminate_process(proc)
        print("\n[Stopped] command interrupted by user")
        raise
    finally:
        try:
            proc.stdout.close()
        except Exception:
            pass

    returncode = proc.wait()
    elapsed = time.time() - started
    print("\n[Done] exit=%d | %.1fs" % (returncode, elapsed))
    sys.stdout.flush()
    return ("exit=%d\n%s" % (returncode, "".join(captured)))[:MAX_OUTPUT_CHARS]


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
    return """You are AzzAgent, a coding and Linux system agent on a small 32-bit Tiny Core Linux machine.
Project root: %s
System root: /

You may READ anywhere using list_files/read_file.
Normal write_file/replace_text are restricted to the project root.
For changes outside the project root, use system_write_file/system_replace_text or system_shell.
Both project and system approval prompts default to YES when the user presses Enter.
Shell command output is streamed live to the user.

IMPORTANT ERROR BEHAVIOUR:
- When a tool or command returns ERROR or a non-zero exit code, read the complete result, diagnose it, and try a sensible corrective action automatically.
- Do not stop at the first failed command unless user input is genuinely required or no safe route remains.
- Inspect before editing and verify fixes afterwards.
- Never claim success until a tool result confirms it.

You may modify AzzAgent itself when the user explicitly asks.
Return EXACTLY one JSON object and no markdown or extra text.

Valid actions:
{"type":"tool","tool":"list_files","args":{"path":"/etc","recursive":false}}
{"type":"tool","tool":"read_file","args":{"path":"/etc/os-release","start_line":1,"end_line":100}}
{"type":"tool","tool":"write_file","args":{"path":"main.py","content":"..."}}
{"type":"tool","tool":"replace_text","args":{"path":"main.py","old":"x","new":"y","count":1}}
{"type":"tool","tool":"shell","args":{"command":"python3.9 test.py"}}
{"type":"tool","tool":"system_shell","args":{"command":"uname -a"}}
{"type":"tool","tool":"system_write_file","args":{"path":"/etc/example.conf","content":"..."}}
{"type":"tool","tool":"system_replace_text","args":{"path":"/etc/example.conf","old":"a","new":"b","count":1}}
{"type":"message","text":"your response"}
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
            "User-Agent": "AzzAgent-Gemini/1.6",
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
    sys.stdout.flush()

    def heartbeat():
        while not stop.wait(HEARTBEAT_SECONDS):
            print("[Thinking] %s | %ss elapsed" % (model, int(time.time() - started)))
            sys.stdout.flush()

    thread = threading.Thread(target=heartbeat)
    thread.daemon = True
    thread.start()
    try:
        text = _raw_model_request(model, prompt, timeout_seconds)
        print("[Received] %s | %.1fs" % (model, time.time() - started))
        return text
    finally:
        stop.set()


def available_models():
    print("[Status] Fetching available Gemini models...")
    sys.stdout.flush()
    url = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
    request = urllib.request.Request(
        url,
        headers={"x-goog-api-key": API_KEY, "User-Agent": "AzzAgent-Gemini/1.6"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=MODEL_LIST_TIMEOUT) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise GeminiHTTPError(e.code, e.read().decode("utf-8", errors="replace"))
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
    score = 10000 if not any(tag in low for tag in ("preview", "experimental", "exp")) else 0
    match = re.search(r"gemini-(\d+)(?:\.(\d+))?", low)
    if match:
        score += int(match.group(1)) * 1000 + int(match.group(2) or 0) * 100
    return score


def fallback_models(current):
    models = [m for m in available_models() if m != current and _model_score(m) > -100000]
    models.sort(key=lambda n: (100000 if "lite" in n.lower() else 0) + _model_score(n), reverse=True)
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
    except Exception as e:
        print("[Fallback] Could not list models: %s" % e)
        if last_error:
            raise last_error
        raise

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
        obj, _ = json.JSONDecoder().raw_decode(text)
        return obj
    except Exception:
        left = text.find("{")
        if left >= 0:
            try:
                obj, _ = json.JSONDecoder().raw_decode(text[left:])
                return obj
            except Exception:
                pass
    raise ProtocolError("Could not parse Gemini action JSON")


def validate_action(action):
    if not isinstance(action, dict):
        raise ProtocolError("Gemini action is not an object")
    kind = action.get("type")
    if kind == "message":
        if "text" not in action:
            raise ProtocolError("message action is missing text")
        return action
    if kind == "tool":
        name = action.get("tool")
        if name not in TOOLS:
            raise ProtocolError("unknown tool: %s" % name)
        if not isinstance(action.get("args", {}), dict):
            raise ProtocolError("tool args must be an object")
        return action
    raise ProtocolError("unknown action type: %r" % kind)


def build_prompt(user_text):
    top = []
    try:
        for item in sorted(PROJECT_ROOT.iterdir(), key=lambda x: x.name.lower())[:80]:
            top.append(item.name + ("/" if item.is_dir() else ""))
    except Exception:
        pass
    recent = "\n".join("%s: %s" % (entry["role"].upper(), entry["text"]) for entry in HISTORY[-24:])
    return "Project files:\n%s\n\nRecent session:\n%s\n\nUSER: %s" % ("\n".join(top), recent, user_text)


def obtain_action(prompt):
    current_prompt = prompt
    for repair in range(MAX_PROTOCOL_REPAIRS + 1):
        raw = call_gemini(current_prompt)
        try:
            return validate_action(parse_action(raw))
        except ProtocolError as e:
            if repair >= MAX_PROTOCOL_REPAIRS:
                raise
            print("[Recover] Gemini returned an invalid agent action: %s" % e)
            print("[Recover] Asking Gemini to repair its response (%d/%d)..." % (repair + 1, MAX_PROTOCOL_REPAIRS))
            sys.stdout.flush()
            current_prompt = build_prompt(
                "PROTOCOL ERROR. Your previous response could not be executed. "
                "Error: %s\nPrevious response:\n%s\n\n"
                "Return exactly ONE valid JSON object only. Do not use markdown. "
                "Use type=message or one of these tools: %s. Continue the original task."
                % (e, raw[:2000], ", ".join(sorted(TOOLS.keys())))
            )
    raise ProtocolError("Could not obtain a valid action")


def describe_tool(name, args):
    if name in ("shell", "system_shell"):
        return "%s: %s" % (name, args.get("command", ""))
    if "path" in args:
        return "%s: %s" % (name, args.get("path"))
    return name


def result_failed(result):
    if not result:
        return False
    first = result.splitlines()[0].strip()
    if first.startswith("ERROR") or first == "DENIED":
        return True
    match = re.match(r"exit=(-?\d+)", first)
    return bool(match and int(match.group(1)) != 0)


def run_turn(user_text):
    HISTORY.append({"role": "user", "text": user_text})
    prompt = build_prompt(user_text)

    for step in range(1, MAX_TOOL_STEPS + 1):
        action = obtain_action(prompt)
        if action.get("type") == "message":
            text = action.get("text", "")
            print("\n[Reply]")
            print(text)
            HISTORY.append({"role": "assistant", "text": text})
            return

        name = action["tool"]
        args = action.get("args", {})
        print("\n[Tool %d/%d] %s" % (step, MAX_TOOL_STEPS, describe_tool(name, args)))

        try:
            started = time.time()
            result = TOOLS[name](args)
            if name not in ("shell", "system_shell"):
                print("[Done] %s | %.1fs" % (name, time.time() - started))
        except Exception as e:
            result = "ERROR: %s" % e
            print("[Tool error] %s" % e)

        if name in ("shell", "system_shell"):
            first_line = result.splitlines()[0] if result else ""
            print("[Result] %s" % first_line)
        else:
            print(result[:4000])

        HISTORY.append({"role": "tool", "text": result})

        if result_failed(result):
            print("[Recover] Tool/command failed. Sending the error back to Gemini to diagnose and fix...")
            prompt = build_prompt(
                "The last tool/command FAILED. Diagnose the actual error, inspect anything needed, "
                "and try a sensible corrective action automatically. Do not merely report the failure. "
                "Latest tool result:\n" + result
            )
        else:
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

    print("AzzAgent Gemini 1.6")
    print("Project root: %s" % PROJECT_ROOT)
    print("System read: /")
    print("Model: %s" % MODEL)
    print("Project approval default: YES")
    print("System approval default: YES")
    print("Command output: LIVE")
    print("Error recovery: ON")
    print("Protocol self-repair: ON (%d retries)" % MAX_PROTOCOL_REPAIRS)
    print("Primary timeout: %ss | fallback: %ss | command: %ss" % (
        PRIMARY_TIMEOUT, FALLBACK_TIMEOUT, COMMAND_TIMEOUT))
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
            print("[Approval] Project auto-approve OFF. Blank project/system approval = YES.")
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
        except ProtocolError as e:
            print("[ERROR] Agent protocol could not recover: %s" % e)
        except Exception as e:
            print("[ERROR] %s" % e)

    return 0


if __name__ == "__main__":
    sys.exit(main())
