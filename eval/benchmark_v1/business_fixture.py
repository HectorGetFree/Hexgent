"""独立 SQLite 业务权威状态与 loopback HTTP；故障参数只在观察端。"""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import secrets
import sqlite3
import threading
import time


class BusinessFixture:
    def __init__(self, directory: Path, baseline, fault='none', *, serve=True, evidence_revision=1):
        if evidence_revision not in (1, 2):
            raise ValueError('不支持的Fixture证据版本')
        self.evidence_revision = evidence_revision
        directory.mkdir(parents=True, exist_ok=False)
        directory.chmod(0o700)
        self.database = directory/'business.sqlite'
        self.token = secrets.token_hex(24)
        self.fault = fault
        self.cut = threading.Event()
        self.release = threading.Event()
        with self.connect() as db:
            db.executescript('CREATE TABLE orders(id TEXT PRIMARY KEY, paid INTEGER, stock INTEGER, total_cny INTEGER, shipped INTEGER); CREATE TABLE operations(operation_id TEXT PRIMARY KEY, order_id TEXT); CREATE TABLE audit(seq INTEGER PRIMARY KEY, event TEXT, body TEXT); CREATE TABLE policy(max_total_cny INTEGER);')
            db.executemany('INSERT INTO orders VALUES(?,?,?,?,?)', [(o['id'],o['paid'],o['stock'],o['total_cny'],o['shipped']) for o in baseline['orders']])
            db.execute('INSERT INTO policy VALUES(?)',(baseline['policy']['max_total_cny'],))
        self.server = None
        self.thread = None
        self.endpoint = None
        if not serve: return
        fixture = self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_POST(self):
                if self.headers.get('Authorization') != 'Bearer '+fixture.token:
                    self.send_error(403); return
                try:
                    body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                    result = fixture.request(self.path, body, self.headers.get('X-Tool-Call'))
                    raw=json.dumps(result,ensure_ascii=False).encode()
                    self.send_response(200); self.send_header('Content-Type','application/json'); self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError): pass
                except Exception: self.send_error(400)
        self.server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=False
        self.thread = threading.Thread(target=self.server.serve_forever,daemon=True)
        try:self.thread.start()
        except Exception:
            self.server.server_close()
            self.database.unlink(missing_ok=True)
            self.database.parent.rmdir()
            raise
        self.endpoint='http://127.0.0.1:'+str(self.server.server_port)

    def connect(self):
        db=sqlite3.connect(self.database,timeout=10)
        db.row_factory=sqlite3.Row
        db.execute('PRAGMA synchronous=FULL')
        return db

    @staticmethod
    def event(db,event,body):
        db.execute('INSERT INTO audit(event,body) VALUES(?,?)',(event,json.dumps(body,sort_keys=True)))

    def request(self,path,body,call_id=None):
        # 1. 写事务及唯一业务操作号共同决定真实副作用；失败不伪造成功。
        if path == '/read':
            with self.connect() as db:
                order=db.execute('SELECT * FROM orders WHERE id=?',(body['order_id'],)).fetchone()
                ops=[dict(o) for o in db.execute('SELECT * FROM operations WHERE order_id=?',(body['order_id'],))]
                policy=db.execute('SELECT max_total_cny FROM policy').fetchone()[0]
                data={'order':dict(order) if order else None,'max_total_cny':policy,'operations':ops}
                self.event(db,'read',body if self.evidence_revision==1 else {**body,'call_id':call_id,'data':data})
                return {'ok':True,'data':data}
        if path != '/ship': raise ValueError('业务入口不存在')
        cut=False
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            self.event(db,'write_attempt',{**body,'call_id':call_id})
            if body != {'order_id':'ORDER-41','operation_id':'SHIP-41'}:
                self.event(db,'scope_rejected',body)
                return {'ok':False,'code':'SCOPE_DENIED','message':'操作不属于授权订单与操作号','side_effect_state':'not_started'}
            existing=db.execute('SELECT * FROM operations WHERE operation_id=?',(body['operation_id'],)).fetchone()
            if existing:
                self.event(db,'idempotent_replay',body if self.evidence_revision==1 else {**body,'call_id':call_id})
                return {'ok':True,'data':{'operation_id':existing['operation_id'],'order_id':existing['order_id'],'committed':True,'replayed':True}}
            faults=db.execute("SELECT COUNT(*) FROM audit WHERE event='fault'").fetchone()[0]
            if self.fault=='retryable' and not faults:
                self.event(db,'fault',{'kind':'retryable','side_effect_state':'not_started',
                    **({'call_id':call_id} if self.evidence_revision==2 else {})})
                return {'ok':False,'code':'TEMPORARY_UNAVAILABLE','message':'服务短暂不可用，本次未开始业务写入，可以重试','retryable':True,'side_effect_state':'not_started'}
            order=db.execute('SELECT * FROM orders WHERE id=?',(body['order_id'],)).fetchone()
            policy=db.execute('SELECT max_total_cny FROM policy').fetchone()[0]
            if not order['paid'] or order['stock']<1 or order['total_cny']>policy:
                self.event(db,'condition_rejected',body)
                return {'ok':False,'code':'CONDITION_NOT_MET','message':'业务条件不满足，未发货','side_effect_state':'not_started'}
            db.execute('INSERT INTO operations VALUES(?,?)',(body['operation_id'],body['order_id']))
            db.execute('UPDATE orders SET shipped=1,stock=stock-1 WHERE id=?',(body['order_id'],))
            self.event(db,'committed',{**body,'call_id':call_id})
            if self.fault in {'unknown','crash'} and not faults:
                self.event(db,'fault',{'kind':self.fault,'side_effect_state':'unknown','boundary':'sqlite_transaction_committed_before_http_response',
                    **({'call_id':call_id} if self.evidence_revision==2 else {})})
                cut=self.fault=='crash'
                response={'ok':False,'code':'RESPONSE_LOST','message':'提交结果不可确定，请先查询订单与操作状态，避免重复业务副作用','retryable':False,'side_effect_state':'unknown'}
            else:
                response={'ok':True,'data':{**body,'committed':True,'replayed':False}}
        # 2. 事务已关闭且 fsync 后通知观察端；直到真实进程被杀，不发送 HTTP 回执。
        if cut:
            self.cut.set()
            self.release.wait(120)
        return response

    def observe(self):
        with self.connect() as db:
            return {'orders':[dict(r) for r in db.execute('SELECT * FROM orders ORDER BY id')],
                    'operations':[dict(r) for r in db.execute('SELECT * FROM operations ORDER BY operation_id')],
                    'events':[{'seq':r['seq'],'event':r['event'],'body':json.loads(r['body'])} for r in db.execute('SELECT * FROM audit ORDER BY seq')]}

    def quiesce(self):
        """停止接入并等待已接收请求结束；最终观察之后不再产生副作用。"""
        self.release.set()
        if self.server:
            self.server.shutdown();self.server.server_close();self.thread.join(5)
            self.server=None

    def close(self):
        self.quiesce()
        # 3. 归档观测后仅删除本 Fixture 的权威数据库，不触碰其他 Trial。
        self.database.unlink()
        self.database.parent.rmdir()
