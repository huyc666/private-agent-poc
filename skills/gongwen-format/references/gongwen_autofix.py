# -*- coding: utf-8 -*-
"""
公文格式自动校正脚本（GB/T 9704-2012）
用法：
  python gongwen_autofix.py 输入.docx 输出.docx
  python gongwen_autofix.py 输入.docx 输出.docx --system-fonts
依赖：python-docx

说明：
  - 默认按国标字体名设置（方正小标宋简体 / 仿宋_GB2312 / 楷体_GB2312 / 黑体），
    并标记嵌入字体。若目标机器未装这些字体，请加 --system-fonts 改用系统
    自带近似字体（仿宋 / 楷体 / 黑体 / 华文中宋），打开即正确显示。
  - 大标题取文档首个标题段落（可用 --title-index 0,1 指定多段，逗号分隔）；
    其余段落按内容识别标题层级并校正。
  - 不覆盖原稿，生成新文件。

强制原则：红头文件、主送机关、落款、成文日期四类结构性要素，
仅当用户显式要求（传 --add-zs/--add-luo/--date/--add-jie 或要求
"红头/带主送/加落款/带日期"）时才添加；默认绝不自动添加、绝不
臆造发文主体与日期。详见 SKILL.md"强制原则"一节。

排版要点（2026-09-25 用户拍板，见 SKILL.md）：
  1) 一/二/三/四级标题与正文一样首行缩进2字、行距固定28磅；二级标题楷体不加粗。
  2) 未显式 --title 时保留原文标题，不更名。
  3) 二级及以下标题若融入正文（非独立段落），与正文同字体（仿宋），以第一个“。”
     为标题截止，标题加粗、后续正文不加粗。
  4) 原文已有页码 → 改写为公文标准格式（— N —，宋体四号，奇右偶左），不新增；
     原文无页码默认不加（--add-page 可显式补加）。
  5) 文末结束语默认不保留：jieyu=False（默认）时不添加，并移除原文末尾已有的
     结束语（"以上报告，请审阅。"等）；仅显式 --add-jie/--jie 时补充。
  6) 正文首字为数字序号（1. / 1、 /（1）/ 1.1 等）时，先判定是否标题：是则按标题
     格式，否则按正文格式。以年份开头（2024年…）、含句末标点或过长者判为正文；
     中文标记（一、/（一）/一是）恒为标题，不经此判定。
"""
import sys
import re
import copy
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_LINE_SPACING, WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.oxml import OxmlElement
from docx.text.run import Run

GB = dict(
    normal_ea="仿宋_GB2312", h1_ea="黑体", h2_ea="楷体_GB2312",
    title_ea="方正小标宋简体", ascii_font="Times New Roman",
)
SYS = dict(
    normal_ea="仿宋", h1_ea="黑体", h2_ea="楷体",
    title_ea="华文中宋", ascii_font="Times New Roman",
)

# 标题层级正则
H1_RE = re.compile(r'^[一二三四五六七八九十]+、')
H2_RE = re.compile(r'^（[一二三四五六七八九十]+）')
# 三级：一是/二是… 或 “1.”/“1、”/“1.1” 后接汉字（避免 “1.2亿元” 误判为标题）
H3_RE = re.compile(r'^([一二三四五六七八九十]+是|\d+[.、]\s*[\u4e00-\u9fff]|\d+\.\d+)')
H4_RE = re.compile(r'^（\d+）')
# 手动页码占位符（无 PAGE 域时也可识别）：—  — / 第 N 页 / - N -
PAGE_HOLDER_RE = re.compile(r'—\s*\d*\s*—|第\s*\d+\s*页|-\s*\d+\s*-')


def set_eastasia(style, ea, ascii_font):
    """关键：必须同时写 w:eastAsia，否则中文仍是默认字体。"""
    rpr = style.element.find(qn('w:rPr'))
    if rpr is None:
        rpr = OxmlElement('w:rPr')
        style.element.append(rpr)
    rf = rpr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts')
        rpr.insert(0, rf)  # rFonts 必须放在 rPr 子元素最前
    rf.set(qn('w:eastAsia'), ea)
    rf.set(qn('w:ascii'), ascii_font)
    rf.set(qn('w:hAnsi'), ascii_font)


def exact_linespacing(style, pt):
    style.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    style.paragraph_format.line_spacing = Pt(pt)
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.space_after = Pt(0)


def diagnose(path):
    """打印各样式实际字体/字号，以及段落手动字体残留情况。"""
    doc = Document(path)
    out = []
    for name in ['Normal', 'Heading 1', 'Heading 2']:
        s = doc.styles[name]
        rpr = s.element.find(qn('w:rPr'))
        ea = None
        if rpr is not None:
            rf = rpr.find(qn('w:rFonts'))
            if rf is not None:
                ea = rf.get(qn('w:eastAsia'))
        out.append(f"  {name}: 中文={ea} 字号={s.font.size.pt if s.font.size else None} 加粗={s.font.bold}")
    cnt = 0
    for idx, p in enumerate(doc.paragraphs):
        for r in p.runs:
            rPr = r._element.rPr
            if rPr is None:
                continue
            if rPr.find(qn('w:rFonts')) is not None:
                cnt += 1
    out.append(f"  含手动字体run总数: {cnt}")
    return "\n".join(out)


def apply(src, dst, system=False, title_indices=(0,)):
    """整体重排：清空所有手动格式，按 GB/T 9704-2012 重建样式与标题层级。"""
    f = SYS if system else GB
    doc = Document(src)

    # 样式
    normal = doc.styles['Normal']
    set_eastasia(normal, f['normal_ea'], f['ascii_font'])
    normal.font.size = Pt(16)
    normal.font.bold = False
    normal.paragraph_format.first_line_indent = Pt(32)  # 首行缩进2字
    exact_linespacing(normal, 28)

    h1 = doc.styles['Heading 1']
    set_eastasia(h1, f['h1_ea'], f['ascii_font'])
    h1.font.size = Pt(16)
    h1.font.bold = False
    h1.paragraph_format.first_line_indent = Pt(32)   # 各级标题与正文同缩进
    h1.paragraph_format.left_indent = None
    exact_linespacing(h1, 28)

    h2 = doc.styles['Heading 2']
    set_eastasia(h2, f['h2_ea'], f['ascii_font'])
    h2.font.size = Pt(16)
    h2.font.bold = False                            # 二级标题楷体不加粗
    h2.paragraph_format.first_line_indent = Pt(32)
    h2.paragraph_format.left_indent = None
    exact_linespacing(h2, 28)

    # 三级/四级标题样式（仿宋、不加粗、与正文同缩进）
    for hname, ea in [('Heading 3', f['normal_ea']), ('Heading 4', f['normal_ea'])]:
        try:
            hs = doc.styles[hname]
            set_eastasia(hs, ea, f['ascii_font'])
            hs.font.size = Pt(16)
            hs.font.bold = False
            hs.paragraph_format.first_line_indent = Pt(32)
            hs.paragraph_format.left_indent = None
            exact_linespacing(hs, 28)
        except KeyError:
            pass

    # 大标题段（支持多段）
    title_set = set(title_indices)
    for ti in title_indices:
        if ti >= len(doc.paragraphs):
            continue
        p = doc.paragraphs[ti]
        p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        p.paragraph_format.line_spacing = Pt(33)
        p.paragraph_format.first_line_indent = None
        for r in p.runs:
            r.font.size = Pt(22)
            rpr = r._element.get_or_add_rPr()
            rf = rpr.find(qn('w:rFonts'))
            if rf is None:
                rf = OxmlElement('w:rFonts')
                rpr.append(rf)
            rf.set(qn('w:eastAsia'), f['title_ea'])
            rf.set(qn('w:ascii'), f['title_ea'])
            rf.set(qn('w:hAnsi'), f['title_ea'])

    # 清除其余段落手动格式覆盖（保留大标题段）
    for idx, para in enumerate(doc.paragraphs):
        if idx in title_set:
            continue
        for r in para.runs:
            rPr = r._element.rPr
            if rPr is None:
                continue
            for tag in ['w:rFonts', 'w:sz', 'w:szCs', 'w:b', 'w:bCs', 'w:i', 'w:iCs']:
                el = rPr.find(qn(tag))
                if el is not None:
                    rPr.remove(el)

    # 按内容识别标题层级并重排（含行内标题拆分）
    for idx, para in enumerate(doc.paragraphs):
        if idx in title_set:
            continue
        t = _text(para).strip()
        if not t:
            para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            para.paragraph_format.line_spacing = Pt(28)
            para.paragraph_format.space_before = Pt(0)
            para.paragraph_format.space_after = Pt(0)
            continue
        level, split = _heading_level_and_split(t)
        if level:
            _format_heading(para, level, f, split)
            continue
        # 正文：继承 Normal（仿宋），仅保证缩进与行距
        para.paragraph_format.first_line_indent = Pt(32)
        para.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        para.paragraph_format.line_spacing = Pt(28)
        para.paragraph_format.space_before = Pt(0)
        para.paragraph_format.space_after = Pt(0)

    # 页边距
    sec = doc.sections[0]
    sec.top_margin = Cm(3.7)
    sec.bottom_margin = Cm(3.5)
    sec.left_margin = Cm(2.8)
    sec.right_margin = Cm(2.6)

    # 嵌入字体标记
    _embed(doc)

    doc.save(dst)
    print(f"已生成: {dst}")


# ===================== 增量校正（原文已是公文格式时） =====================
GONGWEN_EA = ('方正小标宋简体', '方正小标宋', '仿宋_GB2312', '仿宋',
              '楷体_GB2312', '楷体', '华文中宋', '黑体')


def set_style(style, ea, size, bold, ascii_font="Times New Roman"):
    set_eastasia(style, ea, ascii_font)
    style.font.size = size
    style.font.bold = bold
    style.paragraph_format.space_before = Pt(0)
    style.paragraph_format.space_after = Pt(0)


def force_run_font(run, ea=None, size=None, bold=None, ascii_font="Times New Roman"):
    """增量校正核心：仅覆盖指定的 run 字体属性，保留颜色/斜体等其余属性。"""
    rpr = run._element.get_or_add_rPr()
    if ea is not None or ascii_font is not None:
        rf = rpr.find(qn('w:rFonts'))
        if rf is None:
            rf = OxmlElement('w:rFonts')
            rpr.insert(0, rf)
        if ea is not None:
            rf.set(qn('w:eastAsia'), ea)
        if ascii_font is not None:
            rf.set(qn('w:ascii'), ascii_font)
            rf.set(qn('w:hAnsi'), ascii_font)
    if size is not None:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold


def detect_gongwen(doc):
    """检测原文是否已是公文格式，返回信号 dict 与判定布尔值。"""
    sig = dict(heading_styles=False, gongwen_fonts=False, zhusong=False,
               jieyu=False, luokuan=False, odd_even_headers=False)
    for p in doc.paragraphs:
        st = p.style.name
        if st in ('Heading 1', 'Heading 2'):
            sig['heading_styles'] = True
        text = ''.join(r.text for r in p.runs)
        if re.match(r'^(公司领导|各位领导|各(参建|部室|单位))', text):
            sig['zhusong'] = True
        if '以上报告' in text or text.strip().startswith('特此') or '请审阅' in text:
            sig['jieyu'] = True
        if re.match(r'^(数字国控|江西省|集团|公司).{0,12}(工作组|有限公司|集团)$', text.strip()):
            sig['luokuan'] = True
        for r in p.runs:
            rpr = r._element.rPr
            if rpr is not None:
                rf = rpr.find(qn('w:rFonts'))
                if rf is not None:
                    ea = rf.get(qn('w:eastAsia')) or ''
                    if ea in GONGWEN_EA or any(ea.startswith(g) for g in GONGWEN_EA):
                        sig['gongwen_fonts'] = True
    if doc.settings.element.find(qn('w:evenAndOddHeaders')) is not None:
        sig['odd_even_headers'] = True
    already = sig['heading_styles'] or sig['gongwen_fonts']
    return sig, already


def classify(text, style_name):
    """按内容/样式识别段落角色（用于主送/结语/落款等）。标题已由 _heading_level_and_split 处理。"""
    if re.match(r'^(公司领导|各位领导|各(参建|部室|单位))', text):
        return 'zs'
    if '以上报告' in text or text.strip().startswith('特此') or '请审阅' in text:
        return 'jie'
    if re.match(r'^(数字国控|江西省|集团|公司).{0,12}(工作组|有限公司|集团)$', text.strip()):
        return 'luo'
    return 'body'


def _text(p):
    return ''.join(r.text or '' for r in p.runs)


def _looks_like_heading(text):
    """数字序号（N. / N、 /（N） / N.N）开头的段落，判定是否为标题：
    标题是短短语，正文是完整句子。判定规则（需求2）：
      - 以年份(20xx年/19xx年)开头        → 正文（叙事，如“2024年公司…”）
      - 含句末标点‘。’                  → 正文（完整句子，如“1. 公司于…。”）
      - 残余文字长度 > 30 字            → 正文（过长，非标题短语）
      - 否则                           → 标题（短短语，如“1. 加强组织领导”）
    说明：中文标记（一、/（一）/一是）恒为标题，不经此判定；本函数仅用于
    易混淆的数字序号开头段落，避免把“1. 公司于2023年完成…”误判为标题。"""
    m = re.match(r'^([（(]?\d+[.、)）]?\s*)', text)
    rest = text[m.end():]
    if re.match(r'^\d{3,4}年', rest):
        return False
    if '。' in rest:
        return False
    if len(rest) > 30:
        return False
    return True


def _maybe_inline(level, mo, text):
    """二级及以下标题：判断是否“融入正文”（第一个‘。’之后仍有正文）。"""
    idx = text.find('。', len(mo.group(0)))
    if idx == -1 or idx + 1 >= len(text):
        return level, None          # 独立成段落的标题
    return level, idx + 1           # 融入正文：以第一个“。”为标题截止


# 标题候选长度上限（含序号与“。”），超过则视为正文。标题是对本段的概括，不会太长。
TITLE_MAX_LEN = 30

# 后续同级序号标记：本段内出现则判定为“枚举列举”，开头序号不属于标题（按正文处理）
_SIB_RE = re.compile(r'[二三四五六七八九十百]+[、是]|（[二三四五六七八九十百]+）|（[0-9０-９]{1,2}）')


def _title_candidate(text, mo):
    """标题候选文本：序号之后到第一个‘。’（含）或段末。"""
    idx = text.find('。', mo.end())
    return text[:idx + 1] if idx != -1 else text


def _is_cn_heading(text, mo):
    """中文序号开头段落是否为标题（需求2补充，用户拍板）：
      - 标题候选长度 > 30 字           → 太长，非标题（按正文）
      - 本段内出现后续序号（二、/（二）/二是/（2）等）→ 枚举列举，开头非标题（按正文）
      - 否则                          → 视为标题（第一句为概括、短小）
    说明：不再“中文标记恒为标题”，需结合首句是否为本段概括来判定。"""
    if len(_title_candidate(text, mo)) > TITLE_MAX_LEN:
        return False
    if _SIB_RE.search(text, mo.end()):
        return False
    return True


def _heading_level_and_split(text):
    """返回 (level, split)。
    level: 1/2/3/4 命中一级~四级标题；0 非标题起始段（含判为正文者）。
    split: 仅当该段为“标题融入正文”时非 None，表示标题截止字符索引（含“。”）；
           否则 None（独立成段落的标题）。
    判定原则（需求2 + 本轮补充）：中文标记（一、/（一）/一是）与数字序号（N./N、/（N）/N.N）
    均先判定“是否为标题”——首句须为本段概括（标题候选 ≤30 字，且本段内不出现后续序号），
    否则按正文处理；判定为标题后，再判断是否“融入正文”。"""
    text = text.strip()
    mo = H1_RE.match(text)
    if mo:
        if _is_cn_heading(text, mo):
            return 1, None
        return 0, None
    mo = H2_RE.match(text)            # （一）
    if mo:
        if _is_cn_heading(text, mo):
            return _maybe_inline(2, mo, text)
        return 0, None
    mo = H3_RE.match(text)            # 一是… 或 N. / N、 / N.N
    if mo:
        if mo.group(0)[0] in '一二三四五六七八九十百':   # 中文“一是”分支
            if _is_cn_heading(text, mo):
                return _maybe_inline(3, mo, text)
            return 0, None
        if not _looks_like_heading(text):               # 数字序号分支，需判定
            return 0, None
        if not _is_cn_heading(text, mo):
            return 0, None
        return _maybe_inline(3, mo, text)
    mo = H4_RE.match(text)            # （N） 数字
    if mo:
        if not _looks_like_heading(text):
            return 0, None
        if not _is_cn_heading(text, mo):
            return 0, None
        return _maybe_inline(4, mo, text)
    return 0, None


def _apply_inline_heading(p, split):
    """将“标题融入正文”的段落按 run 拆分：标题部分[0,split)加粗，正文部分[split:]不加粗；
    字体均保持与正文一致（不强制改为公文字体），满足需求3“与正文保持同字体”。"""
    runs = p.runs
    pos = 0
    infos = []
    for r in runs:
        t = r.text or ''
        infos.append([r, pos, pos + len(t)])
        pos += len(t)
    for r, s, e in infos:
        if e <= split:
            r.font.bold = True
        elif s >= split:
            r.font.bold = False
        else:
            # run 跨拆分点：拆为标题段 + 正文段
            head = (r.text or '')[:split - s]
            body = (r.text or '')[split - s:]
            r.text = head
            r.font.bold = True
            new_el = OxmlElement('w:r')
            rPr = OxmlElement('w:rPr')
            src_rPr = r._element.find(qn('w:rPr'))
            if src_rPr is not None:
                rPr.append(copy.deepcopy(src_rPr))
            new_el.append(rPr)
            t_el = OxmlElement('w:t')
            t_el.set(qn('xml:space'), 'preserve')
            t_el.text = body
            new_el.append(t_el)
            r._element.addnext(new_el)
            nr = Run(new_el, p)
            nr.font.bold = False


def _format_heading(p, level, f, split=None):
    """格式化标题段落。
    split=None → 独立成段落：一级黑体、二级楷体不加粗、三级/四级仿宋不加粗；均与正文同缩进2字、行距28。
    split!=None → 融入正文（行内）：与正文同字体，标题加粗、后续正文不加粗。"""
    p.paragraph_format.first_line_indent = Pt(32)   # 各级标题与正文同缩进2字
    p.paragraph_format.left_indent = None
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    p.paragraph_format.line_spacing = Pt(28)
    p.paragraph_format.space_before = Pt(0)
    p.paragraph_format.space_after = Pt(0)
    p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    if split is None:
        if level == 1:
            ea, bold = f['h1_ea'], False
        elif level == 2:
            ea, bold = f['h2_ea'], False            # 二级标题楷体不加粗
        else:
            ea, bold = f['normal_ea'], False         # 三级/四级 仿宋不加粗
        for r in p.runs:
            force_run_font(r, ea=ea, size=Pt(16), bold=bold, ascii_font=f['ascii_font'])
    else:
        _apply_inline_heading(p, split)


def _embed(doc):
    settings = doc.settings.element
    ets = settings.find(qn('w:embedTrueTypeFonts'))
    if ets is None:
        ets = OxmlElement('w:embedTrueTypeFonts')
        settings.append(ets)
    ets.text = '1'


def _footer_para(right, page_ea="宋体"):
    p = OxmlElement('w:p')
    pPr = OxmlElement('w:pPr')
    jc = OxmlElement('w:jc')
    jc.set(qn('w:val'), 'right' if right else 'left')
    pPr.append(jc)
    p.append(pPr)
    for token in ['— ', 'PAGE', ' —']:
        if token == 'PAGE':
            fs = OxmlElement('w:fldSimple')
            fs.set(qn('w:instr'), 'PAGE \\* MERGEFORMAT')
            r = OxmlElement('w:r')
            rPr = OxmlElement('w:rPr')
            rf = OxmlElement('w:rFonts')
            rf.set(qn('w:eastAsia'), page_ea)
            rf.set(qn('w:ascii'), page_ea)
            rf.set(qn('w:hAnsi'), page_ea)
            rPr.append(rf)
            sz = OxmlElement('w:sz')
            sz.set(qn('w:val'), '28')              # 四号
            rPr.append(sz)
            szCs = OxmlElement('w:szCs')
            szCs.set(qn('w:val'), '28')
            rPr.append(szCs)
            r.append(rPr)
            t = OxmlElement('w:t')
            t.text = '1'
            r.append(t)
            fs.append(r)
            p.append(fs)
        else:
            r = OxmlElement('w:r')
            rPr = OxmlElement('w:rPr')
            rf = OxmlElement('w:rFonts')
            rf.set(qn('w:eastAsia'), page_ea)
            rf.set(qn('w:ascii'), page_ea)
            rf.set(qn('w:hAnsi'), page_ea)
            rPr.append(rf)
            sz = OxmlElement('w:sz')
            sz.set(qn('w:val'), '28')
            rPr.append(sz)
            szCs = OxmlElement('w:szCs')
            szCs.set(qn('w:val'), '28')
            rPr.append(szCs)
            r.append(rPr)
            t = OxmlElement('w:t')
            t.text = token
            r.append(t)
            p.append(r)
    return p


def _footer_has_page(footer):
    """判断页脚/页眉是否已含页码（PAGE 域 或 手动页码占位符）。"""
    el = footer._element
    if any(True for _ in el.iter(qn('w:fldSimple'))):
        return True
    txt = ''.join(r.text or '' for pp in footer.paragraphs for r in pp.runs)
    return bool(PAGE_HOLDER_RE.search(txt))


def _clear_page_paras(footer):
    el = footer._element
    for p in list(el.findall(qn('w:p'))):
        txt = ''.join(r.text or '' for r in p.findall(qn('w:r')))
        if any(True for _ in p.iter(qn('w:fldSimple'))) or PAGE_HOLDER_RE.search(txt):
            el.remove(p)


def normalize_page_numbers(doc, f, add_if_absent=False):
    """页码处理（需求4）：
    原文已有页码 → 改写为公文标准格式（— N —，宋体四号，奇右偶左），不新增重复页码；
    原文无页码且 add_if_absent=False → 不加；add_if_absent=True → 补加。"""
    settings = doc.settings.element
    if settings.find(qn('w:evenAndOddHeaders')) is None:
        settings.append(OxmlElement('w:evenAndOddHeaders'))
    sec = doc.sections[0]
    odd, even = sec.footer, sec.even_page_footer
    has = (_footer_has_page(odd) or _footer_has_page(even)
           or _footer_has_page(sec.header) or _footer_has_page(sec.even_page_header))
    if has:
        # 已有页码 → 改写（清掉原有页码段，写入标准格式）
        for hf in (sec.header, sec.even_page_header):
            _clear_page_paras(hf)
        _clear_page_paras(odd)
        odd._element.append(_footer_para(True))
        _clear_page_paras(even)
        even._element.append(_footer_para(False))
    elif add_if_absent:
        _clear_page_paras(odd)
        odd._element.append(_footer_para(True))
        _clear_page_paras(even)
        even._element.append(_footer_para(False))
    # 否则：不添加


def _append(doc, text, ea, size, right=False, left=False):
    p = doc.add_paragraph()
    p.paragraph_format.first_line_indent = None
    if right:
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.paragraph_format.right_indent = Pt(64)
    if left:
        p.alignment = WD_ALIGN_PARAGRAPH.LEFT
    r = p.add_run(text)
    force_run_font(r, ea=ea, size=size, bold=False, ascii_font="Times New Roman")
    return p


def apply_incremental(src, dst, system=False, title_indices=(0,),
                      add_zs=None, add_luo=None, date_str=None, add_jie=False,
                      add_page=False):
    """增量校正：原文已是公文格式时，仅修正不合规处，保留作者既有格式（含颜色/斜体）。"""
    f = SYS if system else GB
    doc = Document(src)

    # 1) 基础样式设为正字（仅影响继承样式者；手动覆盖段由下方按角色校正）
    set_style(doc.styles['Normal'], f['normal_ea'], Pt(16), False, f['ascii_font'])
    set_style(doc.styles['Heading 1'], f['h1_ea'], Pt(16), False, f['ascii_font'])
    set_style(doc.styles['Heading 2'], f['h2_ea'], Pt(16), False, f['ascii_font'])  # 二级不加粗
    for hname, ea in [('Heading 3', f['normal_ea']), ('Heading 4', f['normal_ea'])]:
        try:
            set_style(doc.styles[hname], ea, Pt(16), False, f['ascii_font'])
        except KeyError:
            pass

    # 2) 页边距（幂等设置，安全）
    sec = doc.sections[0]
    sec.top_margin = Cm(3.7); sec.bottom_margin = Cm(3.5)
    sec.left_margin = Cm(2.8); sec.right_margin = Cm(2.6)

    # 3) 标题：仅覆盖字体/字号/对齐/行距，不改内容
    for ti in title_indices:
        if ti < len(doc.paragraphs):
            p = doc.paragraphs[ti]
            p.paragraph_format.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            p.paragraph_format.line_spacing = Pt(33)
            p.paragraph_format.first_line_indent = None
            for r in p.runs:
                force_run_font(r, ea=f['title_ea'], size=Pt(22), bold=False, ascii_font=f['title_ea'])

    # 4) 按角色校正正文段（保留非覆盖属性，如颜色/斜体）
    for p in doc.paragraphs:
        t = _text(p).strip()
        if not t:
            continue
        level, split = _heading_level_and_split(t)
        if level:
            _format_heading(p, level, f, split)
            continue
        role = classify(t, p.style.name)
        if role == 'zs':
            p.paragraph_format.first_line_indent = None
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            for r in p.runs:
                force_run_font(r, ea=f['normal_ea'], size=Pt(16), bold=False, ascii_font=f['ascii_font'])
        elif role == 'luo':
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            p.paragraph_format.right_indent = Pt(64)
            for r in p.runs:
                force_run_font(r, ea=f['normal_ea'], size=Pt(16), bold=False, ascii_font=f['ascii_font'])
        elif role == 'jie':
            for r in p.runs:
                force_run_font(r, ea=f['normal_ea'], size=Pt(16), bold=False, ascii_font=f['ascii_font'])
        # body：保留作者既有字体/字号，不强制

    # 5) 页码：原文有则改写标准格式；无则不添加（--add-page 可补加）
    normalize_page_numbers(doc, f, add_if_absent=add_page)

    # 6) 缺失要素按需补充（opt-in）
    if add_zs:
        _append(doc, add_zs, f['normal_ea'], Pt(16), left=True)
    if add_luo:
        _append(doc, add_luo, f['normal_ea'], Pt(16), right=True)
    if date_str:
        _append(doc, date_str, f['normal_ea'], Pt(16), right=True)
    if add_jie:
        _append(doc, '以上报告，请审阅。', f['normal_ea'], Pt(16))
    else:
        _strip_trailing_jieyu(doc)   # 文末结束语默认不保留（需求1）

    # 7) 嵌入字体标记
    _embed(doc)

    doc.save(dst)
    print(f"已生成(增量校正): {dst}")


def format_tables(doc, f):
    """通用：将文档内所有三线表内文字统一为仿宋五号、表头加粗居中。"""
    for tbl in doc.tables:
        for ri, row in enumerate(tbl.rows):
            for cell in row.cells:
                for cp in cell.paragraphs:
                    cp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
                    cp.paragraph_format.line_spacing = Pt(20)
                    cp.paragraph_format.space_before = Pt(2)
                    cp.paragraph_format.space_after = Pt(2)
                    cp.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    is_hdr = (ri == 0)
                    for r in cp.runs:
                        if not r.text:
                            continue
                        force_run_font(r, ea=f['normal_ea'], size=Pt(10.5),
                                       bold=is_hdr, ascii_font=f['ascii_font'])


def _is_jieyu_text(t):
    """判断某段是否为“文末结束语”（如“以上报告，请审阅。”“特此报告。”等）。"""
    t = (t or '').strip()
    if not t or len(t) > 24:
        return False
    return bool(re.search(
        r'(以上报告|以上请示|以上汇报|以上总结|以上说明|特此报告|特此请示|'
        r'专此报告|专此请示|请审阅|请批示|妥否[，,请]请批示)', t))


def _strip_trailing_jieyu(doc):
    """文末结束语默认不保留（需求1）：若最后一个非空段落为结束语，则移除。"""
    for p in reversed(list(doc.paragraphs)):
        t = _text(p).strip()
        if not t:
            continue
        if _is_jieyu_text(t):
            el = p._element
            el.getparent().remove(el)
        break   # 仅处理最后一个非空段


def reformat_plain(src, dst, system=False, title_indices=(0,), title=None,
                   zhusong=None, luokuan=None, date_str=None, jieyu=False, add_page=False):
    """通用：将任意以『一、』『（一）』『一是』等标记的普通 docx 校正为 GB/T 9704-2012 公文。

    设计原则（通用性）：
      - 不依赖 Heading 样式，按段落内容识别标题层级（h1/h2/h3/h4/行内），适用于任何公文；
      - 正文保留作者既有字体/字号/颜色，仅修不合规处（缩进/行距/标题字体）；
      - 不写死任何文档路径与标题，全部由参数传入；
      - 红头/主送/落款/日期默认不添加（opt-in），绝不臆造发文主体与日期；
      - title 给定时覆盖首段标题文字，否则保留原文标题（需求2）；
      - 页码：原文有则改写标准格式，无则不添加（--add-page 可补加，需求4）。
    """
    f = SYS if system else GB
    doc = Document(src)

    # 1) 页边距（幂等，安全）
    sec = doc.sections[0]
    sec.top_margin = Cm(3.7); sec.bottom_margin = Cm(3.5)
    sec.left_margin = Cm(2.8); sec.right_margin = Cm(2.6)

    # 2) 标题（可选改名；默认保留原文，需求2）
    title_set = set(title_indices)
    for ti in title_indices:
        if ti >= len(doc.paragraphs):
            continue
        p = doc.paragraphs[ti]
        if title:
            for r in list(p.runs):
                r._element.getparent().remove(r._element)
            p.add_run(title)
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        p.paragraph_format.line_spacing = Pt(33)
        p.paragraph_format.space_before = Pt(0)
        p.paragraph_format.space_after = Pt(0)
        p.paragraph_format.first_line_indent = None
        for r in p.runs:
            force_run_font(r, ea=f['title_ea'], size=Pt(22), bold=False, ascii_font=f['title_ea'])

    # 3) 正文/标题分级（内容识别）
    for idx, p in enumerate(doc.paragraphs):
        if idx in title_set:
            continue
        t = _text(p).strip()
        if not t:
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            p.paragraph_format.line_spacing = Pt(28)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            continue
        level, split = _heading_level_and_split(t)
        if level:
            _format_heading(p, level, f, split)
            continue
        role = classify(t, p.style.name)
        if role == 'zs':
            p.paragraph_format.first_line_indent = None
            p.alignment = WD_ALIGN_PARAGRAPH.LEFT
            for r in p.runs:
                force_run_font(r, ea=f['normal_ea'], size=Pt(16), bold=False, ascii_font=f['ascii_font'])
        elif role == 'jie':
            for r in p.runs:
                force_run_font(r, ea=f['normal_ea'], size=Pt(16), bold=False, ascii_font=f['ascii_font'])
        else:
            # 正文：保留作者既有字体/字号，仅设首行缩进(2字)与固定行距(28磅)
            p.paragraph_format.first_line_indent = Pt(32)
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            p.paragraph_format.line_spacing = Pt(28)
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)

    # 4) 表格
    format_tables(doc, f)

    # 5) 结语：默认不保留（需求1）
    #    jieyu=True（显式 --add-jie/--jie）→ 仅当缺失时补充；
    #    jieyu=False（默认）→ 不添加，并移除原文末尾已有的结束语。
    if jieyu:
        has_jie = any(_is_jieyu_text(_text(p)) for p in doc.paragraphs)
        if not has_jie:
            jie = doc.add_paragraph()
            jie.paragraph_format.first_line_indent = Pt(32)
            jie.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            jie.paragraph_format.line_spacing = Pt(28)
            jie.paragraph_format.space_before = Pt(0)
            jie.paragraph_format.space_after = Pt(0)
            r = jie.add_run('以上报告，请审阅。')
            force_run_font(r, ea=f['normal_ea'], size=Pt(16), bold=False, ascii_font=f['ascii_font'])
    else:
        _strip_trailing_jieyu(doc)

    # 6) 页码：原文有则改写标准格式；无则不添加（--add-page 可补加）
    normalize_page_numbers(doc, f, add_if_absent=add_page)

    # 7) 缺失要素按需补充（opt-in）
    if zhusong:
        _append(doc, zhusong, f['normal_ea'], Pt(16), left=True)
    if luokuan:
        _append(doc, luokuan, f['normal_ea'], Pt(16), right=True)
    if date_str:
        _append(doc, date_str, f['normal_ea'], Pt(16), right=True)

    # 8) 嵌入字体标记
    _embed(doc)
    doc.save(dst)
    print(f"已生成(通用校正): {dst}")


def looks_plain_report(doc):
    """判断原文是否为『一、/（一）标记』的普通公文（无 Heading 样式/公文字体）。"""
    cnt = 0
    for p in doc.paragraphs:
        t = _text(p)
        if H1_RE.match(t) or H2_RE.match(t):
            cnt += 1
    return cnt >= 2


def _opt(argv, key):
    if key in argv:
        i = argv.index(key) + 1
        if i < len(argv):
            return argv[i]
    return None


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python gongwen_autofix.py 输入.docx 输出.docx "
              "[--system-fonts] [--incremental|-i|--full] [--title-index N] [--title 标题] "
              "[--add-zs 主送机关] [--add-luo 署名] [--date 成文日期] [--add-jie] [--add-page] [--diagnose]\n"
              "  auto 模式：已是公文→增量校正；含『一、/（一）』标记但无样式→通用校正(plain)；"
              "其余→整体重排。所有路径/标题均经参数传入，不写死任何文档。\n"
              "  排版要点：各级标题首行缩进2字、行距28磅、二级标题楷体不加粗；"
              "二级及以下标题融入正文时与正文同字体、标题加粗；原文页码改写为标准 — N —（不新增）。")
        sys.exit(1)
    src, dst = sys.argv[1], sys.argv[2]
    system = "--system-fonts" in sys.argv
    incremental = ("--incremental" in sys.argv) or ("-i" in sys.argv)
    full = "--full" in sys.argv
    add_page = "--add-page" in sys.argv
    ti = [0]
    if "--title-index" in sys.argv:
        raw = sys.argv[sys.argv.index("--title-index") + 1]
        ti = [int(x) for x in raw.split(",") if x.strip() != ""]

    if "--diagnose" in sys.argv:
        print(diagnose(src))
        sig, already = detect_gongwen(Document(src))
        print("\n[公文格式检测]")
        for k, v in sig.items():
            print(f"  {k}: {v}")
        print("  判定:", "原文已是公文格式 → 建议用 --incremental 增量校正"
              if already else "原文非公文格式 → 将执行整体重排")
        sys.exit(0)

    # 模式选择：默认 auto（检测）；显式 --full/--incremental/-i 覆盖
    mode = 'incremental' if incremental else ('full' if full else None)
    if mode is None:
        _, already = detect_gongwen(Document(src))
        if already:
            mode = 'incremental'
        elif looks_plain_report(Document(src)):
            mode = 'plain'
        else:
            mode = 'full'
    print(f"[模式] {mode}")

    title = _opt(sys.argv, "--title")
    if mode == 'full':
        apply(src, dst, system=system, title_indices=ti)
    elif mode == 'plain':
        reformat_plain(
            src, dst, system=system, title_indices=ti, title=title,
            zhusong=_opt(sys.argv, "--add-zs"),
            luokuan=_opt(sys.argv, "--add-luo"),
            date_str=_opt(sys.argv, "--date"),
            jieyu=_opt(sys.argv, "--add-jie") is not None,
            add_page=add_page,
        )
    else:
        apply_incremental(
            src, dst, system=system, title_indices=ti,
            add_zs=_opt(sys.argv, "--add-zs"),
            add_luo=_opt(sys.argv, "--add-luo"),
            date_str=_opt(sys.argv, "--date"),
            add_jie="--add-jie" in sys.argv,
            add_page=add_page,
        )
