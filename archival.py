"""Local drag-and-drop archive ingestion, journaled independently of the scanner."""
import hashlib
import json
import os
import re
import secrets
import shutil
import threading
import time
from pathlib import Path

CHUNK = 4 * 1024 * 1024
MAX_FILE = 2 * 1024 * 1024 * 1024
BUSINESSES = ['党员教育', '党员发展', '组织建设', '综合事务']
KINDS = ['会议纪要', '签到表', '通知', '方案', '总结', '统计表', '照片']
LABELS = {'receiving':'接收中', 'staged':'校验中', 'placing':'保存中', 'ready':'已归档',
          'existing':'已存在', 'pending':'待确认', 'conflict':'其他位置已存在',
          'linked':'已关联现有档案', 'failed':'失败', 'cancelled':'已取消'}


def normalized(p):
    return os.path.normcase(str(Path(p).resolve()))


def safe_name(value):
    value = str(value).strip()
    if not value or value in ('.','..') or len(value)>180 or value[-1:] in ('.',' '):
        raise ValueError('名称为空、过长或结尾无效')
    if re.search(r'[<>:"/\\|?*\x00-\x1f]', value):
        raise ValueError('名称不能包含路径分隔符或特殊字符')
    if value.split('.')[0].upper() in {'CON','PRN','AUX','NUL', *('COM'+str(n) for n in range(1,10)), *('LPT'+str(n) for n in range(1,10))}:
        raise ValueError('名称为 Windows 保留名称')
    return value


def attributes(name, presets):
    fields = {}
    source = {}
    for k in ('year','category','kind','project','tags'):
        value = str(presets.get(k,'')).strip()
        if len(value)>500: raise ValueError('归档信息过长')
        if value:
            fields[k]=value; source[k]='本次指定'
    if fields.get('year') and not re.fullmatch(r'(19|20)\d{2}',fields['year']):
        raise ValueError('业务年度请填写四位年份，例如 2026')
    if not fields.get('year'):
        years=sorted(set(re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)',name)))
        if len(years)==1: fields['year']=years[0];source['year']='文件名年份'
    if not fields.get('category'):
        matches=[b for b in BUSINESSES if b in name]
        if len(matches)==1: fields['category']=matches[0];source['category']='文件名关键词'
    if not fields.get('kind'):
        matches=[k for k in KINDS if k in name]
        if len(matches)==1: fields['kind']=matches[0];source['kind']='文件名关键词'
        elif not matches and Path(name).suffix.lower() in {'.jpg','.jpeg','.png','.heic','.bmp'}:
            fields['kind']='照片';source['kind']='图片格式'
        else: fields['kind']='其他材料';source['kind']='未唯一识别材料类型'
    for k in ('year','category','kind'):
        if fields.get(k): safe_name(fields[k])
    missing=[label for k,label in [('year','年度'),('category','业务分类')] if not fields.get(k)]
    return fields, source, '请确认'+ '、'.join(missing) if missing else ''


class ArchiveService:
    def __init__(self, connection, data, base, digest, log):
        self.connection=connection; self.data=Path(data); self.base=Path(base)
        self.digest=digest;self.log=log;self.lock=threading.RLock()
        self.stage=self.data/'archive_staging';self.stage.mkdir(parents=True,exist_ok=True)

    def init(self):
        with self.connection() as c:
            columns={r[1] for r in c.execute('PRAGMA table_info(files)')}
            for column in ('archive_state','archive_root','archive_batch','original_name'):
                if column not in columns: c.execute('ALTER TABLE files ADD COLUMN '+column+" TEXT DEFAULT ''")
            c.executescript('''
            CREATE TABLE IF NOT EXISTS archive_settings(key TEXT PRIMARY KEY,value TEXT);
            CREATE TABLE IF NOT EXISTS archive_batches(id TEXT PRIMARY KEY,created_at TEXT,root TEXT);
            CREATE TABLE IF NOT EXISTS archive_items(id TEXT PRIMARY KEY,batch_id TEXT,name TEXT,size INTEGER,
              received INTEGER DEFAULT 0,last_modified INTEGER,root TEXT,status TEXT,metadata TEXT,hash TEXT DEFAULT '',
              path TEXT DEFAULT '',temp_path TEXT DEFAULT '',file_id INTEGER,error TEXT DEFAULT '',created_at TEXT);
            CREATE INDEX IF NOT EXISTS archive_items_batch ON archive_items(batch_id);
            PRAGMA user_version=2;
            ''')
        # Journal recovery only touches paths previously assigned by this service.
        with self.connection() as c:
            items=[dict(r) for r in c.execute("SELECT * FROM archive_items WHERE status IN ('receiving','staged','placing') OR (status='failed' AND path!='' AND hash!='')")]
        for item in items:
            try:
                if item['status']=='receiving':
                    self.update(item['id'],status='failed',error='接收中断，请重新选择原文件重试')
                else: self.finish(item['id'])
            except Exception as e: self.update(item['id'],status='failed',error=str(e))

    def update(self,item_id,**fields):
        with self.connection() as c:
            c.execute('UPDATE archive_items SET '+','.join(k+'=?' for k in fields)+' WHERE id=?',list(fields.values())+[item_id])

    def item(self,item_id):
        with self.connection() as c: r=c.execute('SELECT * FROM archive_items WHERE id=?',(item_id,)).fetchone()
        if not r: raise ValueError('归档任务不存在')
        return dict(r)

    def root(self):
        with self.connection() as c: r=c.execute("SELECT value FROM archive_settings WHERE key='root'").fetchone()
        return r[0] if r else ''

    def set_root(self,path):
        with self.lock:
            root=Path(path).expanduser()
            if not root.is_absolute(): raise ValueError('请输入档案目录的完整路径')
            root=root.resolve()
            if str(root).startswith('\\\\'): raise ValueError('首版请选择本机磁盘目录')
            if root==Path(root.anchor) or root==self.base.resolve() or root==self.data.resolve() or self.data.resolve() in root.parents:
                raise ValueError('请使用独立的档案文件夹，不要选择磁盘根目录或应用数据目录')
            root.mkdir(parents=True,exist_ok=True)
            probe=root/('.archive-probe-'+secrets.token_hex(6))
            with probe.open('xb') as f: f.write(b'')
            probe.unlink()
            with self.connection() as c:
                c.execute("INSERT INTO archive_settings VALUES('root',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",(str(root),))
            self.log('设置档案目录',root,'成功')
            return {'root':str(root)}

    def begin(self,manifest,presets):
        with self.lock:
            root=self.root()
            if not root or not Path(root).is_dir(): raise ValueError('请先设置可访问的档案目录')
            if not manifest or len(manifest)>200: raise ValueError('每批请选择 1 至 200 个文件')
            prepared=[]
            for m in manifest:
                name=safe_name(m['name']);size=int(m['size']);modified=int(m.get('last_modified',0))
                if size<0 or size>MAX_FILE: raise ValueError(name+'：单个文件上限为 2 GB')
                fields,sources,reason=attributes(name,presets)
                prepared.append((secrets.token_hex(16),name,size,modified,json.dumps({'fields':fields,'sources':sources,'reason':reason},ensure_ascii=False)))
            batch=secrets.token_hex(12);created=time.strftime('%Y-%m-%d %H:%M:%S')
            with self.connection() as c:
                c.execute('INSERT INTO archive_batches VALUES(?,?,?)',(batch,created,root))
                for item_id,name,size,modified,metadata in prepared:
                    c.execute('INSERT INTO archive_items(id,batch_id,name,size,last_modified,root,status,metadata,created_at) VALUES(?,?,?,?,?,?,?,?,?)',
                              (item_id,batch,name,size,modified,root,'receiving',metadata,created))
            return {'batch_id':batch,'items':[{'id':p[0],'name':p[1],'size':p[2]} for p in prepared]}

    def chunk(self,item_id,offset,body):
        with self.lock:
            item=self.item(item_id)
            if item['status']!='receiving': raise ValueError('此文件不处于接收状态')
            if offset!=item['received']: raise ValueError('接收位置不一致，请重试文件')
            if len(body)>CHUNK or offset+len(body)>item['size']: raise ValueError('接收数据超过文件声明大小')
            path=self.stage/(item_id+'.part')
            with path.open('r+b' if path.exists() else 'w+b') as f:
                f.seek(offset);f.truncate();f.write(body);f.flush();os.fsync(f.fileno())
            self.update(item_id,received=offset+len(body))
            return {'received':offset+len(body)}

    def find_duplicates(self,item,h,fields):
        with self.connection() as c:
            candidates=[dict(r) for r in c.execute("SELECT * FROM files WHERE state='available' AND archive_state IN ('ready','pending') AND archive_root=? AND size=? AND hash=?",(item['root'],item['size'],h))]
        same=[];other=[]
        for r in candidates:
            p=Path(r['path'])
            try:
                s=p.stat()
                if s.st_size!=item['size'] or s.st_mtime_ns!=r['mtime'] or self.digest(p)!=h: continue
            except OSError: continue
            # Project is a business context even though it is virtual metadata.
            if all((r[k] or '')==fields.get(k,'') for k in ('year','category','kind','project')): same.append(r)
            elif r['archive_state']=='ready': other.append(r)
        return same,other

    def destination(self,item,fields,pending):
        root=Path(item['root']).resolve()
        parts=['待确认',item['batch_id']] if pending else [fields['year'],fields['category'],fields['kind']]
        directory=root
        for part in parts:
            directory=directory/safe_name(part)
            directory.mkdir(exist_ok=True)
            if not directory.resolve().is_relative_to(root): raise ValueError('归档路径超出档案目录')
        name=safe_name(item['name']);p=directory/name;n=2
        while p.exists():
            p=directory/(Path(name).stem+'（'+str(n)+'）'+Path(name).suffix);n+=1
        return p

    def register(self,item,path,fields,pending):
        s=path.stat();key=normalized(path);status='pending' if pending else 'ready'
        with self.connection() as c:
            c.execute('''INSERT INTO files(path,name,ext,size,mtime,category,kind,year,project,tags,organized,archived,hash,archive_state,archive_root,archive_batch,original_name)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET
            size=excluded.size,mtime=excluded.mtime,category=excluded.category,kind=excluded.kind,year=excluded.year,
            project=excluded.project,tags=excluded.tags,organized=excluded.organized,archived=excluded.archived,
            hash=excluded.hash,state='available',archive_state=excluded.archive_state,archive_root=excluded.archive_root,
            archive_batch=excluded.archive_batch,original_name=excluded.original_name''',
            (key,path.name,path.suffix.lower(),s.st_size,s.st_mtime_ns,fields.get('category',''),fields.get('kind',''),fields.get('year',''),
             fields.get('project',''),fields.get('tags',''),int(not pending),int(not pending),item['hash'],status,item['root'],item['batch_id'],item['name']))
            fid=c.execute('SELECT id FROM files WHERE path=?',(key,)).fetchone()[0]
            c.execute('UPDATE archive_items SET status=?,file_id=?,error=?,temp_path=? WHERE id=?',
                      (status,fid,json.loads(item['metadata']).get('reason','') if pending else '', '',item['id']))
        self.log('拖入归档',path,'待确认' if pending else '成功')
        return fid

    def place(self,item,source,fields,pending):
        # Write the selected path to the journal before touching the destination.
        if item['status']=='placing' and item['path']:
            dest=Path(item['path']);tmp=Path(item['temp_path']) if item['temp_path'] else None
        else:
            dest=self.destination(item,fields,pending);tmp=dest.parent/('.archive-'+item['id']+'.tmp')
            self.update(item['id'],status='placing',path=str(dest),temp_path=str(tmp))
        if dest.exists():
            if self.digest(dest)!=item['hash']: raise ValueError('目标文件冲突，已保留接收副本，请重试')
        else:
            if not source.is_file(): raise ValueError('接收副本丢失，请重新选择原文件')
            with source.open('rb') as src,tmp.open('wb') as dst:
                shutil.copyfileobj(src,dst,CHUNK);dst.flush();os.fsync(dst.fileno())
            if self.digest(tmp)!=item['hash']: raise ValueError('保存校验失败，可重试')
            if item['last_modified']>0:
                os.utime(tmp,ns=(time.time_ns(),item['last_modified']*1000000))
            # Windows rename is exclusive; hard link gives no-overwrite publication on POSIX.
            if os.name=='nt': os.rename(tmp,dest)
            else: os.link(tmp,dest);tmp.unlink()
        fid=self.register(self.item(item['id']),dest,fields,pending)
        self.cleanup_pending(self.item(item['id']))
        if tmp and tmp.exists(): tmp.unlink()
        # Staging files belong to this service, never to the user's source directory.
        if source.parent==self.stage and source.exists(): source.unlink()
        return fid

    def cleanup_pending(self,item):
        previous=json.loads(item['metadata']).get('previous_pending')
        if not previous: return
        with self.connection() as c: row=c.execute('SELECT * FROM files WHERE id=?',(previous['id'],)).fetchone()
        if not row: return
        source=Path(previous['path'])
        if row['archive_state']!='pending' or row['protected'] or not source.resolve().is_relative_to((Path(item['root'])/'待确认').resolve()):
            self.log('完成归档',source,'旧待确认副本已保留，请人工核对');return
        if source.exists():
            if self.digest(source)!=item['hash']:
                self.log('完成归档',source,'旧待确认副本发生变化，已保留');return
            source.unlink()
        with self.connection() as c:
            c.execute('DELETE FROM files WHERE id=?',(previous['id'],))
            c.execute('UPDATE archive_items SET file_id=?,path=?,error=? WHERE file_id=? AND id!=?',
                      (item['file_id'],item['path'],'',previous['id'],item['id']))

    def finish(self,item_id,allow_other=False):
        with self.lock:
            item=self.item(item_id)
            if item['status'] in ('ready','existing','pending','linked'): return self.public_item(item)
            if item['status']=='cancelled': raise ValueError('任务已取消')
            if item['status']=='failed':
                if item['path'] and item['hash']:
                    self.update(item_id,status='placing');item=self.item(item_id)
                elif item['hash'] and item['received']==item['size']:
                    self.update(item_id,status='staged');item=self.item(item_id)
                else: raise ValueError('接收未完成，请重新选择原文件重试')
            source=self.stage/(item_id+'.part')
            if item['status']=='receiving':
                if item['received']!=item['size']: raise ValueError('文件接收未完成')
                if item['size']==0 and not source.exists(): source.touch()
                if source.stat().st_size!=item['size']: raise ValueError('文件大小校验失败')
                self.update(item_id,status='staged',hash=self.digest(source),error='')
                item=self.item(item_id)
            meta=json.loads(item['metadata']);fields=meta['fields'];pending=not fields.get('year') or not fields.get('category')
            if item['status']!='placing':
                same,other=self.find_duplicates(item,item['hash'],fields)
                if same:
                    self.update(item_id,status='existing',file_id=same[0]['id'],path=same[0]['path'],error='已有待确认副本，请补全原归档记录' if same[0]['archive_state']=='pending' else '')
                    if source.exists(): source.unlink()
                    self.cleanup_pending(self.item(item_id))
                    self.log('拖入归档',same[0]['path'],'已有相同内容，复用档案')
                    return self.public_item(self.item(item_id))
                if other and not allow_other and not pending:
                    meta['candidates']=[{'id':r['id'],'name':r['name'],'path':r['path'],'category':r['category'],'year':r['year'],'project':r['project']} for r in other]
                    self.update(item_id,status='conflict',metadata=json.dumps(meta,ensure_ascii=False),error='相同内容已在其他业务或活动中，请选择关联或另存')
                    return self.public_item(self.item(item_id))
            self.place(item,source,fields,pending)
            return self.public_item(self.item(item_id))

    def complete(self,item_id):
        try: return self.finish(item_id)
        except Exception as e:
            self.update(item_id,status='failed',error=str(e));raise

    def resolve(self,item_id,fields,decision='copy',link_id=None):
        with self.lock:
            item=self.item(item_id)
            if item['status'] not in ('pending','conflict'): raise ValueError('此任务无需确认')
            if decision=='link':
                if item['status']!='conflict': raise ValueError('此任务不支持关联')
                with self.connection() as c: r=c.execute('SELECT * FROM files WHERE id=?',(link_id,)).fetchone()
                if not r or r['archive_root']!=item['root'] or r['archive_state']!='ready' or self.digest(r['path'])!=item['hash']:
                    raise ValueError('已有档案发生变化，请另存')
                self.update(item_id,status='linked',file_id=r['id'],path=r['path'],error='')
                part=self.stage/(item_id+'.part')
                if part.exists(): part.unlink()
                self.log('关联档案',r['path'],'保留已有分类与活动信息')
                return self.public_item(self.item(item_id))
            meta=json.loads(item['metadata']);presets=dict(meta['fields']);presets.update(fields)
            f,s,reason=attributes(item['name'],presets)
            if reason: raise ValueError(reason)
            meta.update(fields=f,sources=s,reason='')
            if item['status']=='pending':
                source=Path(item['path'])
                with self.connection() as c: original=c.execute('SELECT * FROM files WHERE id=?',(item['file_id'],)).fetchone()
                if not original or original['protected']: raise ValueError('文件受保护或记录不存在')
                if not source.is_file() or self.digest(source)!=item['hash']: raise ValueError('待确认文件已变化，请重新导入')
                part=self.stage/(item_id+'.part');shutil.copyfile(source,part)
                meta['previous_pending']={'id':original['id'],'path':str(source)}
                self.update(item_id,status='staged',path='',temp_path='',metadata=json.dumps(meta,ensure_ascii=False),error='')
                result=self.finish(item_id,True)
                return result
            self.update(item_id,status='staged',metadata=json.dumps(meta,ensure_ascii=False),error='')
            return self.finish(item_id,True)

    def cancel(self,batch_id):
        with self.lock:
            with self.connection() as c: items=[dict(r) for r in c.execute("SELECT * FROM archive_items WHERE batch_id=? AND (status='receiving' OR (status='failed' AND hash=''))",(batch_id,))]
            for r in items:
                self.update(r['id'],status='cancelled',error='未完成接收的文件已取消')
                part=self.stage/(r['id']+'.part')
                if part.exists(): part.unlink()
            return self.snapshot(batch_id)

    def retry(self,item_id,has_file=True):
        with self.lock:
            r=self.item(item_id)
            if r['status'] not in ('failed','cancelled'): raise ValueError('只可重试失败或取消的文件')
            if r['status']=='failed' and r['hash'] and r['received']==r['size']:
                return {'completed':True,'item':self.finish(item_id)}
            if not has_file: return {'needs_upload':True}
            self.update(item_id,status='receiving',received=0,error='',hash='',path='',temp_path='')
            part=self.stage/(item_id+'.part')
            if part.exists(): part.unlink()
            return {'id':item_id}

    def public_item(self,item):
        result={k:item[k] for k in ('id','batch_id','name','size','received','status','path','file_id','error','created_at')}
        result['label']=LABELS.get(item['status'],item['status']);result.update(json.loads(item['metadata']))
        if item['status']=='existing' and item['file_id']:
            with self.connection() as c: row=c.execute('SELECT archive_state FROM files WHERE id=?',(item['file_id'],)).fetchone()
            result['pending_file']=bool(row and row['archive_state']=='pending')
        return result

    def snapshot(self,batch_id=None):
        with self.connection() as c:
            if not batch_id:
                r=c.execute('SELECT id FROM archive_batches ORDER BY rowid DESC LIMIT 1').fetchone()
                batch_id=r[0] if r else ''
            if batch_id=='pending':
                items=[self.public_item(dict(r)) for r in c.execute("SELECT * FROM archive_items WHERE status IN ('pending','conflict') ORDER BY rowid DESC")]
            else:
                items=[self.public_item(dict(r)) for r in c.execute('SELECT * FROM archive_items WHERE batch_id=? ORDER BY rowid',(batch_id,))]
            batches=[dict(r) for r in c.execute('SELECT * FROM archive_batches ORDER BY rowid DESC LIMIT 12')]
        counts={k:sum(i['status']==k for i in items) for k in LABELS}
        return {'root':self.root(),'batch_id':batch_id,'items':items,'counts':counts,'batches':batches,'max_file':MAX_FILE,'chunk_size':CHUNK}

    def dispatch(self,action,b):
        if action=='archive-root': return self.set_root(b['path'])
        if action=='archive-begin': return self.begin(b['files'],b.get('presets',{}))
        if action=='archive-complete': return self.complete(b['id'])
        if action=='archive-resolve': return self.resolve(b['id'],b.get('fields',{}),b.get('decision','copy'),b.get('link_id'))
        if action=='archive-cancel': return self.cancel(b['batch_id'])
        if action=='archive-retry': return self.retry(b['id'],b.get('has_file',True))
        if action=='archive-fail':
            with self.lock:
                item=self.item(b['id'])
                if item['status'] in ('receiving','staged','placing','failed'):
                    self.update(b['id'],status='failed',error=str(b.get('error','接收失败'))[:500])
                return self.public_item(self.item(b['id']))
        raise ValueError('未知归档操作')
