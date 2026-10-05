import os, sys, json, sqlite3, threading, time, hashlib, secrets, webbrowser, subprocess
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs

BASE = Path(__file__).resolve().parent
if str(BASE) not in sys.path: sys.path.insert(0,str(BASE))
DATA = Path(os.environ.get('FILE_DESK_DATA', str(BASE / 'data')))
DATA.mkdir(parents=True, exist_ok=True)
DB = DATA / 'library.sqlite3'
TOKEN = secrets.token_urlsafe(32)
LOCK = threading.Lock()
TASK = dict(running=False, count=0, errors=[], message='尚未扫描', cancel=False)

class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try: return super().__exit__(*args)
        finally: self.close()

def connection():
    c = sqlite3.connect(str(DB), timeout=30, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    return c

def init():
    with connection() as c:
        c.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS files(id INTEGER PRIMARY KEY,path TEXT UNIQUE,name TEXT,ext TEXT,size INTEGER,mtime INTEGER,
        state TEXT DEFAULT 'available',category TEXT DEFAULT '',kind TEXT DEFAULT '',year TEXT DEFAULT '',project TEXT DEFAULT '',tags TEXT DEFAULT '',
        organized INTEGER DEFAULT 0,archived INTEGER DEFAULT 0,protected INTEGER DEFAULT 0,favorite INTEGER DEFAULT 0,version TEXT DEFAULT '',hash TEXT DEFAULT '',seen TEXT);
        CREATE INDEX IF NOT EXISTS files_size ON files(size);
        CREATE INDEX IF NOT EXISTS files_hash ON files(hash);
        CREATE TABLE IF NOT EXISTS roots(path TEXT PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS logs(id INTEGER PRIMARY KEY,time TEXT,action TEXT,path TEXT,result TEXT);
        PRAGMA user_version=1;
        ''')
    ARCHIVE.init()

def log(action, path, result):
    with connection() as c:
        c.execute('INSERT INTO logs(time,action,path,result) VALUES(?,?,?,?)',(time.strftime('%Y-%m-%d %H:%M:%S'),action,str(path),result))

def inside(path, root):
    try: return os.path.commonpath([str(path),str(root)]) == str(root)
    except ValueError: return False

def digest(path):
    h=hashlib.sha256()
    with open(path,'rb') as f:
        for block in iter(lambda:f.read(1024*1024), b''): h.update(block)
    return h.hexdigest()

from archival import ArchiveService
ARCHIVE = ArchiveService(connection,DATA,BASE,digest,log)

def scan(root):
    stamp=secrets.token_hex(12)
    errors=[]
    def error(e): errors.append(str(e))
    try:
        if not root.is_dir(): raise ValueError('目录无法访问，保留原有记录')
        with connection() as c:
            c.execute('INSERT OR IGNORE INTO roots VALUES(?)',(str(root),))
        for directory, dirs, names in os.walk(str(root), onerror=error, followlinks=False):
            dirs[:]=[d for d in dirs if d not in ('$RECYCLE.BIN','System Volume Information','.git','node_modules') and not Path(directory,d).is_symlink() and not inside(Path(directory,d).resolve(), DATA.resolve()) and not (getattr(os.stat(Path(directory,d),follow_symlinks=False),'st_file_attributes',0)&1024)]
            if TASK['cancel']: break
            with connection() as c:
                for name in names:
                    if TASK['cancel']: break
                    if name.startswith('.archive-'): continue
                    p=Path(directory,name)
                    if p.is_symlink() or inside(p.resolve(),DATA.resolve()): continue
                    try:
                        s=p.stat()
                        if not p.is_file(): continue
                        path=os.path.normcase(str(p.resolve()))
                        c.execute('''INSERT INTO files(path,name,ext,size,mtime,seen) VALUES(?,?,?,?,?,?)
                        ON CONFLICT(path) DO UPDATE SET name=excluded.name,ext=excluded.ext,
                        hash=CASE WHEN files.size=excluded.size AND files.mtime=excluded.mtime THEN files.hash ELSE '' END,
                        size=excluded.size,mtime=excluded.mtime,state='available',seen=excluded.seen''',
                        (path,name,p.suffix.lower(),s.st_size,s.st_mtime_ns,stamp))
                        TASK['count']+=1
                    except OSError as e: error(e)
        if not errors and not TASK['cancel']:
            with connection() as c:
                for row in c.execute('SELECT id,path,seen FROM files').fetchall():
                    if inside(row['path'],os.path.normcase(str(root))) and row['seen'] != stamp:
                        c.execute("UPDATE files SET state='missing',hash='' WHERE id=?",(row['id'],))
        TASK['errors']=errors[:100]
        TASK['message']='扫描已取消，保留未核对记录' if TASK['cancel'] else '扫描完成 · %s 个文件 · %s 项错误'%(TASK['count'],len(errors))
        log('扫描',root,TASK['message'])
    except Exception as e:
        TASK['message']=str(e); TASK['errors']=[str(e)]
    finally: TASK['running']=False

def duplicates():
    try:
        with connection() as c:
            rows=c.execute("SELECT * FROM files WHERE state='available' AND size IN (SELECT size FROM files WHERE state='available' GROUP BY size HAVING COUNT(*)>1)").fetchall()
        for r in rows:
            if TASK['cancel']: break
            try:
                p=Path(r['path']); before=p.stat(); h=digest(p); after=p.stat()
                if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns) or (after.st_size,after.st_mtime_ns)!=(r['size'],r['mtime']):
                    raise ValueError('文件已变化，请重新扫描')
                with connection() as c: c.execute('UPDATE files SET hash=? WHERE id=?',(h,r['id']))
            except Exception as e:
                with connection() as c: c.execute("UPDATE files SET hash='' WHERE id=?",(r['id'],))
                TASK['errors'].append(str(e))
            TASK['count']+=1
        TASK['message']='重复检测已取消' if TASK['cancel'] else '重复检测完成'
    finally: TASK['running']=False

def start_task(fn,*args):
    with LOCK:
        if TASK['running']: raise ValueError('已有任务正在进行')
        TASK.update(running=True,count=0,errors=[],message='正在处理',cancel=False)
        threading.Thread(target=fn,args=args,daemon=True).start()

def recycle(path):
    if os.name!='nt' or str(path).startswith('\\\\'): raise ValueError('仅支持本机 Windows 磁盘回收')
    from windows_recycle import recycle_file
    recycle_file(path)

def mutate(action,b):
    if action.startswith('archive-'): return ARCHIVE.dispatch(action,b)
    if action=='scan':
        root=Path(b['path']).expanduser().resolve()
        if not root.is_dir(): raise ValueError('请输入可访问的文件夹完整路径')
        start_task(scan,root);return {}
    if action=='duplicates': start_task(duplicates);return {}
    if action=='cancel': TASK['cancel']=True;return {}
    if action=='backup':
        target=DATA/('backup-'+time.strftime('%Y%m%d-%H%M%S')+'.sqlite3')
        with connection() as src, sqlite3.connect(str(target),factory=ClosingConnection) as dst: src.backup(dst)
        log('备份',target,'成功');return {'message':'管理数据备份已保存：'+str(target)}
    if action=='recycle-bin': subprocess.Popen(['explorer.exe','shell:RecycleBinFolder']);return {}
    if action=='remove-root':
        with connection() as c: c.execute('DELETE FROM roots WHERE path=?',(b['path'],))
        return {}
    ids=b.get('ids',[])
    if not ids or len(ids)>500: raise ValueError('请选择 1 至 500 个文件')
    results=[]
    for id in ids:
        with connection() as c: r=c.execute('SELECT * FROM files WHERE id=?',(id,)).fetchone()
        if not r: continue
        try:
            if action=='edit':
                allowed={'category','kind','year','project','tags','organized','archived','protected','favorite','version'}
                fields={k:v for k,v in b['fields'].items() if k in allowed}
                if not fields: raise ValueError('没有需要更新的属性')
                if any(len(str(v))>500 for v in fields.values()): raise ValueError('属性内容过长')
                with connection() as c: c.execute('UPDATE files SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[id])
            elif action in ('open','folder','trash'):
                p=Path(r['path'])
                if action=='folder': subprocess.Popen(['explorer.exe','/select,',str(p)])
                else:
                    if not p.is_file(): raise ValueError('文件无法访问，请重新扫描')
                    if action=='open':
                        if p.suffix.lower() not in {'.doc','.docx','.xls','.xlsx','.ppt','.pptx','.pdf','.txt','.csv','.png','.jpg','.jpeg','.gif','.bmp','.mp4','.mp3'}: raise ValueError('此格式请通过所在文件夹打开')
                        os.startfile(str(p))
                    else:
                        if TASK['running']: raise ValueError('请等待后台任务完成后再清理')
                        if r['protected']: raise ValueError('文件受保护，请先解除保护')
                        if b.get('confirm')!='RECYCLE': raise ValueError('需要确认回收')
                        s=p.stat()
                        if (s.st_size,s.st_mtime_ns)!=(r['size'],r['mtime']): raise ValueError('文件已变化，请重新扫描')
                        if r['hash'] and digest(p)!=r['hash']: raise ValueError('内容已变化，请重新检测')
                        log('移入回收站',p,'准备执行')
                        recycle(p)
                        with connection() as c: c.execute("UPDATE files SET state='recycled',hash='' WHERE id=?",(id,))
            else: raise ValueError('未知操作')
            results.append({'id':id,'name':r['name'],'result':'成功'})
            log(action,r['path'],'成功')
        except Exception as e:
            results.append({'id':id,'name':r['name'],'result':str(e)});log(action,r['path'],str(e))
    return {'results':results}

class Handler(BaseHTTPRequestHandler):
    def send(self,data,status=200,ctype='application/json; charset=utf-8'):
        body=data if isinstance(data,bytes) else json.dumps(data,ensure_ascii=False).encode()
        self.send_response(status);self.send_header('Content-Type',ctype);self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.send_header('X-Frame-Options','DENY');self.end_headers();self.wfile.write(body)
    def do_GET(self):
        if self.headers.get('Host') not in ('127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)): self.send({'error':'无效地址'},403);return
        url=urlparse(self.path)
        if url.path=='/': self.send((BASE/'index.html').read_text('utf-8').replace('__TOKEN__',TOKEN).encode(),ctype='text/html; charset=utf-8');return
        if url.path in ('/archive.js','/archive.css'):
            self.send((BASE/url.path[1:]).read_bytes(),ctype='text/javascript; charset=utf-8' if url.path.endswith('.js') else 'text/css; charset=utf-8');return
        if self.headers.get('X-Token')!=TOKEN: self.send({'error':'请从本机首页访问'},403);return
        if url.path=='/api/archive-state':
            self.send(ARCHIVE.snapshot(parse_qs(url.query).get('batch_id',[None])[0]));return
        if url.path=='/api/archive-find':
            file_id=parse_qs(url.query).get('file_id',['0'])[0]
            with connection() as c:
                item=c.execute("SELECT * FROM archive_items WHERE file_id=? AND status='pending' ORDER BY rowid DESC LIMIT 1",(file_id,)).fetchone()
                file=c.execute('SELECT * FROM files WHERE id=?',(file_id,)).fetchone()
            if item:
                result=ARCHIVE.public_item(dict(item))
                if file: result['fields'].update({k:file[k] for k in ('year','category','kind','project','tags')})
                self.send(result)
            else: self.send({'error':'未找到待确认归档任务'},404)
            return
        with connection() as c:
            if url.path=='/api/state':
                stats=dict(c.execute("SELECT COUNT(*) total,COALESCE(SUM(size),0) bytes,COALESCE(SUM(organized=0),0) pending,COALESCE(SUM(archived),0) archived FROM files WHERE state='available'").fetchone())
                stats['groups']=c.execute("SELECT COUNT(*) FROM (SELECT hash FROM files WHERE state='available' AND hash!='' GROUP BY hash HAVING COUNT(*)>1)").fetchone()[0]
                self.send(dict(stats=stats,task=TASK,roots=[r[0] for r in c.execute('SELECT path FROM roots')],logs=[dict(r) for r in c.execute('SELECT * FROM logs ORDER BY id DESC LIMIT 30')],categories=[r[0] for r in c.execute("SELECT DISTINCT category FROM files WHERE category!=''")],years=[r[0] for r in c.execute("SELECT DISTINCT year FROM files WHERE year!='' ORDER BY year DESC")]))
            elif url.path=='/api/files':
                q={k:v[0] for k,v in parse_qs(url.query).items()};where=['1=1'];args=[]
                if q.get('mode')!='missing': where.append("state='available'")
                else: where.append("state='missing'")
                if q.get('q'): where.append("(name LIKE ? ESCAPE '\\' OR tags LIKE ? ESCAPE '\\' OR project LIKE ? ESCAPE '\\')");args.extend(['%'+q['q'].replace('\\','\\\\').replace('%','\\%').replace('_','\\_')+'%']*3)
                for key in ('category','year','ext'):
                    if q.get(key): where.append(key+'=?');args.append(q[key])
                mode=q.get('mode','')
                if mode=='pending': where.append('organized=0')
                if mode=='archived': where.append('archived=1')
                if mode=='archive-pending': where.append("archive_state='pending'")
                if mode=='archive-copies': where.append("archive_state='ready'")
                if mode=='favorite': where.append('favorite=1')
                if mode=='old': where.append('mtime<?');args.append(int((time.time()-180*86400)*1e9))
                if mode=='duplicates': where.append("hash!='' AND hash IN (SELECT hash FROM files WHERE state='available' AND hash!='' GROUP BY hash HAVING COUNT(*)>1)")
                clause=' AND '.join(where); total=c.execute('SELECT COUNT(*) FROM files WHERE '+clause,args).fetchone()[0]
                page=max(0,int(q.get('page',0)));order='size DESC' if mode=='large' else 'hash,name' if mode=='duplicates' else 'mtime DESC'
                rows=c.execute('SELECT * FROM files WHERE '+clause+' ORDER BY '+order+' LIMIT 100 OFFSET ?',args+[page*100]).fetchall()
                self.send({'total':total,'rows':[dict(r) for r in rows]})
            else: self.send({'error':'不存在'},404)
    def do_POST(self):
        if self.headers.get('X-Token')!=TOKEN or self.headers.get('Host') not in ('127.0.0.1:'+str(self.server.server_port),'localhost:'+str(self.server.server_port)):
            self.send({'error':'访问被拒绝'},403);return
        try:
            size=int(self.headers.get('Content-Length',0))
            url=urlparse(self.path)
            if url.path=='/api/archive-chunk':
                if size<0 or size>4*1024*1024: raise ValueError('文件分块超过限制')
                q=parse_qs(url.query);body=self.rfile.read(size)
                if len(body)!=size: raise ValueError('接收中断')
                self.send(ARCHIVE.chunk(q['id'][0],int(q['offset'][0]),body));return
            if size>1000000: raise ValueError('请求过大')
            b=json.loads(self.rfile.read(size)); self.send(mutate(self.path.rsplit('/',1)[-1],b))
        except Exception as e: self.send({'error':str(e)},400)
    def log_message(self,*args): pass

if __name__=='__main__':
    init(); port=int(os.environ.get('FILE_DESK_PORT','8765'))
    try: server=ThreadingHTTPServer(('127.0.0.1',port),Handler)
    except OSError:
        print('端口已占用。请检查应用是否已经启动。');sys.exit(1)
    url='http://127.0.0.1:%d'%port
    print('File Desk '+url,flush=True)
    if '--no-browser' not in sys.argv: webbrowser.open(url)
    server.serve_forever()
