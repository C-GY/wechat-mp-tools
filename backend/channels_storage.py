"""Incremental local works store, verified legacy migration and snapshot exports."""
from collections.abc import Mapping
from contextlib import contextmanager
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile
import threading
import time


FEEDS_LOCK = threading.RLock()


def locked_feeds(operation):
    @wraps(operation)
    def call(*args, **kwargs):
        with FEEDS_LOCK:
            return operation(*args, **kwargs)
    return call


def read_feeds(path):
    store = feed_store(path)
    store.ensure_ready()
    return FeedView(store)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as output:
            temporary = Path(output.name)
            json.dump(data, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _json(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


class _JsonStream:
    """Decode one legacy record at a time, including BOM and chunk boundaries."""
    def __init__(self, stream):
        self.stream, self.buffer, self.eof = stream, '', False
        self.decoder = json.JSONDecoder()

    def fill(self):
        chunk = self.stream.read(65536)
        self.eof = not chunk
        self.buffer += chunk

    def peek(self):
        self.buffer = self.buffer.lstrip()
        while not self.buffer and not self.eof:
            self.fill()
            self.buffer = self.buffer.lstrip()
        return self.buffer[:1]

    def take(self, token):
        if self.peek() != token:
            raise ValueError('旧作品文件格式不完整，原文件已保留')
        self.buffer = self.buffer[1:]

    def value(self):
        self.peek()
        while True:
            try:
                value, end = self.decoder.raw_decode(self.buffer)
                # A scalar number can straddle a chunk boundary.
                if end == len(self.buffer) and not self.eof:
                    self.fill()
                    continue
                self.buffer = self.buffer[end:]
                return value
            except json.JSONDecodeError:
                if self.eof:
                    raise
                self.fill()


class FeedView(Mapping):
    """Read-only, author-lazy compatibility boundary for existing consumers."""
    def __init__(self, store):
        self.store = store

    def __iter__(self):
        return iter(self.store.authors())

    def __len__(self):
        return len(self.store.authors())

    def __getitem__(self, key):
        result = self.store.author(key)
        if result is None:
            raise KeyError(key)
        return result


class FeedStore:
    def __init__(self, source, database):
        self.source, self.path = Path(source), Path(database)
        self._lock = threading.RLock()
        self._ready = False
        self._worker = None
        self._maintenance = {'status':'idle'}
        self._progress = {'status':'pending', 'records':0, 'authors':0, 'issues':0, 'message':'等待准备本地作品库'}

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            db.execute('PRAGMA synchronous=FULL')
            yield db
        finally:
            db.close()

    def status(self):
        result = dict(self._progress)
        result.update(database_path=str(self.path), legacy_path=str(self.source))
        result['maintenance'] = dict(self._maintenance)
        return result

    def prepare_background(self):
        # Do not acquire the migration lock: status/prepare remain responsive.
        with _stores_lock:
            if self._ready or (self._worker and self._worker.is_alive()):
                return self.status()
            def prepare():
                try:
                    self.ensure_ready()
                except Exception:
                    pass  # ensure_ready preserves the reason in status.
            self._worker = threading.Thread(target=prepare, daemon=True, name='ChannelsStorageMigration')
            self._worker.start()
        return self.status()

    def ensure_ready(self):
        if self._ready:
            return
        with self._lock:
            if self._ready:
                return
            self._progress.update(status='migrating', message='正在准备本地作品库', records=0, authors=0, issues=0)
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                with self.connection() as db:
                    db.executescript('''
                        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY,value TEXT NOT NULL);
                        CREATE TABLE IF NOT EXISTS authors (author TEXT PRIMARY KEY);
                        CREATE TABLE IF NOT EXISTS feeds (
                            author TEXT NOT NULL, record_key TEXT NOT NULL, feed_id TEXT,
                            position INTEGER NOT NULL, payload TEXT NOT NULL,
                            PRIMARY KEY(author,record_key));
                        CREATE INDEX IF NOT EXISTS feeds_order ON feeds(author,position);
                        CREATE TABLE IF NOT EXISTS migration_checks (author TEXT PRIMARY KEY, count INTEGER, digest TEXT);
                        CREATE TABLE IF NOT EXISTS migration_issues (author TEXT, position INTEGER, reason TEXT);
                        CREATE TABLE IF NOT EXISTS pages (task TEXT,author TEXT,page TEXT,digest TEXT,receipt TEXT,
                            PRIMARY KEY(task,author,page));
                        CREATE TABLE IF NOT EXISTS captured (task TEXT,author TEXT,video TEXT,PRIMARY KEY(task,author,video));
                        CREATE TABLE IF NOT EXISTS checkpoints (task TEXT,author TEXT,page TEXT,payload TEXT,
                            PRIMARY KEY(task,author));
                    ''')
                    # The ready marker and the entire migration commit together.
                    # Another process waits for this transaction and rechecks it.
                    with db:
                        db.execute('BEGIN IMMEDIATE')
                        ready = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                        if ready and ready[0] != '1':
                            raise ValueError('本地作品库版本不兼容，请使用匹配的软件版本')
                        if not ready:
                            self._migrate(db)
                            db.execute("INSERT INTO meta VALUES ('schema_version','1')")
                        self._progress['issues'] = db.execute('SELECT COUNT(*) FROM migration_issues').fetchone()[0]
                    db.execute('PRAGMA journal_mode=WAL')
                self._ready = True
                self._progress.update(status='ready', message='本地作品库已就绪')
            except Exception as exc:
                self._progress.update(status='failed', message=str(exc))
                raise

    def _migrate(self, db):
        if not self.source.exists():
            return
        before = self.source.stat()
        if shutil.disk_usage(self.path.parent).free < before.st_size * 3 + 64 * 1024 * 1024:
            raise OSError('磁盘剩余空间不足，无法安全迁移作品库；旧文件未修改')
        self._progress['message'] = '正在迁移历史作品，完成校验后自动继续'
        with self.source.open(encoding='utf-8-sig') as stream:
            reader = _JsonStream(stream)
            reader.take('{')
            if reader.peek() != '}':
                while True:
                    author = reader.value()
                    if not isinstance(author, str):
                        raise ValueError('旧作品文件的作者 ID 必须是文本')
                    try:
                        db.execute('INSERT INTO authors VALUES (?)', (author,))
                    except sqlite3.IntegrityError as exc:
                        raise ValueError('旧作品文件包含重复作者键，迁移已停止并保留原文件') from exc
                    reader.take(':'); reader.take('[')
                    count, digest = 0, hashlib.sha256()
                    if reader.peek() != ']':
                        while True:
                            item = reader.value()
                            payload = _json(item)
                            video = str(item.get('id') or '') if isinstance(item, dict) else ''
                            key = 'id:' + video if video else 'legacy:' + str(count)
                            duplicate = video and db.execute('SELECT 1 FROM feeds WHERE author=? AND record_key=?', (author,key)).fetchone()
                            if not video or duplicate:
                                key = 'legacy:' + str(count)
                                db.execute('INSERT INTO migration_issues VALUES (?,?,?)', (author,count,'duplicate_id' if duplicate else 'missing_id'))
                            db.execute('INSERT INTO feeds VALUES (?,?,?,?,?)', (author,key,video,count,payload))
                            digest.update(payload.encode('utf-8') + b'\n')
                            count += 1
                            self._progress['records'] += 1
                            if count % 128 == 0:
                                time.sleep(0)
                            if reader.peek() == ']':
                                break
                            reader.take(',')
                    reader.take(']')
                    db.execute('INSERT INTO migration_checks VALUES (?,?,?)', (author,count,digest.hexdigest()))
                    self._progress['authors'] += 1
                    if reader.peek() == '}':
                        break
                    reader.take(',')
            reader.take('}')
            if reader.peek():
                raise ValueError('旧作品文件末尾存在额外内容，迁移已停止')
        after = self.source.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise ValueError('迁移期间旧作品文件发生变化，请关闭旧版软件后重试')
        self._progress['message'] = '正在逐作者校验历史作品'
        for author, expected_count, expected_digest in db.execute('SELECT * FROM migration_checks').fetchall():
            digest, count = hashlib.sha256(), 0
            for payload, in db.execute('SELECT payload FROM feeds WHERE author=? ORDER BY position', (author,)):
                digest.update(payload.encode('utf-8') + b'\n')
                count += 1
            if count != expected_count or digest.hexdigest() != expected_digest:
                raise ValueError('迁移校验失败，旧文件已保留')
        db.execute('INSERT INTO meta VALUES (?,?)', ('migration_source',_json({'path':str(self.source), 'bytes':before.st_size,'mtime_ns':before.st_mtime_ns})))

    def authors(self):
        self.ensure_ready()
        with self.connection() as db:
            return [r[0] for r in db.execute('SELECT author FROM authors ORDER BY rowid')]

    def author(self, author):
        self.ensure_ready()
        with self.connection() as db:
            if not db.execute('SELECT 1 FROM authors WHERE author=?',(author,)).fetchone():
                return None
            return [json.loads(r[0]) for r in db.execute('SELECT payload FROM feeds WHERE author=? ORDER BY position',(author,))]

    def _merge(self, db, author, items):
        db.execute('INSERT OR IGNORE INTO authors VALUES (?)',(author,))
        position = db.execute('SELECT COALESCE(MAX(position),-1)+1 FROM feeds WHERE author=?',(author,)).fetchone()[0]
        for item in items:
            video = str(item.get('id') or '')
            if not video:
                raise ValueError('作品缺少有效 ID，保存已回滚')
            key = 'id:' + video
            previous = db.execute('SELECT payload FROM feeds WHERE author=? AND record_key=?',(author,key)).fetchone()
            merged = {**(json.loads(previous[0]) if previous else {}), **item}
            db.execute('INSERT INTO feeds VALUES (?,?,?,?,?) ON CONFLICT(author,record_key) DO UPDATE SET payload=excluded.payload',
                       (author,key,video,position,_json(merged)))
            if not previous:
                position += 1

    def merge(self, author, items, *, alias=None, page=None):
        self.ensure_ready()
        with self.connection() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if page:
                old = db.execute('SELECT digest,receipt FROM pages WHERE task=? AND author=? AND page=?',
                                 (page['task_id'],author,page['page_id'])).fetchone()
                if old:
                    if old[0] != page['digest']:
                        raise ValueError('同一采集页的内容不一致，请重新采集')
                    return json.loads(old[1])
            if alias and alias != author:
                old_rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM feeds WHERE author=? ORDER BY position',(alias,))]
                for row in old_rows:
                    if isinstance(row,dict) and row.get('id') and not db.execute('SELECT 1 FROM feeds WHERE author=? AND record_key=?',(author,'id:'+str(row['id']))).fetchone():
                        self._merge(db,author,[row])
                # Preserve anomalous legacy rows under the alias rather than dropping them.
                if (old_rows and all(isinstance(r,dict) and r.get('id') for r in old_rows)
                        and len({str(r['id']) for r in old_rows}) == len(old_rows)):
                    db.execute('DELETE FROM feeds WHERE author=?',(alias,))
                    db.execute('DELETE FROM authors WHERE author=?',(alias,))
            self._merge(db,author,items)
            if page:
                saved = sorted({str(item['id']) for item in items})
                receipt = {'saved_ids':saved, 'capture_task_id':page['task_id'], 'page_id':page['page_id']}
                db.execute('INSERT INTO pages VALUES (?,?,?,?,?)',(page['task_id'],author,page['page_id'],page['digest'],_json(receipt)))
                db.executemany('INSERT OR IGNORE INTO captured VALUES (?,?,?)',[(page['task_id'],author,video) for video in saved])
                return receipt

    def remove(self, *authors):
        self.ensure_ready()
        with self.connection() as db, db:
            for author in authors:
                db.execute('DELETE FROM feeds WHERE author=?',(author,))
                db.execute('DELETE FROM authors WHERE author=?',(author,))

    def patch_existing(self, author, video, patch):
        self.ensure_ready()
        with self.connection() as db, db:
            db.execute('BEGIN IMMEDIATE')
            old = db.execute('SELECT payload FROM feeds WHERE author=? AND record_key=?',(author,'id:'+str(video))).fetchone()
            if old:
                db.execute('UPDATE feeds SET payload=? WHERE author=? AND record_key=?',
                           (_json({**json.loads(old[0]),**patch}),author,'id:'+str(video)))

    def receipt(self, task, author, page):
        self.ensure_ready()
        with self.connection() as db:
            row = db.execute('SELECT receipt FROM pages WHERE task=? AND author=? AND page=?',(task,author,page)).fetchone()
            return json.loads(row[0]) if row else None

    def checkpoint(self, task, author, page, value):
        self.ensure_ready()
        with self.connection() as db, db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM pages WHERE task=? AND author=? AND page=?',(task,author,page)).fetchone():
                raise ValueError('尚未确认当前页保存，不能推进分页')
            old = db.execute('SELECT payload FROM checkpoints WHERE task=? AND author=?',(task,author)).fetchone()
            if old and json.loads(old[0]).get('page_number',0) > value['page_number']:
                raise ValueError('旧分页进度不能覆盖已确认进度')
            db.execute('INSERT OR REPLACE INTO checkpoints VALUES (?,?,?,?)',(task,author,page,_json(value)))

    def resume(self, task, author):
        self.ensure_ready()
        with self.connection() as db:
            row = db.execute('SELECT payload FROM checkpoints WHERE task=? AND author=?',(task,author)).fetchone()
            result = json.loads(row[0]) if row else {'page_number':1,'marker':'','complete':False}
            result['saved_ids'] = [r[0] for r in db.execute('SELECT video FROM captured WHERE task=? AND author=?',(task,author))]
            return result

    def reset_cursor(self, task, author):
        self.ensure_ready()
        with self.connection() as db, db:
            db.execute('BEGIN IMMEDIATE')
            key = 'cursor_reset:' + task + ':' + author
            if db.execute('SELECT 1 FROM meta WHERE key=?',(key,)).fetchone():
                raise ValueError('恢复分页失败，已尝试从第一页重采，请稍后重试')
            db.execute('INSERT INTO meta VALUES (?,?)',(key,'1'))
            db.execute('DELETE FROM checkpoints WHERE task=? AND author=?',(task,author))

    def backup(self, destination):
        self.ensure_ready()
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() in {self.path.resolve(),self.source.resolve()} or destination.exists():
            raise ValueError('备份目标已存在或与原文件相同')
        target = sqlite3.connect(destination)
        try:
            with self.connection() as db:
                db.backup(target, pages=256)
        finally:
            target.close()

    def start_maintenance(self, kind):
        if kind not in {'backup','export'}:
            raise ValueError('不支持的作品库操作')
        with _stores_lock:
            if self._maintenance['status'] == 'running':
                return dict(self._maintenance)
            import uuid
            suffix = '.sqlite3' if kind == 'backup' else '.json'
            destination = self.path.parent / 'channels_backups' / (time.strftime('%Y%m%d_%H%M%S')+'_'+uuid.uuid4().hex[:8]+suffix)
            self._maintenance = {'status':'running','kind':kind,'message':'正在备份作品库' if kind=='backup' else '正在导出完整作品数据'}
            def run():
                try:
                    (self.backup if kind=='backup' else self.export_json)(destination)
                    self._maintenance = {'status':'completed','kind':kind,'path':str(destination),'message':'文件已保存：'+str(destination)}
                except Exception as exc:
                    self._maintenance = {'status':'failed','kind':kind,'message':str(exc)}
            threading.Thread(target=run,daemon=True,name='ChannelsStorageBackup').start()
            return dict(self._maintenance)

    def export_json(self, destination):
        self.ensure_ready()
        destination = Path(destination)
        if destination.resolve() in {self.path.resolve(),self.source.resolve()}:
            raise ValueError('导出不能覆盖数据库或迁移前备份')
        destination.parent.mkdir(parents=True, exist_ok=True)
        temp = None
        try:
            with self.connection() as db, tempfile.NamedTemporaryFile('w',encoding='utf-8',dir=destination.parent,delete=False) as output:
                temp = Path(output.name)
                db.execute('BEGIN')
                output.write('{')
                for index,(author,) in enumerate(db.execute('SELECT author FROM authors ORDER BY rowid')):
                    output.write((',' if index else '') + _json(author) + ':[')
                    for number,(payload,) in enumerate(db.execute('SELECT payload FROM feeds WHERE author=? ORDER BY position',(author,))):
                        output.write((',' if number else '') + payload)
                    output.write(']')
                output.write('}')
                output.flush(); os.fsync(output.fileno())
            os.replace(temp,destination)
        finally:
            if temp:
                temp.unlink(missing_ok=True)


_stores, _stores_lock = {}, threading.RLock()


def feed_store(source):
    source = Path(source).resolve()
    from backend.runtime import app_dir
    if source == (app_dir() / 'data' / 'channels_parsed_feeds.json').resolve():
        if sys.platform == 'win32':
            root = Path(os.environ.get('APPDATA') or Path.home() / 'AppData' / 'Roaming') / 'Fandow' / 'SelfMediaContentCollector'
        elif sys.platform == 'darwin':
            root = Path.home() / 'Library' / 'Application Support' / 'SelfMediaContentCollector'
        else:
            root = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'self-media-content-collector'
        database = root / 'channels_data.sqlite3'
    else:
        # Explicit alternative data roots (including tests) never touch the profile.
        database = source.with_suffix('.sqlite3')
    with _stores_lock:
        key = (str(source),str(database))
        if key not in _stores:
            _stores[key] = FeedStore(source,database)
        return _stores[key]
