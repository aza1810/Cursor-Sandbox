#!/usr/local/bin/python3.9
import getpass, json, os, subprocess, sys, urllib.request, urllib.error
from pathlib import Path

MODEL = os.environ.get('AZZ_MODEL', 'gemini-3.8-flash')
ROOT = Path.cwd().resolve()
AUTO = False
HIST = []
MAX_TOOL_STEPS = 12


def safe(path):
    p = Path(path)
    p = (ROOT / p if not p.is_absolute() else p).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError('Path is outside the project root')
    return p


def approve(message):
    if AUTO:
        return True
    return input('\n' + message + '\nApprove? [y/N] ').strip().lower() in ('y', 'yes')


def run_tool(name, args):
    if name == 'list_files':
        base = safe(args.get('path', '.'))
        recursive = bool(args.get('recursive', False))
        out = []
        if base.is_file():
            return str(base.relative_to(ROOT))
        if recursive:
            for current, dirs, files in os.walk(str(base)):
                dirs[:] = sorted(d for d in dirs if d not in ('.git', 'node_modules', '__pycache__'))
                cur = Path(current)
                for d in dirs:
                    out.append(str((cur / d).relative_to(ROOT)) + '/')
                    if len(out) >= 250:
                        return '\n'.join(out) + '\n...truncated'
                for f in sorted(files):
                    out.append(str((cur / f).relative_to(ROOT)))
                    if len(out) >= 250:
                        return '\n'.join(out) + '\n...truncated'
        else:
            for item in sorted(base.iterdir(), key=lambda x: x.name.lower()):
                out.append(str(item.relative_to(ROOT)) + ('/' if item.is_dir() else ''))
        return '\n'.join(out)

    if name == 'read_file':
        p = safe(args['path'])
        lines = p.read_text(encoding='utf-8', errors='replace').splitlines()
        start = max(1, int(args.get('start_line', 1)))
        end = int(args.get('end_line', 0)) or min(len(lines), start + 399)
        text = '\n'.join('%d: %s' % (i, line) for i, line in enumerate(lines[start - 1:end], start))
        return text[:30000]

    if name == 'write_file':
        p = safe(args['path'])
        content = args.get('content', '')
        if not approve('WRITE ' + str(p.relative_to(ROOT))):
            return 'DENIED'
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding='utf-8')
        return 'WROTE ' + str(p.relative_to(ROOT))

    if name == 'replace_text':
        p = safe(args['path'])
        old = args['old']
        new = args['new']
        count = int(args.get('count', 1))
        data = p.read_text(encoding='utf-8')
        if old not in data:
            return 'ERROR: old text not found'
        if not approve('EDIT ' + str(p.relative_to(ROOT))):
            return 'DENIED'
        p.write_text(data.replace(old, new, count), encoding='utf-8')
        return 'EDITED ' + str(p.relative_to(ROOT))

    if name == 'shell':
        command = args['command']
        if not approve('RUN: ' + command):
            return 'DENIED'
        try:
            result = subprocess.run(command, cwd=str(ROOT), shell=True,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    universal_newlines=True, timeout=120)
            return ('exit=%d\n%s' % (result.returncode, result.stdout or ''))[:30000]
        except subprocess.TimeoutExpired:
            return 'ERROR: command timed out'

    return 'ERROR: unknown tool'


def instructions():
    return '''You are AzzAgent, a coding agent running on a very small 32-bit Linux computer.
Project root: %s

Work iteratively. Inspect existing files before editing them. Do not invent file contents.
Use tools to inspect, edit, create and test code.

Return EXACTLY one JSON object and no markdown.

Tool calls:
{"type":"tool","tool":"list_files","args":{"path":".","recursive":false}}
{"type":"tool","tool":"read_file","args":{"path":"file.py","start_line":1,"end_line":200}}
{"type":"tool","tool":"write_file","args":{"path":"file.py","content":"..."}}
{"type":"tool","tool":"replace_text","args":{"path":"file.py","old":"exact old text","new":"replacement","count":1}}
{"type":"tool","tool":"shell","args":{"command":"python3.9 test.py"}}

When finished or when you need to speak to the user:
{"type":"message","text":"your response"}

Never claim an edit or command succeeded until the tool result confirms it.
''' % ROOT


def call_gemini(api_key, prompt):
    url = 'https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent' % MODEL
    payload = {
        'systemInstruction': {'parts': [{'text': instructions()}]},
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': {
            'responseMimeType': 'application/json',
            'temperature': 0.2
        }
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'x-goog-api-key': api_key,
            'User-Agent': 'AzzAgent-Gemini/0.2'
        },
        method='POST'
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as r:
            data = json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        body = e.read().decode('utf-8', errors='replace')
        raise RuntimeError('Gemini API HTTP %s: %s' % (e.code, body[:2000]))
    except urllib.error.URLError as e:
        raise RuntimeError('Network error: %s' % e)

    candidates = data.get('candidates', [])
    if not candidates:
        raise RuntimeError('Gemini returned no candidates: %s' % str(data)[:1500])
    parts = candidates[0].get('content', {}).get('parts', [])
    text = ''.join(p.get('text', '') for p in parts)
    if not text:
        raise RuntimeError('Gemini returned no text')
    return text


def parse_action(raw):
    text = raw.strip()
    if text.startswith('```'):
        lines = text.splitlines()[1:]
        if lines and lines[-1].strip().startswith('```'):
            lines = lines[:-1]
        text = '\n'.join(lines).strip()
    try:
        return json.loads(text)
    except Exception:
        left = text.find('{')
        right = text.rfind('}')
        if left >= 0 and right > left:
            return json.loads(text[left:right + 1])
        raise


def build_prompt(user_text):
    top = []
    try:
        for item in sorted(ROOT.iterdir(), key=lambda x: x.name.lower())[:80]:
            top.append(item.name + ('/' if item.is_dir() else ''))
    except Exception:
        pass
    recent = '\n'.join('%s: %s' % (x['role'].upper(), x['text']) for x in HIST[-24:])
    return 'Top-level files:\n%s\n\nRecent session:\n%s\n\nUSER: %s' % ('\n'.join(top), recent, user_text)


def run_turn(api_key, user_text):
    HIST.append({'role': 'user', 'text': user_text})
    prompt = build_prompt(user_text)

    for _ in range(MAX_TOOL_STEPS):
        action = parse_action(call_gemini(api_key, prompt))

        if action.get('type') == 'message':
            text = action.get('text', '')
            print('\nAgent: ' + text)
            HIST.append({'role': 'assistant', 'text': text})
            return

        if action.get('type') != 'tool':
            print('\nAgent error: unknown action: %r' % action)
            return

        name = action.get('tool')
        args = action.get('args', {})
        print('\n[' + str(name) + ']')
        try:
            result = run_tool(name, args)
        except Exception as e:
            result = 'ERROR: ' + str(e)
        print(result[:4000])
        HIST.append({'role': 'tool', 'text': result})
        prompt = build_prompt('Continue the task. The latest tool result is:\n' + result)

    print('\nStopped after %d tool steps to prevent a runaway loop.' % MAX_TOOL_STEPS)


def main():
    global MODEL, AUTO
    print('AzzAgent Gemini 0.2')
    print('Project: %s' % ROOT)
    print('Model:   %s' % MODEL)
    print('Commands: /model ID, /yes, /no, /quit')

    api_key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
    if not api_key:
        api_key = getpass.getpass('Gemini API key: ').strip()
    if not api_key:
        print('No Gemini API key supplied.')
        return 1

    while True:
        try:
            user_text = input('\nYou> ').strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_text:
            continue
        if user_text in ('/quit', '/exit', 'quit', 'exit'):
            break
        if user_text.startswith('/model '):
            MODEL = user_text.split(None, 1)[1].strip()
            print('Model: ' + MODEL)
            continue
        if user_text == '/yes':
            AUTO = True
            print('Auto-approve ON')
            continue
        if user_text == '/no':
            AUTO = False
            print('Auto-approve OFF')
            continue

        try:
            run_turn(api_key, user_text)
        except Exception as e:
            print('\nERROR: %s' % e)

    return 0


if __name__ == '__main__':
    sys.exit(main())
