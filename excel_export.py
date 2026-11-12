"""Creates a standards-compliant XLSX file without external spreadsheet libraries."""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import datetime
from io import BytesIO
from itertools import groupby
from xml.sax.saxutils import escape
from zipfile import ZipFile, ZIP_DEFLATED

INDICATORS = [('conducoes','Conduções'),('flagrantes','Flagrantes'),('tco','TCO'),('mandados','Mandados'),('veiculos','Veículos recuperados')]

def _col(n):
    s=''
    while n:
        n,r=divmod(n-1,26);s=chr(65+r)+s
    return s

def _cell(value, addr, header=False):
    st=' s="1"' if header else ''
    if isinstance(value, (int,float)) and not isinstance(value,bool):
        return f'<c r="{addr}"{st}><v>{value}</v></c>'
    txt=escape(str(value if value is not None else ''), {'"':'&quot;'})
    # text forced, avoids Excel evaluating strings as formulas
    return f'<c r="{addr}" t="inlineStr"{st}><is><t xml:space="preserve">{txt}</t></is></c>'

def _sheet(rows):
    header, *data = rows
    body=[]
    for rnum,row in enumerate(rows,1):
        cells=''.join(_cell(v,f'{_col(i)}{rnum}',rnum==1) for i,v in enumerate(row,1))
        body.append(f'<row r="{rnum}">{cells}</row>')
    maxcol=_col(max(map(len,rows)))
    widths='<cols>'+''.join(f'<col min="{i}" max="{i}" width="{min(40,max(13,len(str(v))+3))}" customWidth="1"/>' for i,v in enumerate(header,1))+'</cols>'
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<dimension ref="A1:{maxcol}{len(rows)}"/><sheetViews><sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
            f'{widths}<sheetData>{"".join(body)}</sheetData><autoFilter ref="A1:{maxcol}{len(rows)}"/></worksheet>')

def build_xlsx(records):
    ordered=sorted(records,key=lambda r:(r.get('date',''),r.get('ref','')))
    rows=[['Data','Horário','Número','Tipo','Classificação','CIA','Cidade','Bairro','Endereço / local','Conduções','Flagrantes','TCO','Mandados','Veículos recuperados','Drogas detalhadas','Armas detalhadas','Observações','Origem']]
    drugs=[['Data','Ocorrência','CIA','Cidade','Bairro','Droga','Quantidade','Unidade']]
    guns=[['Data','Ocorrência','CIA','Cidade','Bairro','Arma','Quantidade']]
    crimes=[['Data','Horário','Número','Crime','CIA','Cidade','Bairro','Local','Observações']]
    cia_city=defaultdict(lambda:Counter())
    month=defaultdict(lambda:Counter())
    years=defaultdict(lambda:Counter())
    neighborhoods=defaultdict(lambda:Counter())
    for r in ordered:
        cls=r.get('crime_category','outros')
        label={'homicidio':'Homicídio','tentativa_homicidio':'Tentativa de homicídio','outros':'Outros'}.get(cls,'Outros')
        base=[r.get('date',''),r.get('time',''),r.get('ref',''),r.get('kind',''),label,r.get('cia',''),r.get('city',''),r.get('neighborhood',''),r.get('location','')]
        rows.append(base+[int(r.get(k,0)) for k,_ in INDICATORS]+['; '.join(f"{d['type']}: {d['qty']} {d.get('unit','g')}" for d in r.get('drugs',[])),'; '.join(f"{g['type']}: {g['qty']}" for g in r.get('guns',[])),r.get('notes',''),r.get('source','')])
        for d in r.get('drugs',[]):drugs.append([r.get('date',''),r.get('ref',''),r.get('cia',''),r.get('city',''),r.get('neighborhood',''),d.get('type',''),d.get('qty',0),d.get('unit','')])
        for g in r.get('guns',[]):guns.append([r.get('date',''),r.get('ref',''),r.get('cia',''),r.get('city',''),r.get('neighborhood',''),g.get('type',''),g.get('qty',0)])
        if cls in ('homicidio','tentativa_homicidio'):
            crimes.append([r.get('date',''),r.get('time',''),r.get('ref',''),label,r.get('cia',''),r.get('city',''),r.get('neighborhood',''),r.get('location',''),r.get('notes','')])
        for key in ((r.get('cia',''),r.get('city','')),r.get('date','')[:7],r.get('date','')[:4],(r.get('city',''),r.get('neighborhood',''))):pass
        counters=[cia_city[(r.get('cia',''),r.get('city',''))],month[r.get('date','')[:7]],years[r.get('date','')[:4]],neighborhoods[(r.get('city',''),r.get('neighborhood',''))]]
        for c in counters:
            c['Ocorrências']+=1
            for field,display in INDICATORS:c[display]+=int(r.get(field,0))
            c['Armas']+=sum(int(g.get('qty',0)) for g in r.get('guns',[]))
            c['Ocorrências com drogas']+=int(bool(r.get('drugs')))
            c['Homicídios']+=int(cls=='homicidio')
            c['Tentativas']+=int(cls=='tentativa_homicidio')
    metric_cols=['Ocorrências']+[display for _,display in INDICATORS]+['Armas','Ocorrências com drogas','Homicídios','Tentativas']
    def summary(group,head):
        out=[head+metric_cols]
        for key,c in sorted(group.items()):
            out.append(list(key) if isinstance(key,tuple) else [key] + [c[m] for m in metric_cols]) if not isinstance(key,tuple) else out.append(list(key)+[c[m] for m in metric_cols])
        return out
    tabs=[('Ocorrências',rows),('Drogas',drugs),('Armas',guns),('Crimes violentos',crimes),('CIA e Cidade',summary(cia_city,['CIA','Cidade'])),('Mensal',summary(month,['Mês'])),('Anual',summary(years,['Ano'])),('Bairros',summary(neighborhoods,['Cidade','Bairro'])),('Legenda',[['Campo','Significado'],['Homicídio','Classificado após revisão administrativa'],['Tentativa de homicídio','Classificada após revisão administrativa'],['Ocorrências com drogas','Número de ocorrências com ao menos uma droga'],['Quantidades de drogas','Não somar unidades diferentes; consultar aba Drogas'],['Exportação',datetime.now().isoformat(timespec='seconds')]])]
    bio=BytesIO()
    with ZipFile(bio,'w',ZIP_DEFLATED) as z:
        z.writestr('[Content_Types].xml','<?xml version="1.0" encoding="UTF-8"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/><Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/><Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>'+''.join(f'<Override PartName="/xl/worksheets/sheet{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>' for i in range(1,len(tabs)+1))+'</Types>')
        z.writestr('_rels/.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>')
        z.writestr('xl/workbook.xml','<?xml version="1.0" encoding="UTF-8"?><workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>'+''.join(f'<sheet name="{escape(name)}" sheetId="{i}" r:id="rId{i}"/>' for i,(name,_) in enumerate(tabs,1))+'</sheets></workbook>')
        z.writestr('xl/_rels/workbook.xml.rels','<?xml version="1.0" encoding="UTF-8"?><Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'+''.join(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet{i}.xml"/>' for i in range(1,len(tabs)+1))+f'<Relationship Id="rId{len(tabs)+1}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>')
        z.writestr('xl/styles.xml','<?xml version="1.0" encoding="UTF-8"?><styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><fonts count="2"><font><sz val="11"/><name val="Aptos"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Aptos"/></font></fonts><fills count="3"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF14130F"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders><cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/><xf numFmtId="0" fontId="1" fillId="2" borderId="0" xfId="0" applyFont="1" applyFill="1"/></cellXfs></styleSheet>')
        for i,(_,sheet) in enumerate(tabs,1):z.writestr(f'xl/worksheets/sheet{i}.xml',_sheet(sheet))
    return bio.getvalue()
