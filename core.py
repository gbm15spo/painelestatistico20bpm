"""Extração de PDF, validação e serialização."""
import datetime as dt
import json
import re
import unicodedata

COUNT_FIELDS = ('conducoes','flagrantes','tco','mandados','veiculos','armas_brancas','simulacros','adolescentes')
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


def bairro_from_address(address):
    # Referências comuns: RUA X, CENTRO - ZONA URBANA, PAULO AFONSO/BA
    normal_address=re.sub(r'\s+', ' ',address)
    match=re.search(r',\s*([^,]+?)\s*-\s*ZONA\s+(?:URBANA|RURAL)\b',normal_address,re.I)
    return match.group(1).strip().title() if match else ''


NUM_WORDS = {'UM':1,'UMA':1,'DOIS':2,'DUAS':2,'TRES':3,'QUATRO':4,'CINCO':5,'SEIS':6,'SETE':7,'OITO':8,'NOVE':9,'DEZ':10}
_QTY = r'(?:(?P<n>\d{1,4})\s*(?:\([A-Z ]+\)\s*)?|\(\s*(?P<n2>\d{1,4})\s*\)\s*(?:[A-Z]+\s+)?|(?P<w>UMA?|DOIS|DUAS|TRES|QUATRO|CINCO|SEIS|SETE|OITO|NOVE|DEZ)\s+)?'
_SEIZE = r'APREEN|ARRECAD|RECOLHID|ENCONTRAD|LOCALIZAD|ENTREG|APRESENTA|CONDUZID|ENCAMINHAD'
_NEG = r'\b(?:NAO|SEM|NADA|NENHUM|NENHUMA|INEXIST\w*|NEGATIV\w*)\b'
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
    """A menção vem logo depois de NÃO/SEM/NADA/NENHUM na mesma frase? (ex.: "não havia mandado de prisão")"""
    return bool(re.search(_NEG + r'[^.;]{0,45}$', text[max(0, pos - 60):pos]))


def _neg(text, m):
    """Negação antes ("sem arma de fogo") ou logo depois ("a faca não foi encontrada")."""
    return _negated(text, m.start()) or bool(re.match(r'\s*(?:[\w.]+\s+){0,3}NAO\b', text[m.end():]))


def analyze_measures(kind, measures):
    """Procura os indicadores no TIPO e nas MEDIDAS ADOTADAS PELA GUARNIÇÃO. Resultado é uma PRÉVIA para conferência humana."""
    tipo, med = normal(kind), normal(measures)
    sentences = [x.strip() for x in re.split(r'[.;]\s+|\s+-\s+|\n', med) if x.strip()]
    both = [tipo] + sentences
    w = []
    r = {k: 0 for k in COUNT_FIELDS}
    r['drugs'], r['guns'] = [], []

    def hit(pattern, texts=both):
        for t in texts:
            for m in re.finditer(pattern, t):
                if not _negated(t, m.start()):
                    return True
        return False

    # Condução à delegacia
    if hit(r'(?:CONDU[CZ]\w+|ENCAMINHA\w+|APRESENTA\w+|LEVAD[OA]S?|ENTREGUES?)[^.;]{0,140}(?:' + _STATION + r')|(?:' + _STATION + r')[^.;]{0,80}(?:CONDUZID|ENCAMINHAD|APRESENTAD)', sentences) \
            or hit(r'CONDUCAO (?:A|PARA A|ATE A) (?:' + _STATION + ')', [tipo]):
        r['conducoes'] = 1
    # Prisão em flagrante
    if hit(r'PRES[OA]S? EM FLAGRANTE|PRISAO(?:OES)? EM FLAGRANTE|PRISOES EM FLAGRANTE|AUTUAD[OA]S? EM FLAGRANTE|AUTO DE PRISAO EM FLAGRANTE|FLAGRANTEAD[OA]S?|FLAGRANTE DELITO|VOZ DE PRISAO|(?:LAVR\w+|CONFECC\w+|REGISTR\w+|FORMALIZ\w+|RATIFIC\w+)[^.;]{0,50}\b(?:APF|FLAGRANTE)\b|\bAPF\b[^.;]{0,50}(?:LAVRAD|CONFECCIONAD|REGISTRAD|FORMALIZAD|RATIFICAD)'):
        r['flagrantes'] = 1
    elif re.search(r'\bAPF\b|FLAGRANTE', tipo + ' ' + med):
        w.append('Há menção a flagrante/APF, mas sem confirmar a prisão. Confira o campo Flagrantes.')
    # TCO
    if hit(r'TERMO CIRCUNSTANCIA\w*|\bT\.?C\.?O\.?\b'):
        r['tco'] = 1
    # Cumprimento de mandado de prisão
    if hit(r'(?:CUMPRI\w+|EXECU\w+)[^.;]{0,60}MANDADOS?[^.;]{0,25}PRISAO|MANDADOS?(?: JUDICIA(?:L|IS))? DE PRISAO[^.;]{0,80}(?:CUMPRID|EXECUTAD|EM ABERTO|EM DESFAVOR|EXPEDID)|(?:EM ABERTO|EM DESFAVOR)[^.;]{0,60}MANDADOS? DE PRISAO'):
        r['mandados'] = 1
    elif hit(r'MANDADOS?[^.;]{0,25}PRISAO'):
        w.append('Há menção a mandado de prisão, mas sem confirmar o cumprimento. Confira o campo Mandados.')
    # Veículos recuperados
    veh = r'VEICULOS?|MOTOCICLETAS?|MOTOS?\b|MOTONETAS?|AUTOMOVE(?:L|IS)|CARROS?|CAMINHONETES?|CAMINHAO|CAMINHOES|CAMIONETES?'
    n = 0
    for t in both:
        for m in re.finditer(_QTY + r'(?:' + veh + r')[^.;]{0,90}(?:RECUPERAD|RESTITUID|DEVOLVID\w+ AO PROPRIETARIO|COM RESTRICAO DE (?:ROUBO|FURTO)|COM QUEIXA DE (?:ROUBO|FURTO)|PRODUTO DE (?:ROUBO|FURTO)|ROUBAD[OA]S? (?:FOI|FORAM) LOCALIZAD)|(?:RECUPERACAO|RECUPERAD[OA]S?|RESTITUICAO) (?:D[EO]S? )?' + _QTY.replace('<n>', '<n3>').replace('<n2>', '<n4>').replace('<w>', '<w2>') + r'(?:' + veh + r')', t):
            if _negated(t, m.start()): continue
            g = m.groupdict()
            n += _num(m) if (g.get('n') or g.get('n2') or g.get('w')) else (max(1, int(g['n3'] or g['n4'])) if (g.get('n3') or g.get('n4')) else NUM_WORDS.get(g.get('w2') or '', 1))
    r['veiculos'] = min(n, 99)
    if not n and re.search(r'DETRAN|GUINCH|REMOVID[OA] AO PATIO', med):
        w.append('Veículo removido/recolhido não foi contado como recuperado.')

    # Apreensões: só contam em frases com verbo de apreensão. TIPO como "tráfico" ou "porte" sozinho gera só aviso.
    def seizing(t):
        return bool(re.search(_SEIZE, t)) or (t is tipo and 'APREENS' in t)

    fakes = blades = 0
    guns = {}
    drugs = {}
    loose = set()
    for t in both:
        if not t: continue
        ok = seizing(t)
        # simulacros primeiro, para "simulacro de pistola" não virar arma de fogo
        rest = t
        for m in list(re.finditer(_QTY + r'(?:' + FAKES + r')', t)):
            if _neg(t, m): continue
            if ok: fakes += _num(m)
            else: loose.add('simulacro')
        rest = re.sub(FAKES, ' ', rest)
        for m in re.finditer(_QTY + r'(?:' + BLADES + r')', rest):
            if _neg(rest, m): continue
            if ok: blades += _num(m)
            else: loose.add('arma branca')
        taken = []
        for label, pat in FIREARMS:
            for m in re.finditer(_QTY + r'(?:' + pat + r')\b', rest):
                if any(a < m.end() and m.start() < b for a, b in taken) or _neg(rest, m): continue
                taken.append((m.start(), m.end()))
                if t is tipo and label == 'Arma de fogo': loose.add('arma de fogo (no TIPO)'); continue
                if ok: guns[label] = guns.get(label, 0) + _num(m)
                else: loose.add('arma de fogo')
        for label, pat in DRUGS + [('Não especificada', r'ENTORPECENTES?|DROGAS?\b|SUBSTANCIAS? (?:ILICITAS?|ENTORPECENTES?|ANALOGAS?)(?! A)')]:
            for m in re.finditer(pat, rest):
                if _neg(rest, m): continue
                if label == 'Não especificada' and (drugs or t is tipo and not ok):
                    continue
                if not ok: loose.add('droga'); continue
                q = None
                back = rest[max(0, m.start() - 90):m.start()]
                units = '|'.join(u for u, _ in DRUG_UNIT_MAP)
                qm = None
                for qm in re.finditer(r'(\d{1,6}(?:[.,]\d{1,3})?)\s*(?:\([A-Z ]+\)\s*)?(' + units + r')\b', back): pass
                if qm:
                    unit = next(u for pat_u, u in DRUG_UNIT_MAP if re.fullmatch(pat_u, qm.group(2)))
                    try: q = (float(qm.group(1).replace('.', '').replace(',', '.')) if ',' in qm.group(1) else float(qm.group(1)), unit)
                    except ValueError: q = None
                drugs.setdefault(label, [])
                if q and q[0] > 0: drugs[label].append(q)
    if drugs.get('Não especificada') is not None and len(drugs) > 1:
        drugs.pop('Não especificada')
    for label, qs in drugs.items():
        if qs:
            agg = {}
            for q, u in qs: agg[u] = agg.get(u, 0) + q
            for u, q in agg.items(): r['drugs'].append({'type': label, 'qty': round(q, 3), 'unit': u})
        else:
            r['drugs'].append({'type': label, 'qty': None, 'unit': 'g'})
            w.append(f'Droga apreendida ({label}): quantidade não identificada — informe antes de salvar.')
    r['guns'] = [{'type': k, 'qty': v} for k, v in guns.items()]
    r['armas_brancas'], r['simulacros'] = min(blades, 999), min(fakes, 999)
    if r['guns']: w.append('Arma(s) de fogo identificada(s): confira tipo e quantidade.')
    if r['drugs']: w.append('Droga(s) identificada(s): confira tipo, quantidade e unidade.')
    if r['guns']: loose.discard('arma de fogo (no TIPO)')
    for x in sorted(loose):
        w.append(f'Há menção a {x} sem indicação clara de apreensão: não foi contada. Confira.')

    # Adolescentes apreendidos
    teen = r'ADOLESCENTES?|MENOR(?:ES)?(?: DE IDADE| INFRATOR(?:ES)?)?\b'
    n = 0
    for t in both:
        for m in re.finditer(_QTY + r'(?:' + teen + r')', t):
            if _negated(t, m.start()): continue
            ctx = t[max(0, m.start() - 90):m.end() + 110]
            if re.search(r'APREEN', ctx) or (re.search(r'CONDUZID|ENCAMINHAD|APRESENTAD', ctx) and re.search(_STATION + r'|CONSELHO TUTELAR|MINISTERIO PUBLICO', ctx) and not re.search(r'VITIMA', ctx)):
                n += _num(m); break
        else:
            continue
    if not n and re.search(teen, tipo + ' ' + med):
        w.append('Há menção a adolescente/menor sem indicação clara de apreensão: não foi contado. Confira.')
    r['adolescentes'] = min(n, 99)
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
        med = re.search(r'MEDIDAS\s+ADOTADAS(?:\s+PELA\s+GUARNI[ÇC][ÃA]O)?\s*:?(.*?)(?=SA[ÍI]DA\s+DA\s+DP|POLICIAIS\s+QUE\s+ATENDERAM|\[N[º°o]|OBJETOS\s+RELACIONADOS|\Z)', piece, re.I | re.S)
        measures = med.group(1) if med else ''
        if not measures.strip():
            warnings.append('Seção "MEDIDAS ADOTADAS PELA GUARNIÇÃO" não encontrada ou vazia: indicadores lidos só do TIPO.')
        found = analyze_measures(m.group(1), measures)
        warnings.extend(found.pop('warnings'))
        warnings.append('Conferência humana obrigatória: verifique indicadores, local e CIA.')
        rows.append({
            'date': day, 'time': date_match.group(2) or '', 'cia': cia, 'city': city,
            'location': address[:350], 'neighborhood': bairro_from_address(address), 'ref': m.group(2), 'kind': re.sub(r'\s+',' ',m.group(1)).strip()[:120], 'crime_category': crime_type(m.group(1)),
            **found,
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
    if not private:
        for k in ('location','ref','notes','source','created_at','updated_at','lat','lon'):
            d.pop(k, None)
    return d


