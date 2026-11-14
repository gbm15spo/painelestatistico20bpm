"""Extração de PDF, validação e serialização."""
import datetime as dt
import json
import re
import unicodedata

COUNT_FIELDS = ('conducoes','flagrantes','tco','mandados','veiculos','armas_brancas','simulacros','adolescentes','conduzidos','presos')
DRUG_UNITS = {'g','kg','porções','unidades','comprimidos','ml','L','pés'}

def normal(s):
    s = unicodedata.normalize('NFKD', s).encode('ascii', 'ignore').decode('ascii')
    return re.sub(r'\s+', ' ', s).strip().upper()


def crime_type(kind):
    cat=normal(kind)
    # Classificar somente por tipo principal, não por menção contextual a homicídio.
    if re.search(r'\bTENTATIVA\s+(?:DE\s+)?HOMICIDIO\b|\bHOMICIDIO\s+TENTADO\b',cat):
        return 'tentativa_homicidio'
    if re.search(r'\bHOMICIDIO\b',cat):
        return 'homicidio'
    return 'outros'


_ROMAN = {'1':'I','2':'II','3':'III','4':'IV','5':'V','6':'VI','7':'VII','8':'VIII','9':'IX','10':'X'}
_SIGLAS = {'BTN','CHESF','BNH','COHAB','URBIS','INOCOOP','PA','BA','KM','BR'}
_MINUSC = {'de','da','do','das','dos','e','em','a','o'}


def norm_bairro(name):
    """Padroniza a escrita do bairro: "Btn Iii", "BTN 3" e "btn  iii" viram "BTN III"; "General Dutra(Chesf)" vira "General Dutra (CHESF)"."""
    s = re.sub(r'\s+', ' ', str(name or '')).strip(' ,.;-')
    if not s: return ''
    s = re.sub(r'\s*\(\s*', ' (', s); s = re.sub(r'\s*\)', ')', s)
    s = re.sub(r'(?i)\b(BTN)[\s-]*(\d{1,2}|[IVX]{1,4})\b', lambda m: m.group(1) + ' ' + m.group(2), s)
    out = []
    for i, w in enumerate(s.split(' ')):
        lead = '(' if w.startswith('(') else ''; tail = ')' if w.endswith(')') else ''
        core_w = w[len(lead):len(w) - len(tail)]
        up = core_w.upper()
        if up in _SIGLAS: x = up
        elif re.fullmatch(r'[IVX]{1,4}', up) and i > 0: x = up
        elif core_w.isdigit() and i > 0 and core_w in _ROMAN and out and out[-1].upper() in _SIGLAS | {'SETOR','SIRIEMA','SERIEMA','PRAINHA','MOXOTO','MOXOTÓ'}: x = _ROMAN[core_w]
        elif core_w.lower() in _MINUSC and i > 0: x = core_w.lower()
        else: x = core_w[:1].upper() + core_w[1:].lower()
        out.append(lead + x + tail)
    return ' '.join(out)[:100]


def bairro_key(city, name):
    """Chave para reconhecer o mesmo bairro escrito com/sem acento ou pontuação."""
    return re.sub(r'[^A-Z0-9]', '', normal(str(city or ''))) + '|' + re.sub(r'[^A-Z0-9]', '', normal(norm_bairro(name)))


def canon_bairros(pairs):
    """Para cada bairro escrito de formas diferentes (ex.: com e sem acento), escolhe uma grafia: a mais acentuada e, no empate, a mais usada."""
    seen = {}
    for city, name in pairs:
        nb = norm_bairro(name)
        if nb: seen.setdefault(bairro_key(city, nb), {}).setdefault(nb, 0); seen[bairro_key(city, nb)][nb] += 1
    return {k: max(v.items(), key=lambda kv: (sum(ord(ch) > 127 for ch in kv[0]), kv[1], kv[0]))[0] for k, v in seen.items()}


SOCIAL_TYPES = r'SUICID|APOIO|AUXILIO|SOCORRO|SURTO|DESAPAREC|ESCOLTA|PESSOA PERDIDA|ALIENAD|TRANSPORTE DE|PARTURIENTE|ANIMAL|OUTRAS OCORRENCIAS NAO RELACIONADAS|ACIDENTE DE VEICULO SEM VITIMA'
GROUPS = ('policial', 'social')


def auto_group(kind):
    """'social' = atendimento social/apoio (não é crime); 'policial' = demais. Com vários TIPOS, basta um policial para ser policial."""
    parts = [x for x in (p.strip() for p in re.split(r',(?![^(]*\))', normal(kind))) if x and not x.startswith('AVERIGUA')]
    if not parts: return 'policial'
    return 'social' if all(re.search(SOCIAL_TYPES, x) for x in parts) else 'policial'


def bairro_from_address(address):
    # Referências comuns: RUA X, CENTRO - ZONA URBANA, PAULO AFONSO/BA
    normal_address=re.sub(r'\s+', ' ',address)
    match=re.search(r',\s*([^,]+?)\s*-\s*ZONA\s+(?:URBANA|RURAL)\b',normal_address,re.I)
    return norm_bairro(match.group(1)) if match else ''


NUM_WORDS = {'UM':1,'UMA':1,'DOIS':2,'DUAS':2,'TRES':3,'QUATRO':4,'CINCO':5,'SEIS':6,'SETE':7,'OITO':8,'NOVE':9,'DEZ':10}
_QTY = r'(?:(?P<n>\d{1,4})\s*(?:\([A-Z ]+\)\s*)?|\(\s*(?P<n2>\d{1,4})\s*\)\s*(?:[A-Z]+\s+)?|(?P<w>UMA?|DOIS|DUAS|TRES|QUATRO|CINCO|SEIS|SETE|OITO|NOVE|DEZ)\s+)?'
_SEIZE = r'APREEN|ARRECAD|RECOLHID|ENCONTRAD|LOCALIZAD|ENTREG|APRESENTA|CONDUZID|ENCAMINHAD'
_NEG = r'\b(?:NAO|SEM|NADA|NENHUM|NENHUMA|TAMPOUCO|NEM|INEXIST\w*|NEGATIV\w*)\b'
_STATION = r'DELEGACIA|DEPOL|\bD\.?P\.?\b|\bDT\b|\bDEAM\b|\bDISEP\b|\bCOORPIN\b|POLICIA CIVIL|POLICIA JUDICIARIA|AUTORIDADE POLICIAL|CENTRAL DE FLAGRANTES|PLANTAO POLICIAL|DELEGAD[OA]'
FIREARMS = [('Revólver', r'REVOLVER(?:ES)?'), ('Pistola', r'PISTOLAS?'), ('Espingarda', r'ESPINGARDAS?|ESCOPETAS?|CARTUCHEIRAS?'), ('Fuzil', r'FUZIL|FUZIS'),
            ('Carabina/Rifle', r'CARABINAS?|RIFLES?'), ('Submetralhadora', r'(?:SUB)?METRALHADORAS?'), ('Garrucha', r'GARRUCHAS?'),
            ('Arma de fabricação caseira', r'ARMAS? (?:DE FOGO )?(?:DE FABRICACAO )?(?:CASEIRA|ARTESANAL)S?'), ('Arma de fogo', r'ARMAS? DE FOGO')]
BLADES = r'ARMAS? BRANCAS?|FAC(?:AO|OES|AS|A)\b(?: (?:DO )?TIPO \w+)?|PEIXEIRAS?|CANIVETES?|PUNHAL|PUNHAIS|ADAGAS?|ESTILETES?|FOICES?|MACHAD(?:O|OS|INHA|INHAS)\b|TERCADOS?'
FAKES = r'(?:SIMULACROS?|REPLICAS?|ARMAS? DE BRINQUEDO|ARMAS? DE AIRSOFT|AIRSOFT)(?: DE (?:ARMAS? DE FOGO|PISTOLAS?|REVOLVER(?:ES)?|FUZIL|FUZIS|ESPINGARDAS?))?'
DRUGS = [('Maconha', r'MACONHA|CANNABIS'), ('Cocaína', r'COCAINA'), ('Crack', r'CRACK'), ('Haxixe', r'HAXIXE'), ('Skunk', r'SKUNK'), ('Ecstasy', r'ECSTASY'), ('LSD', r'\bLSD\b')]
DRUG_UNIT_MAP = [(r'KG|QUILOS?|QUILOGRAMAS?', 'kg'), (r'G|GR|GRAMAS?', 'g'), (r'ML', 'ml'), (r'LITROS?', 'L'), (r'PES', 'pés'), (r'COMPRIMIDOS?', 'comprimidos'),
                 (r'PORCAO|PORCOES|TROUXINHAS?|TROUXAS?|PEDRAS?|PINOS?|PAPELOTES?|BUCHAS?|DOLAS?|PETECAS?|EMBALAGENS?|INVOLUCROS?|SACOLES?', 'porções'),
                 (r'UNIDADES?|TABLETES?|CIGARROS?|BARRAS?|PEDACOS?', 'unidades')]


def _num(m):
    g = m.groupdict()
    if g.get('n') or g.get('n2'):
        return max(1, min(int(g.get('n') or g.get('n2')), 9999))
    return NUM_WORDS.get(g.get('w') or '', 1)


def _negated(text, pos):
    """A menção vem depois de NÃO/SEM/NADA/NENHUM/TAMPOUCO na mesma frase? (ex.: "não havia mandado de prisão")"""
    return bool(re.search(_NEG + r'[^.;]{0,100}$', text[max(0, pos - 115):pos]))


def _neg(text, m):
    """Negação antes ("sem arma de fogo") ou logo depois ("a faca não foi encontrada", "APF: não informado")."""
    return _negated(text, m.start()) or bool(re.match(r'[\s:]*(?:[\w.]+\s+){0,3}NAO\b', text[m.end():]))


def _tag_field(name):
    """Campo estruturado do relatório, ex.: [TCO: 1], [ARMAS BRANCAS: 1] -> nome do indicador."""
    if 'TCO' in name.split() or 'CIRCUNSTANCIAD' in name: return 'tco'
    if re.search(r'\bPRES[OA]S?\b', name): return 'presos'
    if 'FLAGRANTE' in name or 'APF' in name.split(): return 'flagrantes'
    if 'MANDADO' in name: return 'mandados'
    if 'VEICULO' in name and 'RECUPER' in name: return 'veiculos'
    if 'SIMULACRO' in name: return 'simulacros'
    if 'BRANCA' in name: return 'armas_brancas'
    if 'ARMA' in name and 'FOGO' in name: return 'guns'
    if ('ADOLESCENTE' in name or 'MENOR' in name) and 'APREEN' in name: return 'adolescentes'
    if 'CONDUZID' in name: return 'conduzidos'
    if 'CONDUC' in name: return 'conducoes'
    if re.search(r'\bPRES[OA]S?\b|PRISAO|PRISOES', name): return 'presos'
    if 'DROGA' in name or 'ENTORPECENTE' in name: return 'drugs'
    return None


def analyze_measures(kind, measures):
    """Procura os indicadores no TIPO e no bloco MEDIDAS ADOTADAS PELA GUARNIÇÃO (texto + campos que o seguem:
    DOCUMENTOS GERADOS, DROGAS APREENDIDAS, [TCO: 1], ENTRADA NA DP...). Resultado é uma PRÉVIA para conferência humana."""
    tipo = normal(kind)
    raw = unicodedata.normalize('NFKD', measures).encode('ascii', 'ignore').decode('ascii').upper()
    raw = re.sub(r'\((?:S|ES|IS)\)', '', raw)                      # TROUXINHA(S) -> TROUXINHA
    raw = re.sub(r'USUARI[OA]S? DE (?:DROGAS?|ENTORPECENTES?)', ' ', raw)
    # campos estruturados
    tags, docs = {}, set()
    for m in re.finditer(r'\[\s*N\S*\s+DOC\.?\s+([A-Z ]+?)\s*:\s*([^\]]*)\]', raw):
        if re.search(r'\d', m.group(2)): docs.add(m.group(1).strip())
    for m in re.finditer(r'\[\s*([A-Z][A-Z /.]{1,40}?)\s*:\s*(\d{1,4})\s*\]', raw):
        tags[m.group(1).strip()] = tags.get(m.group(1).strip(), 0) + int(m.group(2))
    in_dp = bool(re.search(r'ENTRADA NA DP\s*:\s*\d', raw))   # só a SAÍDA preenchida não comprova condução
    body = re.sub(r'\[[^\]]*\]', '\n', raw)
    body = re.sub(r'(?:ENTRADA|SAIDA|PERMANENCIA) NA DP\s*:\s*[\d:H]*|SAIDA DA DP\s*:\s*[\d:H]*', '\n', body)
    # texto corrido (linhas quebradas pelo PDF são emendadas) + campos finais, que são lidos linha a linha
    cut = re.search(r'DOCUMENTOS GERADOS|\n[A-Z ]{4,40}(?:APREENDID[OA]S|RECUPERAD[OA]S)\s*:\s*\n', body)
    narrative, tail = (body[:cut.start()], body[cut.start():]) if cut else (body, '')
    parts = re.split(r'[.;]\s+|\s+-\s+|\*|\n\s*\n', narrative) + tail.split('\n')
    sentences = [re.sub(r'\s+', ' ', x).strip(' *-') for x in parts]
    sentences = [x for x in sentences if x]
    both = [tipo] + sentences
    w = []
    r = {k: 0 for k in COUNT_FIELDS}
    r['drugs'], r['guns'] = [], []

    def hit(pattern, texts=both):
        for t in texts:
            for m in re.finditer(pattern, t):
                if not _neg(t, m):
                    return True
        return False

    # Condução à delegacia
    if in_dp or hit(r'(?:CONDU[CZ]\w+|ENCAMINHA\w+|APRESENTA\w+|LEVAD[OA]S?|ENTREGUES?)[^.;]{0,140}(?:' + _STATION + r')|(?:' + _STATION + r')[^.;]{0,80}(?:CONDUZID|ENCAMINHAD|APRESENTAD)', sentences) \
            or hit(r'CONDUCAO (?:A|PARA A|ATE A) (?:' + _STATION + ')', [tipo]):
        r['conducoes'] = 1
    # Prisão em flagrante
    if any('APF' in d.split() or 'FLAGRANTE' in d for d in docs) or hit(r'PRES[OA]S? EM FLAGRANTE|PRISAO EM FLAGRANTE|PRISOES EM FLAGRANTE|AUTUAD[OA]S? EM FLAGRANTE|AUTO DE PRISAO EM FLAGRANTE|FLAGRANTEAD[OA]S?|VOZ DE PRISAO|(?:LAVR\w+|CONFECC\w+|REGISTR\w+|FORMALIZ\w+|RATIFIC\w+)[^.;]{0,50}\b(?:APF|FLAGRANTE)\b|\bAPF\b[^.;]{0,50}(?:LAVRAD|CONFECCIONAD|REGISTRAD|FORMALIZAD|RATIFICAD)'):
        r['flagrantes'] = 1
    elif hit(r'\bAPF\b|FLAGRANTE'):
        w.append('Há menção a flagrante/APF, mas sem confirmar a prisão. Confira o campo Flagrantes.')
    # TCO
    if any('TCO' in d.split() or 'CIRCUNSTANCIAD' in d for d in docs) or hit(r'TERMO CIRCUNSTANCIA\w*|\bT\.?C\.?O\.?\b'):
        r['tco'] = 1
    # Cumprimento de mandado de prisão
    if hit(r'(?:CUMPRI\w+|EXECU\w+)[^.;]{0,60}MANDADOS?[^.;]{0,25}PRISAO|MANDADOS?(?: JUDICIA(?:L|IS))? DE PRISAO[^.;]{0,80}(?:CUMPRID|EXECUTAD|EM ABERTO|EM DESFAVOR|EXPEDID)|(?:EM ABERTO|EM DESFAVOR)[^.;]{0,60}MANDADOS? DE PRISAO|MANDADO EM ABERTO'):
        r['mandados'] = 1
    elif hit(r'MANDADOS?[^.;]{0,25}PRISAO'):
        w.append('Há menção a mandado de prisão, mas sem confirmar o cumprimento. Confira o campo Mandados.')
    # Veículos recuperados
    veh = r'VEICULOS?|MOTOCICLETAS?|MOTOS?\b|MOTONETAS?|AUTOMOVE(?:L|IS)|CARROS?|CAMINHONETES?|CAMINHAO|CAMINHOES|CAMIONETES?'
    n = 0
    for t in both:
        for m in re.finditer(_QTY + r'(?:' + veh + r')[^.;]{0,90}(?:RECUPERAD|RESTITUID|DEVOLVID\w+ AO PROPRIETARIO|COM RESTRICAO DE (?:ROUBO|FURTO)|COM QUEIXA DE (?:ROUBO|FURTO)|PRODUTO DE (?:ROUBO|FURTO)|ROUBAD[OA]S? (?:FOI|FORAM) LOCALIZAD)|(?:RECUPERACAO|RECUPERAD[OA]S?|RESTITUICAO) (?:D[EO]S? )?' + _QTY.replace('<n>', '<n3>').replace('<n2>', '<n4>').replace('<w>', '<w2>') + r'(?:' + veh + r')', t):
            if _neg(t, m): continue
            g = m.groupdict()
            n += _num(m) if (g.get('n') or g.get('n2') or g.get('w')) else (max(1, int(g['n3'] or g['n4'])) if (g.get('n3') or g.get('n4')) else NUM_WORDS.get(g.get('w2') or '', 1))
    r['veiculos'] = min(n, 99)
    if not n and re.search(r'DETRAN|GUINCH|REMOVID[OA] AO PATIO', raw):
        w.append('Veículo removido/recolhido não foi contado como recuperado.')

    # Apreensões: só contam em frases com verbo de apreensão, ou em listas ("MATERIAL RECOLHIDO:", "DROGAS APREENDIDAS:").
    # TIPO como "tráfico" ou "porte" sozinho gera só aviso.
    found = {'fake': [], 'blade': [], 'gun': []}      # (rótulo, quantidade, quantidade explícita?)
    drugs, loose = {}, set()
    in_list = False
    for t in both:
        ok = bool(re.search(_SEIZE + r'|RETIRAD', t)) or in_list or (t is tipo and 'APREENS' in t)
        if t is not tipo and re.search(r'(?:APREENDID[OA]S?|RECOLHID[OA]S?|RECUPERAD[OA]S?|ARRECADAD[OA]S?)\s*:?$|^(?:MATERIA(?:L|IS)|OBJETOS?|DROGAS?|ARMAS?)\b[^.]{0,40}:$', t):
            in_list = True
        rest = t
        for m in re.finditer(_QTY + r'(?:' + FAKES + r')', t):
            if _neg(t, m): continue
            if ok: found['fake'].append(('simulacro', _num(m), bool(m.group('n') or m.group('n2') or m.group('w'))))
            else: loose.add('simulacro')
        rest = re.sub(FAKES, ' ', rest)
        specific = False
        for m in re.finditer(_QTY + r'(?:' + BLADES + r')', rest):
            if _neg(rest, m): continue
            label = re.sub(r'^(?:\d+\s*|\(.*?\)\s*)*', '', m.group(0)).split(' TIPO')[0].split(' DO TIPO')[0].strip()
            label = 'ARMA BRANCA' if label.startswith('ARMA') else label[:4]
            if ok: found['blade'].append((label, _num(m), bool(m.group('n') or m.group('n2') or m.group('w'))))
            else: loose.add('arma branca')
        taken = []
        for label, pat in FIREARMS:
            for m in re.finditer(_QTY + r'(?:' + pat + r')\b', rest):
                if any(x < m.end() and m.start() < y for x, y in taken) or _neg(rest, m): continue
                taken.append((m.start(), m.end()))
                if t is tipo and not ok: loose.add('arma de fogo (no TIPO)'); continue
                if ok: found['gun'].append((label, _num(m), bool(m.group('n') or m.group('n2') or m.group('w'))))
                else: loose.add('arma de fogo')
        for label, pat in DRUGS + [('Não especificada', r'ENTORPECENTES?|DROGAS?\b|SUBSTANCIAS? (?:ILICITAS?|ENTORPECENTES?)')]:
            for m in re.finditer(pat, rest):
                if _neg(rest, m): continue
                if label == 'Não especificada' and (t is tipo or re.search(r'^DROGAS? APREENDID', rest)): continue
                if not ok: loose.add('droga'); continue
                q = None
                back = rest[max(0, m.start() - 90):m.start()]
                units = '|'.join(u for u, _ in DRUG_UNIT_MAP)
                qm = None
                for qm in re.finditer(r'(?:(\d{1,6}(?:[.,]\d{1,3})?)|\b(UMA?|DOIS|DUAS|TRES|QUATRO|CINCO|SEIS|SETE|OITO|NOVE|DEZ))\s*(?:\([A-Z ]+\)\s*)?(' + units + r')\b', back): pass
                if qm:
                    unit = next(u for pat_u, u in DRUG_UNIT_MAP if re.fullmatch(pat_u, qm.group(3)))
                    try: q = ((float(qm.group(1).replace('.', '').replace(',', '.')) if ',' in qm.group(1) else float(qm.group(1))) if qm.group(1) else float(NUM_WORDS[qm.group(2)]), unit)
                    except ValueError: q = None
                drugs.setdefault(label, [])
                if q and q[0] > 0 and q not in drugs[label]: drugs[label].append(q)   # mesma apreensão citada duas vezes conta uma vez
    if len(drugs) > 1: drugs.pop('Não especificada', None)

    def total(items):
        # Havendo quantidade escrita ("01 (uma) faca"), vale a soma dessas; senão, 1 por objeto distinto citado.
        items = [x for x in items if not (x[0] == 'ARMA BRANCA' and any(y[0] != 'ARMA BRANCA' for y in items))]
        exp = {}
        for label, q, e in items:
            if e: exp[label] = max(exp.get(label, 0), q) if False else exp.get(label, 0) + q
        return sum(exp.values()) if exp else len({x[0] for x in items})

    r['simulacros'], r['armas_brancas'] = min(total(found['fake']), 999), min(total(found['blade']), 999)
    guns = {}
    for label in {x[0] for x in found['gun']}:
        guns[label] = total([x for x in found['gun'] if x[0] == label])
    if len(guns) > 1: guns.pop('Arma de fogo', None)
    r['guns'] = [{'type': k, 'qty': v} for k, v in guns.items() if v]
    for label, qs in drugs.items():
        if qs:
            agg = {}
            for q, u in qs: agg[u] = agg.get(u, 0) + q
            for u, q in agg.items(): r['drugs'].append({'type': label, 'qty': round(q, 3), 'unit': u})
        else:
            r['drugs'].append({'type': label, 'qty': None, 'unit': 'g'})
            w.append(f'Droga apreendida ({label}): quantidade não identificada — informe antes de salvar.')

    # Adolescentes apreendidos
    teen = r'ADOLESCENTES?|MENOR(?:ES)?(?: DE IDADE| INFRATOR(?:ES)?)?\b'
    n = 0
    for t in both:
        for m in re.finditer(_QTY + r'(?:' + teen + r')', t):
            if _neg(t, m): continue
            ctx = t[max(0, m.start() - 90):m.end() + 110]
            if re.search(r'APREEN', ctx) or (re.search(r'CONDUZID|ENCAMINHAD|APRESENTAD', ctx) and re.search(_STATION, ctx) and not re.search(r'VITIMA', ctx)):
                n += _num(m); break
    r['adolescentes'] = min(n, 99)
    if not n and re.search(r'ATO INFRACIONAL|APREENSAO DE (?:ADOLESCENTE|MENOR)', tipo):
        w.append('O TIPO indica ato infracional/adolescente, mas a apreensão não está clara nas medidas. Confira.')

    # Campos estruturados do relatório ([TCO: 1], [ARMAS BRANCAS: 1]...) valem mais que o texto
    for name, val in tags.items():
        f = _tag_field(name)
        if f is None:
            w.append(f'Campo [{name.title()}: {val}] não reconhecido pelo leitor. Confira.')
        elif f == 'guns':
            if sum(g['qty'] for g in r['guns']) != val:
                r['guns'] = [{'type': 'Arma de fogo', 'qty': val}] if val else []
            loose -= {'arma de fogo', 'arma de fogo (no TIPO)'}
        elif f == 'drugs':
            loose.discard('droga')
        else:
            r[f] = val
            if f == 'conduzidos' and val: r['conducoes'] = max(r['conducoes'], 1)
            if f == 'presos' and val: r['flagrantes'] = max(r['flagrantes'], 1)
            loose -= {'arma branca'} if f == 'armas_brancas' else {'simulacro'} if f == 'simulacros' else set()
    if r['guns']: w.append('Arma(s) de fogo identificada(s): confira tipo e quantidade.'); loose -= {'arma de fogo (no TIPO)'}
    if r['drugs']: w.append('Droga(s) identificada(s): confira tipo, quantidade e unidade.'); loose.discard('droga')
    if r['armas_brancas']: loose.discard('arma branca')
    for x in sorted(loose):
        w.append(f'Há menção a {x} sem indicação clara de apreensão: não foi contada. Confira.')
    r['warnings'] = w
    return r


def pdf_preview(data: bytes):
    """Conservative parsing of standardized PMBA report; NEVER auto-saves entries."""
    try:
        import fitz
    except ImportError as e:
        raise ValueError('Biblioteca PyMuPDF não instalada (pip install -r requirements.txt).') from e
    try:
        doc = fitz.open(stream=data, filetype='pdf')
        if len(doc) > 100:
            raise ValueError('PDF muito extenso (máximo de 100 páginas).')
        text = '\n'.join(page.get_text(sort=True) for page in doc)
        doc.close()
    except ValueError:
        raise
    except Exception as e:
        raise ValueError('Não foi possível ler este PDF.') from e
    if len(text.strip()) < 100:
        raise ValueError('PDF sem texto selecionável. PDFs digitalizados precisam de OCR; cadastre manualmente.')

    regex = r'TIPO\(S\)\s*:\s*(.*?)\s*\|\s*N[º°o\.\s]*DA OCORR[ÊE]NCIA\s*:\s*(\d+)'
    matches = list(re.finditer(regex, text, re.I | re.S))
    if not matches:
        raise ValueError('Não foram encontradas ocorrências no padrão “TIPO(S) ... Nº DA OCORRÊNCIA”. Revise o PDF ou registre manualmente.')
    rows = []
    for idx, m in enumerate(matches[:100]):
        piece = text[m.end():matches[idx+1].start() if idx+1 < len(matches) else len(text)]
        # Limit to occurrence details: subsequent statistical/report appendices must not be parsed.
        piece = re.split(r'\bPRODUTIVIDADE\b|\bORDENS DE POLICIAMENTO\b|\bINFORMA[ÇC][ÕO]ES ADICIONAIS\b', piece, maxsplit=1, flags=re.I)[0]
        header = piece[:1300]
        date_match = re.search(r'DATA\s*:\s*(\d{2}/\d{2}/\d{4})\s*,?\s*(\d{1,2}:\d{2})?', header, re.I)
        if not date_match:
            continue
        try:
            day = dt.datetime.strptime(date_match.group(1), '%d/%m/%Y').date().isoformat()
        except ValueError:
            continue
        resp = re.search(r'RESPONS[ÁA]VEL\s*:\s*([^\n]+)', header, re.I)
        responsible = resp.group(1).strip() if resp else ''
        cia_match = re.search(r'\b(\d{1,2})\s*[ªa°]\s*CIA\b', responsible, re.I)
        if cia_match:
            cia = cia_match.group(1) + 'ª CIA'
        elif re.search(r'\bCETO\b', responsible, re.I):
            cia = 'CETO'  # ex.: "CETO/3º PEL MOTOPATRULHAMENTO"
        else:
            cia = 'Não informada (avulsa)'
        warnings = []
        if cia == 'Não informada (avulsa)':
            if 'MOTOPATRULHAMENTO' in normal(piece) or 'MOTO PATRULHAMENTO' in normal(piece):
                warnings.append('A ocorrência é avulsa, mas o texto menciona motopatrulhamento (possível CETO). Confirme a CIA.')
            else:
                warnings.append('CIA não identificada no campo responsável. Confirme antes de salvar.')
        # ENDEREÇO may break into multiple lines, but should stop at next section field.
        address_match = re.search(r'ENDERE[ÇC]O\s*:\s*(.*?)(?=\n\s*(?:SOLICITANTE|SUSPEITO|AUTOR|V[IÍ]TIMA|OBJETOS RELACIONADOS|DESCRI[ÇC][ÃA]O|DIN[ÂA]MICA|\[N[º°o]|POLICIAIS QUE|TIPO\(S\))\s*:|\n\n)', header, re.I | re.S)
        address = re.sub(r'\s+', ' ', address_match.group(1)).strip(' ,') if address_match else ''
        # City from full address or service header when present.
        city_found = re.search(r'([A-ZÀ-Ú][A-ZÀ-Ú\s.-]{2,45})/\s*BA\b', address, re.I)
        city = city_found.group(1).split(',')[-1].strip(' ,') .title() if city_found else ''
        if not city:
            # fallback only if recognized in page; must be reviewed
            if 'PAULO AFONSO/BA' in normal(piece[:1700]):
                city = 'Paulo Afonso'
            else:
                warnings.append('Cidade não identificada automaticamente.')
        # Indicadores: lidos SOMENTE do TIPO e da seção MEDIDAS ADOTADAS PELA GUARNIÇÃO.
        # Bloco: do ÚLTIMO título "MEDIDAS ADOTADAS PELA GUARNIÇÃO" até os policiais que atenderam. Inclui os campos
        # que o relatório põe em seguida (DOCUMENTOS GERADOS, DROGAS APREENDIDAS, [TCO: 1], ENTRADA NA DP).
        heads = list(re.finditer(r'MEDIDAS\s+ADOTADAS\s+PELA\s+GUARNI[ÇC][ÃA]O\s*:', piece, re.I)) or list(re.finditer(r'MEDIDAS\s+ADOTADAS\s*:', piece, re.I))
        measures = ''
        if heads:
            measures = re.split(r'POLICIAIS\s+QUE\s+ATENDERAM|\n\s*(?:DESCRI[ÇC][ÃA]O|DIN[ÂA]MICA\s+DOS\s+FATOS)\s*:', piece[heads[-1].end():], maxsplit=1, flags=re.I)[0]
            measures = re.sub(r'\n[ \t]{40,}\d{1,3}[ \t]*(?=\n|$)', '\n', measures)   # número de página solto
        if not measures.strip():
            warnings.append('Seção "MEDIDAS ADOTADAS PELA GUARNIÇÃO" não encontrada ou vazia: indicadores lidos só do TIPO.')
        found = analyze_measures(m.group(1), measures)
        warnings.extend(found.pop('warnings'))
        # Pessoas: conta os AUTORES qualificados na ocorrência (o relatório lista um por linha)
        authors = len(re.findall(r'(?m)^\s*\d{0,3}\s*AUTOR(?:A|ES)?\s*:', piece[:heads[-1].start()] if heads else piece)) or 1
        if found['conducoes'] and not found['conduzidos']:
            found['conduzidos'] = authors
            warnings.append(f'Pessoas conduzidas: {authors} (pelos autores listados). Confira se vítimas ou outros também foram conduzidos.')
        if found['flagrantes'] and not found['presos']:
            found['presos'] = authors
            warnings.append(f'Pessoas presas em flagrante: {authors} (pelos autores listados). Confira.')
        warnings.append('Conferência humana obrigatória: verifique indicadores, local e CIA.')
        rows.append({
            'date': day, 'time': date_match.group(2) or '', 'cia': cia, 'city': city,
            'location': address[:350], 'neighborhood': bairro_from_address(address), 'ref': m.group(2), 'kind': re.sub(r'\s+',' ',m.group(1)).strip()[:120], 'crime_category': crime_type(m.group(1)),
            **found, 'grupo': auto_group(m.group(1)),
            'notes': '', 'source': 'PDF (revisado pelo administrador)', 'warnings': warnings
        })
    if not rows:
        raise ValueError('O relatório foi lido, mas nenhuma ocorrência pôde ser identificada com data válida.')
    return {'records': rows, 'count': len(rows), 'text_pages': len(text.split('\f')),
            'notice': 'Prévia automática: nenhuma ocorrência foi gravada. Revise e confirme cada item.'}


def clean_text(value, max_len, field, required=False):
    x = str(value if value is not None else '').strip()
    if len(x) > max_len or '\x00' in x:
        raise ValueError(f'Campo {field}: texto inválido ou muito longo.')
    if required and not x:
        raise ValueError(f'Informe {field}.')
    return x


def validate_record(d):
    if not isinstance(d, dict):
        raise ValueError('Registro inválido.')
    date = clean_text(d.get('date'), 10, 'data', True)
    try:
        if dt.date.fromisoformat(date).isoformat() != date:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError('Data inválida (AAAA-MM-DD).')
    t = clean_text(d.get('time'), 5, 'horário')
    if t and not re.fullmatch(r'(?:[01]\d|2[0-3]):[0-5]\d', t):
        raise ValueError('Horário inválido (HH:MM).')
    r = {'date': date, 'time': t}
    for k, mx, required in [('cia',90,True),('city',100,True),('neighborhood',100,False),('location',350,False),('ref',90,False),('kind',120,False),('notes',750,False),('source',120,False)]:
        r[k] = clean_text(d.get(k), mx, k, required)
    r['neighborhood'] = norm_bairro(r['neighborhood'])
    grupo = clean_text(d.get('grupo'), 20, 'grupo')
    r['grupo'] = grupo if grupo in GROUPS else auto_group(r['kind'])
    # Coordenada exata da ocorrência (opcional; visível só ao administrador)
    lat, lon = d.get('lat'), d.get('lon')
    if lat in (None, '') and lon in (None, ''):
        r['lat'] = r['lon'] = None
    else:
        try:
            lat, lon = float(lat), float(lon)
        except (TypeError, ValueError):
            raise ValueError('Coordenada inválida.')
        if isinstance(d.get('lat'), bool) or not (-90 <= lat <= 90 and -180 <= lon <= 180):
            raise ValueError('Coordenada inválida.')
        r['lat'], r['lon'] = round(lat, 6), round(lon, 6)
    cls=clean_text(d.get('crime_category',crime_type(r['kind'])),30,'classificação') or 'outros'
    if cls not in ('outros','homicidio','tentativa_homicidio'):raise ValueError('Classificação de crime inválida.')
    r['crime_category']=cls
    for k in COUNT_FIELDS:
        n = d.get(k, 0)
        if isinstance(n, bool): raise ValueError('Indicadores devem ser inteiros.')
        try: ni = int(n)
        except (TypeError, ValueError): raise ValueError('Indicador '+k+' inválido.')
        if str(n) not in (str(ni),) and not isinstance(n, int): raise ValueError('Indicador '+k+' deve ser inteiro.')
        if ni < 0 or ni > 100000: raise ValueError('Indicador '+k+' fora do limite.')
        r[k] = ni
    # Pessoas: se há condução/flagrante, há ao menos 1 pessoa
    if r['conducoes'] and not r['conduzidos']: r['conduzidos'] = r['conducoes']
    if r['flagrantes'] and not r['presos']: r['presos'] = r['flagrantes']
    for field in ('drugs','guns'):
        items = d.get(field, [])
        if not isinstance(items,list) or len(items)>60: raise ValueError('Lista de apreensões inválida.')
        ok = []
        for item in items:
            if not isinstance(item,dict): raise ValueError('Item de apreensão inválido.')
            typ = clean_text(item.get('type'),100,'tipo de apreensão',True)
            raw_qty = item.get('qty')
            try: qty = float(raw_qty)
            except (TypeError,ValueError): raise ValueError('Quantidade de apreensão inválida.')
            if not (0 < qty < 100000000): raise ValueError('Quantidade deve ser positiva.')
            if field=='guns':
                if not qty.is_integer(): raise ValueError('Quantidade de armas deve ser inteira.')
                ok.append({'type':typ, 'qty':int(qty)})
            else:
                unit=clean_text(item.get('unit'),20,'unidade') or 'g'
                if unit not in DRUG_UNITS: raise ValueError('Unidade de droga inválida.')
                ok.append({'type':typ,'qty':qty,'unit':unit})
        r[field] = ok
    return r


def record_from_row(row, private=False):
    d = dict(row)
    d['drugs'] = json.loads(d.pop('drugs'))
    d['guns'] = json.loads(d.pop('guns'))
    d['neighborhood'] = norm_bairro(d.get('neighborhood'))
    d['grupo'] = d.get('grupo') if d.get('grupo') in GROUPS else auto_group(d.get('kind', ''))
    if not private:
        for k in ('location','ref','notes','source','created_at','updated_at','lat','lon','created_by','updated_by'):
            d.pop(k, None)
    return d
