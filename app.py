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
from core import validate_record, record_from_row, pdf_preview, COUNT_FIELDS, norm_bairro, bairro_key, canon_bairros

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
          conduzidos INTEGER NOT NULL DEFAULT 0, presos INTEGER NOT NULL DEFAULT 0, grupo TEXT NOT NULL DEFAULT '',
          created_by TEXT NOT NULL DEFAULT '', updated_by TEXT NOT NULL DEFAULT '',
          drugs TEXT NOT NULL DEFAULT '[]', guns TEXT NOT NULL DEFAULT '[]', notes TEXT NOT NULL DEFAULT '',
          source TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        )'''))
        cols={x[0] for x in c.execute(text("SELECT * FROM records WHERE 1=0")).cursor.description}
        for col in ('lat','lon'):
            if col not in cols:c.execute(text(f'ALTER TABLE records ADD COLUMN {col} DOUBLE PRECISION'))
        for col in ('grupo','created_by','updated_by'):
            if col not in cols:c.execute(text(f"ALTER TABLE records ADD COLUMN {col} TEXT NOT NULL DEFAULT ''"))
        for col in ('armas_brancas','simulacros','adolescentes','conduzidos','presos'):
            if col not in cols:c.execute(text(f'ALTER TABLE records ADD COLUMN {col} INTEGER NOT NULL DEFAULT 0'))
        if 'conduzidos' not in cols:  # ocorrências antigas: ao menos 1 pessoa por condução/flagrante registrado
            c.execute(text('UPDATE records SET conduzidos=conducoes WHERE conduzidos=0 AND conducoes>0'))
            c.execute(text('UPDATE records SET presos=flagrantes WHERE presos=0 AND flagrantes>0'))
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
        c.execute(text('''CREATE TABLE IF NOT EXISTS users (
          username TEXT PRIMARY KEY, name TEXT NOT NULL DEFAULT '', password_hash TEXT NOT NULL,
          role TEXT NOT NULL DEFAULT 'operador', active INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL
        )'''))
        c.execute(text('''CREATE TABLE IF NOT EXISTS audit (
          id TEXT PRIMARY KEY, ts TEXT NOT NULL, username TEXT NOT NULL, action TEXT NOT NULL,
          target TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT ''
        )'''))
        c.execute(text('CREATE INDEX IF NOT EXISTS audit_ts ON audit(ts)'))
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
    recs=[record_from_row(dict(x._mapping),private) for x in rows]
    canon=canon_bairros((r['city'],r['neighborhood']) for r in recs)   # mesma grafia para o mesmo bairro
    for r in recs:
        if r['neighborhood']:r['neighborhood']=canon.get(bairro_key(r['city'],r['neighborhood']),r['neighborhood'])
    return recs


def now_iso():return dt.datetime.now(dt.timezone.utc).isoformat()


def log(c,user,action,target='',detail=''):
    # Registro de auditoria: quem fez o quê e quando.
    c.execute(text('INSERT INTO audit(id,ts,username,action,target,detail) VALUES(:id,:ts,:u,:a,:t,:d)'),
              {'id':secrets.token_urlsafe(12),'ts':now_iso(),'u':user,'a':action,'t':str(target)[:200],'d':str(detail)[:20000]})


def rec_label(r):return f"nº {r.get('ref') or 's/n'} de {'/'.join(reversed(str(r.get('date','')).split('-')))} ({str(r.get('kind') or 'sem tipo')[:60]})"


def insert_records(records,user=''):
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
            params={**r,'id':uid,'created_at':now,'updated_at':now,'created_by':user,'updated_by':user,'drugs':json.dumps(r['drugs'],ensure_ascii=False),'guns':json.dumps(r['guns'],ensure_ascii=False)}
            c.execute(text('''INSERT INTO records(id,date,time,cia,city,neighborhood,location,ref,kind,crime_category,
               conducoes,flagrantes,tco,mandados,veiculos,armas_brancas,simulacros,adolescentes,conduzidos,presos,grupo,drugs,guns,notes,source,lat,lon,created_by,updated_by,created_at,updated_at)
               VALUES(:id,:date,:time,:cia,:city,:neighborhood,:location,:ref,:kind,:crime_category,
               :conducoes,:flagrantes,:tco,:mandados,:veiculos,:armas_brancas,:simulacros,:adolescentes,:conduzidos,:presos,:grupo,:drugs,:guns,:notes,:source,:lat,:lon,:created_by,:updated_by,:created_at,:updated_at)'''),params)
            log(c,user,'Cadastrou ocorrência',rec_label(r),r.get('source',''))
            inserted.append(uid)
    return {'inserted':len(inserted),'skipped':len(skipped),'skipped_refs':skipped}


def verify_hash(stored,candidate):
    try:
        algo,iterations,salt,digest=str(stored).split('$',3)
        if algo!='pbkdf2_sha256' or not (200_000<=int(iterations)<=2_000_000): return False
        got=hashlib.pbkdf2_hmac('sha256',candidate.encode(),bytes.fromhex(salt),int(iterations)).hex()
        return hmac.compare_digest(got,digest)
    except (ValueError,UnicodeError,TypeError): return False


def make_hash(password):
    salt=secrets.token_hex(16);rounds=600_000
    return f"pbkdf2_sha256${rounds}${salt}${hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),rounds).hex()}"


def check_new_password(p):
    p=str(p or '')
    if len(p)<8 or len(p)>200:raise ValueError('A senha deve ter pelo menos 8 caracteres.')
    if p.isdigit() or p.lower() in ('password','senha123','12345678','123456789','qwertyui'):raise ValueError('Senha muito fraca: misture letras e números.')
    return p


def account(username):
    """Perfil do usuário: o administrador principal vem das variáveis da Vercel; os demais, da tabela users."""
    if ADMIN_USERNAME and hmac.compare_digest(str(username),ADMIN_USERNAME):
        return {'username':ADMIN_USERNAME,'name':'Administrador principal','role':'admin','master':True}
    row=query_one('SELECT username,name,role FROM users WHERE username=:u AND active=1',{'u':username})
    return {**row,'master':False} if row else None


def session():
    """Sessão de quem está logado (qualquer perfil). Conta desativada perde o acesso na hora."""
    token = request.cookies.get('sess','')
    if not token or len(token)>200:return None
    digest=hashlib.sha256(token.encode()).hexdigest()
    row=query_one('SELECT username,csrf,expires FROM sessions WHERE token_hash=:hash',{'hash':digest})
    if not row or int(row['expires'])<=int(time.time()):return None
    acc=account(row['username'])
    return {**row,**acc} if acc else None


def admin():
    """Usuário logado + token CSRF (necessário para qualquer alteração)."""
    s=session()
    if not s:return None
    check=request.headers.get('X-CSRF-Token','')
    if not check or not hmac.compare_digest(check,str(s['csrf'])): return None
    return s


def boss():
    """Somente perfil Administrador (usuários, auditoria, exclusão)."""
    s=admin()
    return s if s and s['role']=='admin' else None


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
    return jsonify({'admin':bool(s),'username':s['username'] if s else None,'csrf':s['csrf'] if s else None,
                    'role':s['role'] if s else None,'name':s['name'] if s else None,'master':bool(s and s['master'])})


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
    # Posição (lat/lon) de cada bairro para o mapa. Não contém dados de ocorrências.
    initialize()
    with database().connect() as c:
        rows=c.execute(text('SELECT city,neighborhood,lat,lon FROM geo')).fetchall()
        used=c.execute(text("SELECT DISTINCT city,neighborhood FROM records WHERE neighborhood!=''")).fetchall()
    canon=canon_bairros((x[0],x[1]) for x in used)
    out={}
    for x in rows:
        d=dict(x._mapping);k=bairro_key(d['city'],d['neighborhood'])
        d['neighborhood']=canon.get(k,norm_bairro(d['neighborhood']));out[k]=d
    return jsonify({'places':list(out.values())})


@app.post('/api/geo')
def api_geo_save():
    if not admin():return no_access()
    body=json_body()
    places=body.get('places') if isinstance(body,dict) else None
    if not isinstance(places,list) or not 1<=len(places)<=200:raise ValueError('Lista de locais inválida.')
    ok=[]
    for p in places:
        if not isinstance(p,dict):raise ValueError('Local inválido.')
        city=str(p.get('city') or '').strip();nb=norm_bairro(p.get('neighborhood'))
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
    username=str(data.get('username','')).strip()[:80]
    password=str(data.get('password',''))[:300]
    if hmac.compare_digest(username,ADMIN_USERNAME):
        ok=verify_hash(ADMIN_PASSWORD_HASH,password)
    else:
        row=query_one('SELECT password_hash FROM users WHERE username=:u AND active=1',{'u':username})
        ok=verify_hash(row['password_hash'] if row else ADMIN_PASSWORD_HASH,password) and bool(row)
    if ok:
        acc=account(username)
        token=secrets.token_urlsafe(40)
        csrf=secrets.token_urlsafe(32)
        exp=int(time.time())+SESSION_SECONDS
        with database().begin() as c:
            c.execute(text('DELETE FROM login_limits WHERE key=:key'),{'key':key})
            c.execute(text('DELETE FROM sessions WHERE expires<:now'),{'now':int(time.time())})
            c.execute(text('INSERT INTO sessions(token_hash,username,csrf,expires) VALUES(:hash,:u,:csrf,:exp)'),
                      {'hash':hashlib.sha256(token.encode()).hexdigest(),'u':username,'csrf':csrf,'exp':exp})
            log(c,username,'Entrou no sistema')
        resp=jsonify({'admin':True,'username':username,'csrf':csrf,'role':acc['role'],'name':acc['name'],'master':acc['master']})
        resp.set_cookie('sess',token,max_age=SESSION_SECONDS,secure=bool(os.environ.get('VERCEL') or os.environ.get('APP_SECURE_COOKIES')=='1'),httponly=True,samesite='Strict',path='/')
        return resp
    register_failed_login(key)
    return no_access('Usuário ou senha incorretos.',401)


# ---------- Usuários (somente perfil Administrador) ----------
ROLES=('admin','operador')


@app.get('/api/users')
def api_users():
    s=session()
    if not s or s['role']!='admin':return no_access('Somente administradores.',403)
    with database().connect() as c:
        rows=c.execute(text('SELECT username,name,role,active,created_at FROM users ORDER BY username')).fetchall()
    return jsonify({'users':[{'username':ADMIN_USERNAME,'name':'Administrador principal','role':'admin','active':1,'master':True}]+[dict(x._mapping) for x in rows]})


@app.post('/api/users')
def api_user_create():
    s=boss()
    if not s:return no_access('Somente administradores.',403)
    d=json_body()
    if not isinstance(d,dict):raise ValueError('Dados inválidos.')
    u=str(d.get('username','')).strip();name=str(d.get('name','')).strip()[:80];role=str(d.get('role','operador'))
    if not re.fullmatch(r'[A-Za-z0-9._-]{3,40}',u):raise ValueError('Usuário: use de 3 a 40 letras, números, ponto, hífen ou sublinhado (sem espaços).')
    if hmac.compare_digest(u,ADMIN_USERNAME):raise ValueError('Esse usuário já existe.')
    if role not in ROLES:raise ValueError('Perfil inválido.')
    if not name:raise ValueError('Informe o nome da pessoa.')
    pw=check_new_password(d.get('password'))
    with database().begin() as c:
        if c.execute(text('SELECT 1 FROM users WHERE username=:u'),{'u':u}).fetchone():raise ValueError('Esse usuário já existe.')
        c.execute(text('INSERT INTO users(username,name,password_hash,role,active,created_at) VALUES(:u,:n,:h,:r,1,:t)'),{'u':u,'n':name,'h':make_hash(pw),'r':role,'t':now_iso()})
        log(c,s['username'],'Criou usuário',u,f'{name} · perfil {role}')
    return jsonify({'ok':True}),201


@app.put('/api/users/<username>')
def api_user_update(username):
    s=boss()
    if not s:return no_access('Somente administradores.',403)
    d=json_body()
    if not isinstance(d,dict):raise ValueError('Dados inválidos.')
    if hmac.compare_digest(username,ADMIN_USERNAME):raise ValueError('O administrador principal é configurado na Vercel e não pode ser alterado aqui.')
    with database().begin() as c:
        row=c.execute(text('SELECT name,role,active FROM users WHERE username=:u'),{'u':username}).fetchone()
        if not row:return no_access('Usuário não encontrado.',404)
        changes=[]
        if 'name' in d:
            name=str(d['name']).strip()[:80]
            if not name:raise ValueError('Informe o nome da pessoa.')
            c.execute(text('UPDATE users SET name=:n WHERE username=:u'),{'n':name,'u':username});changes.append(f'nome: {name}')
        if 'role' in d:
            if d['role'] not in ROLES:raise ValueError('Perfil inválido.')
            c.execute(text('UPDATE users SET role=:r WHERE username=:u'),{'r':d['role'],'u':username});changes.append(f"perfil: {d['role']}")
        if 'active' in d:
            act=1 if d['active'] else 0
            c.execute(text('UPDATE users SET active=:a WHERE username=:u'),{'a':act,'u':username});changes.append('reativado' if act else 'desativado')
            if not act:c.execute(text('DELETE FROM sessions WHERE username=:u'),{'u':username})
        if d.get('password'):
            c.execute(text('UPDATE users SET password_hash=:h WHERE username=:u'),{'h':make_hash(check_new_password(d['password'])),'u':username})
            c.execute(text('DELETE FROM sessions WHERE username=:u'),{'u':username});changes.append('senha redefinida')
        log(c,s['username'],'Alterou usuário',username,'; '.join(changes))
    return jsonify({'ok':True})


@app.post('/api/password')
def api_password():
    s=admin()
    if not s:return no_access()
    if s['master']:raise ValueError('A senha do administrador principal fica nas configurações da Vercel (ADMIN_PASSWORD_HASH) e não pode ser trocada por aqui.')
    d=json_body()
    if not isinstance(d,dict):raise ValueError('Dados inválidos.')
    row=query_one('SELECT password_hash FROM users WHERE username=:u',{'u':s['username']})
    if not row or not verify_hash(row['password_hash'],str(d.get('current',''))):raise ValueError('Senha atual incorreta.')
    pw=check_new_password(d.get('new'))
    with database().begin() as c:
        c.execute(text('UPDATE users SET password_hash=:h WHERE username=:u'),{'h':make_hash(pw),'u':s['username']})
        log(c,s['username'],'Trocou a própria senha')
    return jsonify({'ok':True})


@app.get('/api/audit')
def api_audit():
    s=session()
    if not s or s['role']!='admin':return no_access('Somente administradores.',403)
    with database().connect() as c:
        rows=c.execute(text('SELECT ts,username,action,target,detail FROM audit ORDER BY ts DESC LIMIT 500')).fetchall()
    return jsonify({'entries':[dict(x._mapping) for x in rows]})


@app.post('/api/bairros/rename')
def api_bairro_rename():
    # Junta um bairro escrito de outro jeito (ex.: "Seriema I" -> "Siriema I") em todas as ocorrências da cidade.
    s=admin()
    if not s:return no_access()
    d=json_body()
    if not isinstance(d,dict):raise ValueError('Dados inválidos.')
    city=str(d.get('city','')).strip();old=norm_bairro(d.get('from'));new=norm_bairro(d.get('to'))
    if not old or not new or len(new)>100:raise ValueError('Informe o bairro atual e o nome correto.')
    ko,kn=bairro_key(city,old),bairro_key(city,new)
    n=0
    with database().begin() as c:
        for rid,nb in c.execute(text('SELECT id,neighborhood FROM records WHERE city=:c'),{'c':city}).fetchall():
            if bairro_key(city,nb) in (ko,kn) and nb!=new:
                c.execute(text('UPDATE records SET neighborhood=:n,updated_at=:t,updated_by=:u WHERE id=:id'),{'n':new,'t':now_iso(),'u':s['username'],'id':rid});n+=1
        geo={bairro_key(city,g[0]):g for g in c.execute(text('SELECT neighborhood,lat,lon FROM geo WHERE city=:c'),{'c':city}).fetchall()}
        keep=geo.get(kn) or geo.get(ko)
        for k,g in geo.items():
            if k in (ko,kn):c.execute(text('DELETE FROM geo WHERE city=:c AND neighborhood=:n'),{'c':city,'n':g[0]})
        if keep:c.execute(text('INSERT INTO geo(city,neighborhood,lat,lon) VALUES(:c,:n,:la,:lo)'),{'c':city,'n':new,'la':keep[1],'lo':keep[2]})
        log(c,s['username'],'Renomeou bairro',f'{old} → {new} ({city})',f'{n} ocorrência(s) alterada(s)')
    return jsonify({'ok':True,'changed':n})


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
    s=admin()
    if not s:return no_access()
    return jsonify(insert_records([json_body()],s['username'])),201


@app.post('/api/records/import')
def api_records_import():
    s=admin()
    if not s:return no_access()
    body=json_body()
    if not isinstance(body,dict):raise ValueError('Requisição inválida.')
    return jsonify(insert_records(body.get('records',[]),s['username']))


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


FIELD_NAMES={'date':'data','time':'hora','cia':'CIA','city':'cidade','neighborhood':'bairro','location':'endereço','ref':'número','kind':'tipo',
  'crime_category':'classificação','grupo':'grupo','conducoes':'conduções','flagrantes':'flagrantes','tco':'TCO','mandados':'mandados','veiculos':'veículos',
  'armas_brancas':'armas brancas','simulacros':'simulacros','adolescentes':'adolescentes','conduzidos':'pessoas conduzidas','presos':'pessoas presas',
  'drugs':'drogas','guns':'armas de fogo','notes':'observações','source':'origem','lat':'latitude','lon':'longitude'}


@app.put('/api/records/<uid>')
def api_record_edit(uid):
    s=admin()
    if not s:return no_access()
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',uid):return no_access('Ocorrência inválida.',400)
    r=validate_record(json_body())
    r={**r,'drugs':json.dumps(r['drugs'],ensure_ascii=False),'guns':json.dumps(r['guns'],ensure_ascii=False),'updated_at':now_iso(),'updated_by':s['username'],'id':uid}
    with database().begin() as c:
        old=c.execute(text('SELECT * FROM records WHERE id=:id'),{'id':uid}).fetchone()
        if not old:return no_access('Ocorrência não encontrada.',404)
        old=dict(old._mapping)
        diff=[f"{FIELD_NAMES[k]}: {old.get(k) if old.get(k) not in (None,'') else '(vazio)'} → {r[k] if r[k] not in (None,'') else '(vazio)'}" for k in FIELD_NAMES if str(old.get(k) if old.get(k) is not None else '')!=str(r[k] if r[k] is not None else '')]
        c.execute(text('''UPDATE records SET date=:date,time=:time,cia=:cia,city=:city,
          neighborhood=:neighborhood,location=:location,ref=:ref,kind=:kind,crime_category=:crime_category,
          conducoes=:conducoes,flagrantes=:flagrantes,tco=:tco,mandados=:mandados,veiculos=:veiculos,
          armas_brancas=:armas_brancas,simulacros=:simulacros,adolescentes=:adolescentes,conduzidos=:conduzidos,presos=:presos,grupo=:grupo,
          drugs=:drugs,guns=:guns,notes=:notes,source=:source,lat=:lat,lon=:lon,updated_at=:updated_at,updated_by=:updated_by WHERE id=:id'''),r)
        log(c,s['username'],'Editou ocorrência',rec_label(r),'; '.join(diff) or 'sem alterações')
    return jsonify({'ok':True})


@app.delete('/api/records/<uid>')
def api_record_delete(uid):
    s=boss()
    if not s:return no_access('Somente administradores podem excluir ocorrências.',403)
    if not re.fullmatch(r'[a-zA-Z0-9_-]{8,100}',uid):return no_access('Ocorrência inválida.',400)
    with database().begin() as c:
        old=c.execute(text('SELECT * FROM records WHERE id=:id'),{'id':uid}).fetchone()
        result=c.execute(text('DELETE FROM records WHERE id=:id'),{'id':uid})
        if old:  # guarda a cópia completa do registro excluído, para poder recuperar
            log(c,s['username'],'Excluiu ocorrência',rec_label(dict(old._mapping)),json.dumps(dict(old._mapping),ensure_ascii=False,default=str))
    return jsonify({'ok':True,'deleted':result.rowcount})


@app.route('/')
@app.route('/index.html')
def home():return send_from_directory(HERE,'index.html')


@app.route('/<path:filename>')
def files(filename):
    return no_access('Não encontrado.',404)


if __name__=='__main__':
    app.run(debug=False,host='127.0.0.1',port=int(os.environ.get('PORT','8080')))