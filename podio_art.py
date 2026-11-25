"""Arte do pódio de produtividade por CIA para post de Instagram (1080 x 1350, formato retrato 4:5).

Gera PDF de uma página ou PNG com o brasão do 20º BPM, título, período e um pódio (1º, 2º e 3º) por indicador.
Usa PyMuPDF (já exigido pelo projeto) e a fonte Inter (licença OFL, em assets/).
"""
from __future__ import annotations
from pathlib import Path

HERE = Path(__file__).resolve().parent
ASSETS = HERE / 'assets'
W, H = 1080, 1350

INK = (0x14 / 255, 0x13 / 255, 0x0f / 255)
CARD = (0x22 / 255, 0x20 / 255, 0x19 / 255)
LINE = (0x4a / 255, 0x42 / 255, 0x2e / 255)
KHAKI = (0xc3 / 255, 0xb0 / 255, 0x91 / 255)
WHITE = (1, 1, 1)
DIM = (0x8a / 255, 0x80 / 255, 0x6a / 255)
MEDAL = {1: (0xd4 / 255, 0xaf / 255, 0x37 / 255), 2: (0xb8 / 255, 0xbf / 255, 0xc6 / 255), 3: (0xc0 / 255, 0x7a / 255, 0x45 / 255)}
FONTS = {'blk': 'Inter-Black.otf', 'bold': 'Inter-Bold.otf', 'med': 'Inter-Medium.otf'}


def _clean(s, n):
    return ' '.join(str(s or '').split())[:n]


def validate(data):
    if not isinstance(data, dict):
        raise ValueError('Dados inválidos.')
    items = data.get('items')
    if not isinstance(items, list) or not 1 <= len(items) <= 8:
        raise ValueError('Envie de 1 a 8 indicadores.')
    out = []
    for it in items:
        if not isinstance(it, dict):
            raise ValueError('Indicador inválido.')
        vals = it.get('values') or []
        if not isinstance(vals, list) or len(vals) > 40:
            raise ValueError('Lista de CIAs inválida.')
        clean = []
        for v in vals:
            if not isinstance(v, (list, tuple)) or len(v) != 2:
                raise ValueError('Valor inválido.')
            try:
                n = float(v[1])
            except (TypeError, ValueError):
                raise ValueError('Valor inválido.')
            if n < 0 or n > 1e6:
                raise ValueError('Valor inválido.')
            clean.append((_clean(v[0], 40) or '—', n))
        out.append({'title': _clean(it.get('title'), 48), 'note': _clean(it.get('note'), 48), 'values': clean})
    return {'period': _clean(data.get('period'), 70), 'scope': _clean(data.get('scope'), 120), 'items': out}


def podium(values):
    """Até 3 posições com valor > 0. Empates dividem a mesma colocação."""
    arr = sorted([v for v in values if v[1] > 0], key=lambda x: (-x[1], x[0]))
    res, place, last = [], 0, None
    for i, (cia, v) in enumerate(arr):
        if v != last:
            place, last = i + 1, v
        if place > 3:
            break
        res.append((place, cia, v))
    return res[:3]


def _num(v):
    return f'{int(v):,}'.replace(',', '.') if float(v).is_integer() else f'{v:.1f}'.replace('.', ',')


def build(data, fmt='pdf'):
    try:
        import pymupdf as fitz
    except ImportError:  # versões antigas
        import fitz
    d = validate(data)
    doc = fitz.open()
    page = doc.new_page(width=W, height=H)
    F = {k: fitz.Font(fontfile=str(ASSETS / f)) for k, f in FONTS.items()}
    for k, f in FONTS.items():
        page.insert_font(fontname=k, fontfile=str(ASSETS / f))

    def text(x, y, s, font='bold', size=20, color=WHITE, align='l', maxw=None):
        w = F[font].text_length(s, fontsize=size)
        if maxw and w > maxw:  # reduz a fonte para caber
            size = max(9, size * maxw / w)
            w = F[font].text_length(s, fontsize=size)
        x0 = x - w / 2 if align == 'c' else x - w if align == 'r' else x
        page.insert_text((x0, y), s, fontname=font, fontsize=size, color=color)
        return w

    def rect(r, fill=None, color=None, width=0, radius=None):
        page.draw_rect(fitz.Rect(*r), color=color, fill=fill, width=width, radius=radius)

    # fundo e moldura
    rect((0, 0, W, H), fill=INK)
    rect((0, 0, W, 10), fill=KHAKI)
    rect((0, H - 10, W, H), fill=KHAKI)
    rect((28, 28, W - 28, H - 28), color=LINE, width=2, radius=0.02)

    # cabeçalho: brasão + organização
    page.insert_image(fitz.Rect(70, 58, 250, 237), filename=str(ASSETS / 'brasao.png'), keep_proportion=True)
    text(280, 112, 'POLÍCIA MILITAR DA BAHIA', 'bold', 30, KHAKI, maxw=720)
    text(280, 158, '20º BATALHÃO DE POLÍCIA MILITAR', 'med', 30, WHITE, maxw=720)
    text(280, 205, 'PMBA - 20 BPM', 'blk', 26, KHAKI)

    # título e período
    text(W / 2, 318, 'PÓDIO DE PRODUTIVIDADE', 'blk', 70, WHITE, 'c', maxw=W - 120)
    text(W / 2, 362, 'Os 3 primeiros colocados por Companhia', 'med', 28, KHAKI, 'c', maxw=W - 160)
    per = f"PERÍODO: {d['period']}".upper() if d['period'] else 'PERÍODO: TODO O PERÍODO'
    pw = F['blk'].text_length(per, fontsize=26)
    pw = min(pw, W - 200)
    rect((W / 2 - pw / 2 - 26, 384, W / 2 + pw / 2 + 26, 432), fill=KHAKI, radius=0.5)
    text(W / 2, 418, per, 'blk', 26, INK, 'c', maxw=W - 200)

    # grade de pódios: 2 colunas; a última linha com 1 cartão fica centralizada
    items = d['items']
    n = len(items)
    rows = (n + 1) // 2
    top, bottom, gap, side = 462, 1262, 14, 56
    ch = (bottom - top - gap * (rows - 1)) / rows
    cw = (W - side * 2 - gap) / 2
    for i, it in enumerate(items):
        r, c = divmod(i, 2)
        x = side + c * (cw + gap)
        if n % 2 and i == n - 1:
            x = (W - cw) / 2
        y = top + r * (ch + gap)
        rect((x, y, x + cw, y + ch), fill=CARD, color=LINE, width=1.5, radius=0.06)
        tw = text(x + 22, y + 36, it['title'].upper(), 'blk', 22, KHAKI, maxw=cw - 44)
        if it['note'] and cw - 44 - tw - 12 > 60:  # observação na mesma linha do título, se couber
            text(x + 22 + tw + 10, y + 35, it['note'], 'med', 15, DIM, maxw=cw - 44 - tw - 10)
        pod = podium(it['values'])
        base = y + ch - 16
        colw = (cw - 60) / 3
        heights = {1: ch * 0.33, 2: ch * 0.24, 3: ch * 0.16}
        slots = [None, None, None]  # esquerda (2º), centro (1º), direita (3º)
        order = [1, 0, 2]
        for k, p in enumerate(pod):
            slots[order[k]] = p
        for s in range(3):
            bx = x + 30 + s * colw
            cx = bx + colw / 2
            p = slots[s]
            place = p[0] if p else (2 if s == 0 else 1 if s == 1 else 3)
            hgt = heights[place]
            bt = base - hgt
            if p:
                rect((bx + 8, bt, bx + colw - 8, base), fill=MEDAL[place], radius=0.08)
                text(cx, bt + min(hgt - 8, 34), f'{place}º', 'blk', 28 if place == 1 else 24, INK, 'c')
                text(cx, bt - 34, _num(p[2]), 'blk', 32 if place == 1 else 28, WHITE, 'c', maxw=colw - 6)
                text(cx, bt - 8, p[1], 'bold', 19, WHITE, 'c', maxw=colw - 6)
            else:
                rect((bx + 8, bt, bx + colw - 8, base), color=LINE, width=1.5, radius=0.08)
                text(cx, bt + min(hgt - 8, 30), '—', 'bold', 22, DIM, 'c')
        if not pod:
            text(x + cw / 2, y + ch / 2 + 6, 'Sem registros no período', 'med', 18, DIM, 'c')

    foot = 'Dados: Sistema de Estatísticas Operacionais do 20º BPM'
    if d['scope']:
        foot = d['scope'] + ' · ' + foot
    text(W / 2, H - 50, foot, 'med', 17, DIM, 'c', maxw=W - 120)

    if fmt == 'png':
        pix = page.get_pixmap(dpi=72 * 2)  # 2160 x 2700: nítido no Instagram
        out = pix.tobytes('png')
    else:
        doc.subset_fonts()
        out = doc.tobytes(deflate=True, garbage=3)
    doc.close()
    return out
