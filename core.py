"""Extração de PDF, validação e serialização."""
import datetime as dt
import json
import re
import unicodedata

COUNT_FIELDS = ('conducoes','flagrantes','tco','mandados','veiculos')
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
        cia = (cia_match.group(1) + 'ª CIA') if cia_match else 'Não informada (avulsa)'
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
        # Use MEDIDAS and DESCRIÇÃO sections, but avoid counting repeated narrative twice.
        descriptive = normal(piece)
        conducao = 1 if re.search(r'CONDU(?:CAO|ZID[OA]?|ZIMOS|ZIR|ZIDO).*?(?:DELEGACIA|POLICIA CIVIL|\bDP\b)|(?:DELEGACIA|\bDP\b).*?CONDUZID[OA]', descriptive, re.S) else 0
        # Must not classify acronym APF as arrest without corroboration.
        flagrante = 1 if re.search(r'(?:PRES[OA]|PRISAO) EM FLAGRANTE|AUTUAD[OA] EM FLAGRANTE|FLAGRANTE DELITO FORMALIZAD', descriptive) else 0
        if 'APF' in descriptive and not flagrante:
            warnings.append('Menção a APF: prisão em flagrante NÃO confirmada pelo documento. Verifique o auto.')
        tco = 1 if re.search(r'TERMO CIRCUNSTANCIADO (?:DE OCORRENCIA|LAVRADO)|\bTCO\b.{0,35}LAVRAD|LAVRAD.{0,35}\bTCO\b', descriptive) else 0
        mandados = 1 if re.search(r'MANDADO DE PRISAO (?:CUMPRID|EXECUTAD)|CUMPRIMENTO DE MANDADO DE PRISAO', descriptive) else 0
        veiculos = 1 if re.search(r'VEICULO[S]? RECUPERADO[S]?|RECUPERACAO DE VEICULO', descriptive) else 0
        # A bare seized/towed vehicle is not a recovered vehicle.
        if 'DETRAN' in descriptive and not veiculos:
            warnings.append('Veículo recolhido ao DETRAN não foi contabilizado como recuperado.')
        if re.search(r'\b(?:MACONHA|COCAINA|CRACK|ECSTASY|HAXIXE|LSD|SKUNK)\b', descriptive):
            warnings.append('Há referência a drogas. Confira tipo, quantidade e contexto antes de informar apreensões.')
        if re.search(r'ARMA[S]? DE FOGO APREENDID|(?:REVOLVER|PISTOLA|FUZIL|ESPINGARDA) APREENDID', descriptive):
            warnings.append('Há referência a armas. Informe tipos/quantidades somente após conferir.')
        warnings.append('Conferência humana obrigatória: verifique indicadores, local e CIA.')
        rows.append({
            'date': day, 'time': date_match.group(2) or '', 'cia': cia, 'city': city,
            'location': address[:350], 'neighborhood': bairro_from_address(address), 'ref': m.group(2), 'kind': re.sub(r'\s+',' ',m.group(1)).strip()[:120], 'crime_category': crime_type(m.group(1)),
            'conducoes': conducao, 'flagrantes': flagrante, 'tco': tco,
            'mandados': mandados, 'veiculos': veiculos, 'drugs': [], 'guns': [],
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
        for k in ('location','ref','notes','source','created_at','updated_at'):
            d.pop(k, None)
    return d


