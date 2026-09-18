#!/usr/local/bin/python3.9
import getpass,json,os,subprocess,sys,urllib.request,urllib.error
from pathlib import Path
API='https://api.openai.com/v1/responses'
MODEL=os.environ.get('AZZ_MODEL','gpt-5.6-luna')
ROOT=Path.cwd().resolve(); AUTO=False; HIST=[]

def safe(p):
    q=Path(p); q=(ROOT/q if not q.is_absolute() else q).resolve()
    if q!=ROOT and ROOT not in q.parents: raise ValueError('outside project root')
    return q

def approve(msg):
    if AUTO: return True
    return input('\n'+msg+'\nApprove? [y/N] ').strip().lower() in ('y','yes')

def tool(name,a):
    if name=='list_files':
        p=safe(a.get('path','.')); rec=a.get('recursive',False); out=[]
        if rec:
            for d,dirs,files in os.walk(str(p)):
                dirs[:]=[x for x in dirs if x not in ('.git','node_modules','__pycache__')]
                for x in sorted(dirs): out.append(str((Path(d)/x).relative_to(ROOT))+'/')
                for x in sorted(files): out.append(str((Path(d)/x).relative_to(ROOT)))
                if len(out)>250: return '\n'.join(out[:250])+'\n...truncated'
        else:
            for x in sorted(p.iterdir(),key=lambda x:x.name.lower()):
                out.append(str(x.relative_to(ROOT))+('/' if x.is_dir() else ''))
        return '\n'.join(out)
    if name=='read_file':
        p=safe(a['path']); lines=p.read_text(encoding='utf-8',errors='replace').splitlines()
        s=max(1,int(a.get('start_line',1))); e=int(a.get('end_line',0)) or min(len(lines),s+399)
        return '\n'.join('%d: %s'%(i,v) for i,v in enumerate(lines[s-1:e],s))[:30000]
    if name=='write_file':
        p=safe(a['path']); data=a.get('content','')
        if not approve('WRITE '+str(p.relative_to(ROOT))): return 'DENIED'
        p.parent.mkdir(parents=True,exist_ok=True); p.write_text(data,encoding='utf-8'); return 'WROTE '+str(p.relative_to(ROOT))
    if name=='replace_text':
        p=safe(a['path']); old=a['old']; new=a['new']; count=int(a.get('count',1)); data=p.read_text(encoding='utf-8')
        if old not in data: return 'ERROR: old text not found'
        if not approve('EDIT '+str(p.relative_to(ROOT))): return 'DENIED'
        p.write_text(data.replace(old,new,count),encoding='utf-8'); return 'EDITED '+str(p.relative_to(ROOT))
    if name=='shell':
        c=a['command']
        if not approve('RUN: '+c): return 'DENIED'
        try:
            r=subprocess.run(c,cwd=str(ROOT),shell=True,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,universal_newlines=True,timeout=120)
            return ('exit=%d\n%s'%(r.returncode,r.stdout or ''))[:30000]
        except subprocess.TimeoutExpired: return 'ERROR: timeout'
    return 'ERROR: unknown tool'

def instr():
    return '''You are AzzAgent, a coding agent on 32-bit Linux. Project root: %s
Return EXACTLY one JSON object, no markdown.
Use tools iteratively and inspect existing files before editing.
Tool calls:
{"type":"tool","tool":"list_files","args":{"path":".","recursive":false}}
{"type":"tool","tool":"read_file","args":{"path":"file.py","start_line":1,"end_line":200}}
{"type":"tool","tool":"write_file","args":{"path":"file.py","content":"..."}}
{"type":"tool","tool":"replace_text","args":{"path":"file.py","old":"exact text","new":"replacement","count":1}}
{"type":"tool","tool":"shell","args":{"command":"python3.9 test.py"}}
When done: {"type":"message","text":"..."}
Never claim a tool succeeded until its result confirms it.'''%ROOT

def api(key,prompt):
    body=json.dumps({'model':MODEL,'instructions':instr(),'input':prompt}).encode()
    req=urllib.request.Request(API,data=body,headers={'Authorization':'Bearer '+key,'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=180) as r: j=json.loads(r.read().decode())
    except urllib.error.HTTPError as e: raise RuntimeError('API HTTP %s: %s'%(e.code,e.read().decode(errors='replace')[:1500]))
    for item in j.get('output',[]):
        if item.get('type')=='message':
            for c in item.get('content',[]):
                if c.get('type')=='output_text': return c.get('text','')
    raise RuntimeError('No text output')

def parse(s):
    s=s.strip()
    if s.startswith('```'):
        ls=s.splitlines()[1:]
        if ls and ls[-1].strip().startswith('```'): ls=ls[:-1]
        s='\n'.join(ls)
    try:return json.loads(s)
    except:
        a=s.find('{'); b=s.rfind('}')
        if a>=0 and b>a:return json.loads(s[a:b+1])
        raise

def prompt(user):
    top='\n'.join(x.name+('/' if x.is_dir() else '') for x in list(ROOT.iterdir())[:80])
    h='\n'.join('%s: %s'%(x['role'],x['text']) for x in HIST[-24:])
    return 'Top-level files:\n%s\n\nRecent session:\n%s\nUSER: %s'%(top,h,user)

def turn(key,user):
    HIST.append({'role':'user','text':user}); p=prompt(user)
    for _ in range(12):
        a=parse(api(key,p))
        if a.get('type')=='message':
            t=a.get('text',''); print('\nAgent: '+t); HIST.append({'role':'assistant','text':t}); return
        if a.get('type')!='tool': print('Bad action:',a); return
        n=a.get('tool'); ar=a.get('args',{}); print('\n['+str(n)+']')
        try:r=tool(n,ar)
        except Exception as e:r='ERROR: '+str(e)
        print(r[:4000]); HIST.append({'role':'tool','text':r}); p=prompt('Continue. Latest tool result:\n'+r)
    print('Stopped after 12 tool steps.')

def main():
    global MODEL,AUTO
    print('AzzAgent 0.1 | Project:',ROOT,'| Model:',MODEL)
    print('Commands: /model ID  /yes  /no  /quit')
    key=os.environ.get('OPENAI_API_KEY') or getpass.getpass('OpenAI API key: ').strip()
    if not key:return 1
    while True:
        try:u=input('\nYou> ').strip()
        except (EOFError,KeyboardInterrupt):print();break
        if not u:continue
        if u in ('/quit','/exit','quit','exit'):break
        if u.startswith('/model '): MODEL=u.split(None,1)[1].strip(); print('Model:',MODEL); continue
        if u=='/yes': AUTO=True; print('Auto-approve ON'); continue
        if u=='/no': AUTO=False; print('Auto-approve OFF'); continue
        try:turn(key,u)
        except Exception as e:print('\nERROR:',e)
    return 0
if __name__=='__main__':sys.exit(main())
