"""PMBA - 20 BPM: Flask + PostgreSQL para implantação no Vercel.
Consulta pública apenas; alteração e exportação detalhada restritas ao administrador.
Sem comandos de exclusão em massa. Sem dados semeados e sem credenciais no código.
"""
from __future__ import annotations
import datetime as dt
import base64, hashlib, hmac, io, json, os, re, secrets, time
from pathlib import Path
from flask import Flask, Response, request, jsonify, send_from_directory
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.pool import NullPool
from werkzeug.exceptions import HTTPException
from core import validate_record, record_from_row, pdf_preview, COUNT_FIELDS

HERE = Path(__file__).resolve().parent
app = Flask(__name__, static_folder=None)
app.config['MAX_CONTENT_LENGTH'] = 12 * 1024 * 1024
DB_URL = (os.environ.get('DATABASE_URL') or os.environ.get('POSTGRES_URL') or '').strip()
ADMIN_USERNAME = os.environ.get('ADMIN_USERNAME', '').strip()
ADMIN_PASSWORD_HASH = os.environ.get('ADMIN_PASSWORD_HASH', '').strip()
SESSION_SECONDS = 12 * 3600
_engine = None
_init_done = False


def _inline_hashes():
    # A página é um único index.html com <style> e <script> embutidos; a CSP só libera exatamente esse conteúdo.
    try:
        html = (HERE / 'index.html').read_text(encoding='utf-8')
    except OSError:
        return '', ''
    h = lambda s: "'sha256-" + base64.b64encode(hashlib.sha256(s.encode('utf-8')).digest()).decode() + "'"
    return (' '.join(h(m) for m in re.findall(r'<script>(.*?)</script>', html, re.S)),
            ' '.join(h(m) for m in re.findall(r'<style>(.*?)</style>', html, re.S)))


SCRIPT_HASHES, STYLE_HASHES = _inline_hashes()


def database():
    global _engine
    if not DB_URL:
        raise RuntimeError('Banco não configurado. Defina DATABASE_URL via Vercel/Neon.')
    if os.environ.get('VERCEL') and not DB_URL.startswith(('postgres://','postgresql://','postgresql+psycopg://')):
        raise RuntimeError('Em produção use PostgreSQL externo. SQLite não persiste na Vercel.')
    if _engine is None:
        url = DB_URL.replace('postgres://', 'postgresql+psycopg://', 1).replace('postgresql://', 'postgresql+psycopg://', 1)
        _engine = create_engine(url, poolclass=NullPool, pool_pre_ping=True, connect_args={'connect_timeout': 10} if url.startswith('postgresql') else {})
    return _engine


def initialize():
    # Duas instâncias podem iniciar ao mesmo tempo e disputar a criação/atualização das tabelas: tenta de novo.
    global _init_done
    if _init_done: return
    from sqlalchemy.exc import DBAPIError
    for attempt in range(4):
        try:
            _initialize_once(); break
        except DBAPIError:
            if attempt == 3: raise
            time.sleep(0.4 * (attempt + 1))
    _init_done = True


def _initialize_once():
    with database().begin() as c:
        c.execute(text('''CREATE TABLE IF NOT EXISTS records (
          id TEXT PRIMARY KEY, date TEXT NOT NULL, time TEXT NOT NULL DEFAULT '',
          cia TEXT NOT NULL, city TEXT NOT NULL, neighborhood TEXT NOT NULL DEFAULT '', location TEXT NOT NULL DEFAULT '',
          ref TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL DEFAULT '', crime_category TEXT NOT NULL DEFAULT 'outros',
          conducoes INTEGER NOT NULL DEFAULT 0, flagrantes INTEGER NOT NULL DEFAULT 0,
          tco INTEGER NOT NULL DEFAULT 0, mandados INTEGER NOT NULL DEFAULT 0, veiculos INTEGER NOT NULL DEFAULT 0,
          armas_brancas INTEGER NOT NULL DEFAULT 0, simulacros INTEGER NOT NULL DEFAULT 0, adolescentes INTEGER NOT NULL DEFAULT 0,
          drugs TEXT NOT NULL DEFAULT '[]', guns TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )'''))
        cols={x[0] for x in c.execute(text("SELECT * FROM records WHERE 1=0")).cursor.description}
        for col in ('lat','lon'):
            if col not in cols:c.execute(text(f'ALTER TABLE records ADD COLUMN {col} DOUBLE PRECISION'))
        for col in ('armas_brancas','simulacros','adolescentes'):
            if col not in cols:c.execute(text(f'ALTER TABLE records ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0'))
        c.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS records_identify ON records(date, ref) WHERE ref != ''"))
        c.execute(text('''CREATE TABLE IF NOT EXISTS sessions (
          token_hash TEXT PRIMARY KEY, username TEXT NOT NULL,
          csrf TEXT NOT NULL, expires BIGINT NOT NULL
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS draft (
          username TEXT PRIMARY KEY, body TEXT NOT NULL, updated_at TEXT NOT NULL
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS geo (
          city TEXT NOT NULL, neighborhood TEXT NOT NULL,
          lat DOUBLE PRECISION NOT NULL, lon DOUBLE PRECISION NOT NULL,
          PRIMARY KEY(city, neighborhood)
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS login_limits (
          key TEXT PRIMARY KEY, attempts INTEGER NOT NULL, window_started BIGINT NOT NULL
        )'''))


def query_one(sql, params=None):
    initialize()
    with database().connect() as c:
        row = c.execute(text(sql), params or {}).fetchone()
        return dict(row._mapping) if row else None


def get_records(private=False):
    initialize()
    with database().connect() as c:
        rows = c.execute(text('SELECT * FROM records ORDER BY date DESC,time DESC,created_at DESC')).fetchall()
    return [record_from_row(dict(x._mapping),private) for x in rows]


def insert_records(records):
    if not isinstance(records,list) or not 1 <= len(records) <= 200:
        raise ValueError('Envie entre 1 e 200 registros por lote.')
    valid=[validate_record(x) for x in records]
    now=dt.datetime.now(dt.timezone.utc).isoformat()
    inserted, skipped = [], []
    initialize()
    with database().begin() as c:
        for r in valid:
            if r['ref'] and c.execute(text('SELECT id FROM records WHERE date=:d AND ref=:r'),{'d':r['date'],'r':r['ref']}).fetchone():
                skipped.append(r['ref']); continue
            uid=secrets.token_urlsafe(16)
            params={**r,'id':uid,'created_at':now,'updated_at':now,'drugs':json.dumps(r['drugs'],ensure_ascii=False),'guns':json.dumps(r['guns'],ensure_ascii=False)}
            c.execute(text('''INSERT INTO records(id,date,time,cia,city,neighborhood,location,ref,kind,crime_category,
               conducoes,flagrantes,tco,mandados,veiculos,armas_brancas,simulacros,adolescentes,drugs,guns,notes,source,lat,lon,created_at,updated_at)
               VALUES(:id,:date,:time,:cia,:city,:neighborhood,:location,:ref,:kind,:crime_category,
               :conducoes,:flagrantes,:tco,:mandados,:veiculos,:armas_brancas,:simulacros,:adolescentes,:drugs,:guns,:notes,:source,:lat,:lon,:created_at,:updated_at)'''),params)
            inserted.append(uid)
    return {'inserted':len(inserted),'skipped':len(skipped),'skipped_refs':skipped}


def password_ok(candidate):
    try:
        algo,iterations,salt,digest=ADMIN_PASSWORD_HASH.split('$',3)
        if algo!='pbkdf2_sha256' or not (200_000<=int(iterations)<=2_000_000): return False
        got=hashlib.pbkdf2_hmac('sha256',candidate.encode(),bytes.fromhex(salt),int(iterations)).hex()
        return hmac.compare_digest(got,digest)
    except (ValueError,UnicodeError,TypeError): return False


def session():
    token = request.cookies.get('sess','')
    if not token or len(token)>200:return None
    digest=hashlib.sha256(token.encode()).hexdigest()
    row=query_one('SELECT username,csrf,expires FROM sessions WHERE token_hash=:hash',{'hash':digest})
    return row if row and int(row['expires'])>int(time.time()) else None


def admin():
    s=session()
    if not s:return None
    check=request.headers.get('X-CSRF-Token','')
    if not check or not hmac.compare_digest(check,str(s['csrf'])): return None
    return s


def json_body():
    if request.content_length and request.content_length > 2 * 1024 * 1024: raise ValueError('JSON muito grande.')
    if not request.is_json: raise ValueError('Envie Content-Type application/json.')
    obj=request.get_json(silent=True)
    if obj is None:raise ValueError('JSON inválido.')
    return obj


def login_key():
    ip=(request.headers.get('X-Forwarded-For','') or request.remote_addr or 'unknown').split(',')[0].strip()
    return hmac.new(ADMIN_PASSWORD_HASH.encode(),ip.encode(),hashlib.sha256).hexdigest()


def login_attempts(key):
    row=query_one('SELECT attempts,window_started FROM login_limits WHERE key=:key',{'key':key})
    return int(row['attempts']) if row and int(time.time())-int(row['window_started'])<900 else 0


def register_failed_login(key):
    now=int(time.time())
    with database().begin() as c:
        c.execute(text('''INSERT INTO login_limits(key,attempts,window_started) VALUES(:key,1,:now)
          ON CONFLICT(key) DO UPDATE SET
          attempts=CASE WHEN login_limits.window_started<:older THEN 1 ELSE login_limits.attempts+1 END,
          window_started=CASE WHEN login_limits.window_started<:older THEN :now ELSE login_limits.window_started END'''),
          {'key':key,'now':now,'older':now-900})


def no_access(message='Acesso administrativo necessário.',status=401):
    return jsonify({'error':message}),status


@app.after_request
def headers(resp):
    resp.headers['X-Content-Type-Options']='nosniff'
    resp.headers['Referrer-Policy']='strict-origin-when-cross-origin'  # o servidor de mapas (OpenStreetMap) exige a origem do site
    resp.headers['X-Frame-Options']='DENY'
    resp.headers['Content-Security-Policy']=f"default-src 'self'; script-src 'self' https://unpkg.com {SCRIPT_HASHES}; style-src 'self' https://unpkg.com {STYLE_HASHES}; img-src 'self' data: https://*.tile.openstreetmap.org https://unpkg.com; connect-src 'self' https://nominatim.openstreetmap.org; object-src 'none'; base-uri 'none'; form-action 'self'; frame-ancestors 'none'"
    resp.headers['Cache-Control']='no-store'
    return resp


@app.errorhandler(413)
def too_big(err):return jsonify({'error':'O arquivo enviado ultrapassa 12 MB.'}),413


@app.errorhandler(ValueError)
def bad_input(err):return jsonify({'error':str(err)}),400


@app.errorhandler(IntegrityError)
def duplicate(err):return jsonify({'error':'Número e data já cadastrados. Verifique duplicidade.'}),409


@app.errorhandler(Exception)
def internal(err):
    if isinstance(err,HTTPException):
        return jsonify({'error':'Não encontrado.' if err.code==404 else (err.description or 'Requisição inválida.')}),err.code or 500
    if isinstance(err,RuntimeError) and 'Banco não configurado' in str(err):
        return jsonify({'error':'Banco de dados não configurado. Solicite ao administrador a configuração do PostgreSQL (DATABASE_URL).'}),503
    app.logger.exception('Erro interno no painel')
    return jsonify({'error':'Erro no servidor. Verifique os logs da Vercel e o banco de dados.'}),500


@app.get('/api/session')
def api_session():
    s=session()
    return jsonify({'admin':bool(s),'username':s['username'] if s else None,'csrf':s['csrf'] if s else None})


@app.get('/api/records')
def api_records():
    return jsonify({'records':get_records(private=bool(session())),'admin':bool(session())})


@app.post('/api/geo/resolve')
def api_geo_resolve():
    # Abre um link curto do Google Maps (maps.app.goo.gl) para obter o endereço completo, que contém a coordenada.
    if not admin():return no_access()
    body=json_body(); url=str(body.get('url','') if isinstance(body,dict) else '').strip()[:500]
    from urllib.parse import urlsplit
    import urllib.request
    ok_hosts=('maps.app.goo.gl','goo.gl','maps.google.com','www.google.com','google.com','www.google.com.br','google.com.br')
    if urlsplit(url).scheme!='https' or urlsplit(url).hostname not in ok_hosts:raise ValueError('Link do Google Maps inválido.')
    class Guard(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            u=urlsplit(newurl)
            if u.scheme!='https' or not (u.hostname or '').endswith(('google.com','google.com.br','goo.gl')):return None
            return super().redirect_request(req, fp, code, msg, headers, newurl)
    final=url
    try:
        req=urllib.request.Request(url,headers={'User-Agent':'Mozilla/5.0'})
        with urllib.request.build_opener(Guard).open(req,timeout=8) as r:final=r.geturl()
    except urllib.error.HTTPError as e:final=e.geturl() or url
    except Exception:raise ValueError('Não foi possível abrir o link. Cole a coordenada (ex.: -9.4062, -38.2147).')
    return jsonify({'url':final})


@app.get('/api/geo')
def api_geo():
    # Posição (lat/lon) de cada bairro para o mapa de calor. Não contém dados de ocorrências.
    initialize()
    with database().connect() as c:
        rows=c.execute(text('SELECT city,neighborhood,lat,lon FROM geo')).fetchall()
    return jsonify({'places':[dict(x._mapping) for x in rows]})


@app.post('/api/geo')
def api_geo_save():
    if not admin():return no_access()
    body=json_body()
    places=body.get('places') if isinstance(body,dict) else None
    if not isinstance(places,list) or not 1<=len(places)<=200:raise ValueError('Lista de locais inválida.')
    ok=[]
    for p in places:
        if not isinstance(p,dict):raise ValueError('Local inválido.')
        city=str(p.get('city') or '').strip();nb=str(p.get('neighborhood') or '').strip()
        try:lat=float(p.get('lat'));lon=float(p.get('lon'))
        except (TypeError,ValueError):raise ValueError('Coordenadas inválidas.')
        if not nb or len(city)>100 or len(nb)>100 or not (-90<=lat<=90 and -180<=lon<=180):raise ValueError('Local inválido.')
        ok.append({'city':city,'nb':nb,'lat':lat,'lon':lon})
    with database().begin() as c:
        for p in ok:
            c.execute(text('''INSERT INTO geo(city,neighborhood,lat,lon) VALUES(:city,:nb,:lat,:lon)
              ON CONFLICT(city,neighborhood) DO UPDATE SET lat=excluded.lat,lon=excluded.lon'''),p)
    return jsonify({'ok':True,'saved':len(ok)})


@app.get('/api/draft')
def api_draft_get():
    s=session()
    if not s:return no_access()
    row=query_one('SELECT body FROM draft WHERE username=:u',{'u':s['username']})
    return jsonify({'draft':json.loads(row['body']) if row else None})


@app.get('/api/backup')
def api_backup():
    if not session():return no_access()
    b=json.dumps({'version':2,'exported_at':dt.datetime.now(dt.timezone.utc).isoformat(),'records':get_records(True)},ensure_ascii=False,indent=2)
    return Response(b,content_type='application/json; charset=utf-8',headers={'Content-Disposition':'attachment; filename="backup_20bpm.json"'})


@app.get('/api/export.xlsx')
def api_export():
    if not session():return no_access('Somente o administrador pode exportar dados completos.')
    from excel_export import build_xlsx
    b=build_xlsx(get_records(True))
    return Response(b,content_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',headers={'Content-Disposition':'attachment; filename="estatisticas_operacionais_20bpm.xlsx"'})


@app.get('/api/export.pdf')
def api_export_pdf():
    s=session()
    if not s:return no_access('Somente o administrador pode exportar dados completos.')
    from pdf_export import build_pdf
    b=build_pdf(get_records(True),s['username'])
    return Response(b,content_type='application/pdf',headers={'Content-Disposition':'attachment; filename="planilha_ocorrencias_20bpm.pdf"'})


@app.post('/api/login')
def api_login():
    if not ADMIN_USERNAME or not ADMIN_PASSWORD_HASH:
        return no_access('Administrador ainda não configurado. Defina ADMIN_USERNAME e ADMIN_PASSWORD_HASH na Vercel.',503)
    key=login_key()
    if login_attempts(key)>=7:return no_access('Muitas tentativas. Tente novamente em 15 minutos.',429)
    data=json_body()
    if not isinstance(data,dict):raise ValueError('Credenciais inválidas.')
    username=str(data.get('username',''))[:80]
    password=str(data.get('password',''))[:300]
    if hmac.compare_digest(username,ADMIN_USERNAME) and password_ok(password):
        token=secrets.token_urlsafe(40)
        csrf=secrets.token_urlsafe(32)
        exp=int(time.time())+SESSION_SECONDS
        with database().begin() as c:
            c.execute(text('DELETE FROM login_limits WHERE key=:key'),{'key':key})
            c.execute(text('DELETE FROM sessions WHERE expires<:now'),{'now':int(time.time())})
            c.execute(text('INSERT INTO sessions(token_hash,username,csrf,expires) VALUES(:hash,:u,:csrf,:exp)'),
                      {'hash':hashlib.sha256(token.encode()).hexdigest(),'u':username,'csrf':csrf,'exp':exp})
        resp=jsonify({'admin':True,'username':username,'csrf':csrf})
        resp.set_cookie('sess',token,max_age=SESSION_SECONDS,secure=bool(os.environ.get('VERCEL') or os.environ.get('APP_SECURE_COOKIES')=='1'),httponly=True,samesite='Strict',path='/')
        return resp
    register_failed_login(key)
    return no_access('Usuário ou senha incorretos.',401)


@app.post('/api/logout')
def api_logout():
    s=admin()
    if not s:return no_access()
    token=request.cookies.get('sess','')
    with database().begin() as c:
        c.execute(text('DELETE FROM sessions WHERE token_hash=:hash'),{'hash':hashlib.sha256(token.encode()).hexdigest()})
    resp=jsonify({'ok':True})
    resp.delete_cookie('sess',path='/',secure=bool(os.environ.get('VERCEL')),httponly=True,samesite='Strict')
    return resp


@app.post('/api/pdf/preview')
def api_pdf_preview():
    if not admin():return no_access()
    if request.mimetype!='application/pdf':raise ValueError('Envie um PDF válido com Content-Type application/pdf.')
    raw=request.get_data(cache=False)
    if not raw.startswith(b'%PDF-'):raise ValueError('Arquivo PDF inválido.')
    if len(raw)>12*1024*1024:raise ValueError('PDF excede 12 MB.')
    return jsonify(pdf_preview(raw))


@app.post('/api/records')
def api_record_create():
    if not admin():return no_access()
    return jsonify(insert_records([json_body()])),201


@app.post('/api/records/import')
def api_records_import():
    if not admin():return no_access()
    body=json_body()
    if not isinstance(body,dict):raise ValueError('Requisição inválida.')
    return jsonify(insert_records(body.get('records',[])))


@app.post('/api/draft')
def api_draft_save():
    s=admin()
    if not s:return no_access()
    body=json.dumps(json_body(),ensure_ascii=False)
    with database().begin() as c:
        c.execute(text('''INSERT INTO draft(username,body,updated_at) VALUES(:u,:body,:now)
            ON CONFLICT(username) DO UPDATE SET body=excluded.body,updated_at=excluded.updated_at'''),
            {'u':s['username'],'body':body,'now':dt.datetime.now(dt.timezone.utc).isoformat()})
    return jsonify({'ok':True})


@app.post('/api/draft/clear')
def api_draft_clear():
    s=admin()
    if not s:return no_access()
    with database().begin() as c:c.execute(text('DELETE FROM draft WHERE username=:u'),{'u':s['username']})
    return jsonify({'ok':True})


@app.put('/api/records/<uid>')
def api_record_edit(uid):
    if not admin():return no_access()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',uid):return no_access('Ocorrência inválida.',400)
    r=validate_record(json_body())
    r={**r,'drugs':json.dumps(r['drugs'],ensure_ascii=False),'guns':json.dumps(r['guns'],ensure_ascii=False),'updated_at':dt.datetime.now(dt.timezone.utc).isoformat(),'id':uid}
    with database().begin() as c:
        result=c.execute(text('''UPDATE records SET date=:date,time=:time,cia=:cia,city=:city,
          neighborhood=:neighborhood,location=:location,ref=:ref,kind=:kind,crime_category=:crime_category,
          conducoes=:conducoes,flagrantes=:flagrantes,tco=:tco,mandados=:mandados,veiculos=:veiculos,
          armas_brancas=:armas_brancas,simulacros=:simulacros,adolescentes=:adolescentes,
          drugs=:drugs,guns=:guns,notes=:notes,source=:source,lat=:lat,lon=:lon,updated_at=:updated_at WHERE id=:id'''),r)
    if result.rowcount!=1:return no_access('Ocorrência não encontrada.',404)
    return jsonify({'ok':True})


@app.delete('/api/records/<uid>')
def api_record_delete(uid):
    if not admin():return no_access()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',uid):return no_access('Ocorrência inválida.',400)
    with database().begin() as c:result=c.execute(text('DELETE FROM records WHERE id=:id'),{'id':uid})
    return jsonify({'ok':True,'deleted':result.rowcount})


@app.route('/')
@app.route('/index.html')
def home():return send_from_directory(HERE,'index.html')


@app.route('/<path:filename>')
def files(filename):
    return no_access('Não encontrado.',404)


if __name__=='__main__':
    app.run(debug=False,host='127.0.0.1',port=int(os.environ.get('PORT','8080')))