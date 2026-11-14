"""Planilha em PDF (A4 paisagem) com todas as ocorrências. Usa PyMuPDF, já exigido pelo projeto."""
from __future__ import annotations
import datetime as dt
import io
from html import escape

CATS = {'homicidio': 'Homicídio', 'tentativa_homicidio': 'Tentativa de homicídio', 'outros': 'Outros'}
# (título, largura relativa, alinhamento)
COLS = [('Data', 5.2, 0), ('Hora', 3.6, 0), ('Nº', 6.5, 0), ('Natureza', 10, 0), ('Classif.', 5.5, 0), ('CIA', 5.5, 0),
        ('Cidade', 7, 0), ('Bairro', 7.5, 0), ('Endereço / local', 13, 0), ('Cond.', 3.2, 1), ('Flag.', 3.2, 1),
        ('TCO', 3, 1), ('Mand.', 3.3, 1), ('Veíc.', 3.2, 1), ('A.br.', 3.2, 1), ('Simul.', 3.4, 1), ('Adol.', 3.2, 1), ('P.cond.', 3.6, 1), ('Presos', 3.6, 1), ('Drogas', 8.5, 0), ('Armas de fogo', 7.5, 0), ('Observações', 9.5, 0)]


def _row(r):
    d = r.get('date', '')
    date = '/'.join(reversed(d.split('-'))) if d else ''
    drugs = '; '.join(f"{x.get('type','')}: {x.get('qty',0):g} {x.get('unit','g')}" for x in r.get('drugs', []))
    guns = '; '.join(f"{x.get('type','')}: {x.get('qty',0)}" for x in r.get('guns', []))
    n = lambda k: str(int(r.get(k, 0) or 0) or '')
    return [date, r.get('time', ''), r.get('ref', ''), r.get('kind', ''), CATS.get(r.get('crime_category'), 'Outros'),
            r.get('cia', ''), r.get('city', ''), r.get('neighborhood', ''), r.get('location', ''),
            n('conducoes'), n('flagrantes'), n('tco'), n('mandados'), n('veiculos'), n('armas_brancas'), n('simulacros'), n('adolescentes'), n('conduzidos'), n('presos'), drugs, guns, r.get('notes', '')]


def build_pdf(records, username=''):
    import fitz
    ordered = sorted(records, key=lambda r: (r.get('date', ''), r.get('time', ''), r.get('ref', '')))
    total = sum(w for _, w, _ in COLS)
    head = ''.join(f'<th style="width:{w/total*100:.2f}%">{escape(t)}</th>' for t, w, _ in COLS)
    body = []
    for i, r in enumerate(ordered):
        cells = ''.join(f'<td class="{"c" if COLS[j][2] else ""}">{escape(str(v or ""))}</td>' for j, v in enumerate(_row(r)))
        body.append(f'<tr class="{"z" if i % 2 else ""}">{cells}</tr>')
    sums = {k: sum(int(r.get(k, 0) or 0) for r in ordered) for k in ('conducoes', 'flagrantes', 'tco', 'mandados', 'veiculos', 'armas_brancas', 'simulacros', 'adolescentes')}
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=-3))).strftime('%d/%m/%Y %H:%M')
    period = ''
    if ordered:
        f = lambda s: '/'.join(reversed(s.split('-')))
        period = f" · Período: {f(ordered[0]['date'])} a {f(ordered[-1]['date'])}"
    html = f'''<h1>PMBA - 20 BPM · POLÍCIA MILITAR DA BAHIA · 20º BATALHÃO DE POLÍCIA MILITAR</h1>
<h2>Planilha geral de ocorrências</h2>
<p class="m">{len(ordered)} ocorrência(s){period} · Conduções: {sums['conducoes']} · Flagrantes: {sums['flagrantes']} · TCO: {sums['tco']} · Mandados: {sums['mandados']} · Veículos recuperados: {sums['veiculos']} · Armas brancas: {sums['armas_brancas']} · Simulacros: {sums['simulacros']} · Adolescentes apreendidos: {sums['adolescentes']}</p>
<p class="m">Emitido em {now}{(' por ' + escape(username)) if username else ''} · Documento de uso interno — contém dados restritos.</p>
<table><thead><tr>{head}</tr></thead><tbody>{''.join(body) or f'<tr><td colspan="{len(COLS)}">Nenhuma ocorrência cadastrada.</td></tr>'}</tbody></table>'''
    css = '''body{font-family:sans-serif;font-size:6.6pt;color:#16202b}
h1{font-size:10pt;margin:0;color:#5f5126} h2{font-size:12pt;margin:2pt 0 3pt 0} p.m{margin:0 0 2pt 0;font-size:7pt;color:#444}
table{width:100%;border-collapse:collapse;margin-top:5pt}
th{background-color:#14130f;color:#ffffff;font-size:6.6pt;text-align:left;padding:2pt;border:0.5pt solid #14130f}
td{padding:2pt;border:0.5pt solid #d3cab3;vertical-align:top} td.c{text-align:center} tr.z td{background-color:#f4f0e4}'''
    story = fitz.Story(html=html, user_css=css)
    buf = io.BytesIO()
    writer = fitz.DocumentWriter(buf)
    page = fitz.paper_rect('a4-l')
    where = page + (24, 24, -24, -30)
    more = 1
    while more:
        dev = writer.begin_page(page)
        more, _ = story.place(where)
        story.draw(dev)
        writer.end_page()
    writer.close()
    doc = fitz.open('pdf', buf.getvalue())
    for i, p in enumerate(doc, 1):
        p.insert_text((24, page.height - 14), f'PMBA · 20º BPM — Planilha geral de ocorrências · Página {i} de {len(doc)}', fontsize=7, color=(.35, .35, .35))
    out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out
