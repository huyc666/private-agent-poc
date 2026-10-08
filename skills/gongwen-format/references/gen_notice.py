# -*- coding: utf-8 -*-
"""生成《关于印发〈数字国控项目系统应急预案〉的通知》标准公文（GB/T 9704-2012）。
用法：python gen_notice.py
输出：数字国控项目系统应急预案-印发通知.docx
说明：发文机关、发文字号、主送、署名、日期为占位/建议值，需按实际发文主体替换。
"""
import sys, copy
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

sys.path.insert(0, "C:/Users/Lenovo/.workbuddy/skills/gongwen-format/references")
import gongwen_autofix as G

GB = G.GB

# 默认发文机关（示例值，可用 --org 覆盖；不写死到任何具体发文字号/日期）
DEFAULT_ORG = "江西省国有资本运营控股集团有限公司"

RED = RGBColor(0xC0, 0x00, 0x00)

# 红头/主送/落款/日期：默认不添加，仅显式传参才加（强制原则）
def _opt(argv, key):
    if key in argv:
        i = argv.index(key) + 1
        if i < len(argv):
            return argv[i]
    return None

hongtou = "--hongtou" in sys.argv          # 红头文件
zs = _opt(sys.argv, "--zs")                # 主送机关
luo = _opt(sys.argv, "--luo")              # 署名
date_str = _opt(sys.argv, "--date")        # 成文日期
org = _opt(sys.argv, "--org") or DEFAULT_ORG
title = _opt(sys.argv, "--title") or "关于印发《数字国控项目系统应急预案》的通知"
pre_path = _opt(sys.argv, "--pre") or _opt(sys.argv, "--source")   # 附件(预案)源 docx
out = _opt(sys.argv, "--out")              # 输出路径（必填）
if not out or not pre_path:
    print("用法: python gen_notice.py --pre 预案源.docx --out 输出.docx "
          "[--hongtou] [--org 发文机关] [--title 标题] [--zs 主送] [--luo 署名] [--date 日期]")
    sys.exit(1)

def set_run_font(run, ea, size=None, bold=None, color=None, ascii_f=None):
    run.font.name = ea
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts'); rpr.append(rf)
    af = ascii_f or ea
    rf.set(qn('w:eastAsia'), ea); rf.set(qn('w:ascii'), af); rf.set(qn('w:hAnsi'), af)
    if size: run.font.size = Pt(size)
    if bold is not None: run.font.bold = bool(bold)
    if color: run.font.color.rgb = color

def add_red_bottom_rule(p):
    pPr = p._p.get_or_add_pPr()
    pBdr = pPr.find(qn('w:pBdr'))
    if pBdr is None:
        pBdr = OxmlElement('w:pBdr'); pPr.append(pBdr)
    b = OxmlElement('w:bottom')
    b.set(qn('w:val'), 'single'); b.set(qn('w:sz'), '12'); b.set(qn('w:space'), '1'); b.set(qn('w:color'), 'C00000')
    pBdr.append(b)

doc = Document()

# 页边距
sec = doc.sections[0]
sec.top_margin = Cm(3.7); sec.bottom_margin = Cm(3.5)
sec.left_margin = Cm(2.8); sec.right_margin = Cm(2.6)

# 样式字体（国标）
normal = doc.styles['Normal']
G.set_eastasia(normal, GB['normal_ea'], GB['ascii_font'])
normal.font.size = Pt(16); normal.font.bold = False
normal.paragraph_format.first_line_indent = Pt(32)
G.exact_linespacing(normal, 28)

h1 = doc.styles['Heading 1']
G.set_eastasia(h1, GB['h1_ea'], GB['ascii_font'])
h1.font.size = Pt(16); h1.font.bold = False
h1.paragraph_format.first_line_indent = None; h1.paragraph_format.left_indent = None
G.exact_linespacing(h1, 28)

h2 = doc.styles['Heading 2']
G.set_eastasia(h2, GB['h2_ea'], GB['ascii_font'])
h2.font.size = Pt(16); h2.font.bold = True
h2.paragraph_format.first_line_indent = None; h2.paragraph_format.left_indent = None
G.exact_linespacing(h2, 28)

# ---- 版头：发文机关标志（红）——仅用户要求"红头/带红头"时生成，默认白头 ----
if hongtou:
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(0); p.paragraph_format.space_after = Pt(0)
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY; p.paragraph_format.line_spacing = Pt(30)
    set_run_font(p.add_run(org + "文件"), "方正小标宋简体", size=22, color=RED)

    # ---- 发文字号 + 红色反线 ----
    p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.paragraph_format.space_before = Pt(6); p.paragraph_format.space_after = Pt(6)
    p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY; p.paragraph_format.line_spacing = Pt(28)
    set_run_font(p.add_run("赣国控发〔2026〕  号"), "仿宋_GB2312", size=16)
    add_red_bottom_rule(p)

# ---- 标题 ----
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_before = Pt(18); p.paragraph_format.space_after = Pt(18)
p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY; p.paragraph_format.line_spacing = Pt(33)
set_run_font(p.add_run(title), "方正小标宋简体", size=22)

# ---- 主送机关（顶格）——默认不添加，仅用户要求时写 ----
if zs:
    p = doc.add_paragraph(); p.paragraph_format.first_line_indent = Pt(0); p.paragraph_format.space_after = Pt(8)
    set_run_font(p.add_run(zs), "仿宋_GB2312", size=16)

# ---- 正文 ----
for t in [
    "现将《数字国控项目系统应急预案》印发给你们，请结合实际认真组织学习，严格遵照执行，确保项目系统安全平稳运行。",
    "本预案自印发之日起施行。此前有关规定与本预案不一致的，以本预案为准。",
]:
    doc.add_paragraph(t)

# ---- 附件说明（左空二字） ----
p = doc.add_paragraph()
set_run_font(p.add_run("附件：数字国控项目系统应急预案"), "仿宋_GB2312", size=16)

# ---- 署名 + 成文日期（右对齐）——默认不添加，仅用户要求时写，绝不臆造 ----
if luo or date_str:
    for t in [x for x in (luo, date_str) if x]:
        p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        p.paragraph_format.first_line_indent = Pt(0); p.paragraph_format.space_after = Pt(0)
        set_run_font(p.add_run(t), "仿宋_GB2312", size=16)

# ---- 分页，进入附件页 ----
doc.add_page_break()

# ---- 附件标题（二号方正小标宋居中） ----
p = doc.add_paragraph(); p.alignment = WD_ALIGN_PARAGRAPH.CENTER
p.paragraph_format.space_after = Pt(18)
p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY; p.paragraph_format.line_spacing = Pt(33)
set_run_font(p.add_run("数字国控项目系统应急预案"), "方正小标宋简体", size=22)

# ---- 克隆预案正文（跳过预案大标题段）+ 表格 ----
pre = Document(pre_path)
pre_body = pre.element.body
new_body = doc.element.body
sectPr = new_body.find(qn('w:sectPr'))
skip_first_p = True
for child in list(pre_body):
    tag = child.tag
    if tag == qn('w:p') and skip_first_p:
        skip_first_p = False
        continue
    if tag in (qn('w:p'), qn('w:tbl')):
        el = copy.deepcopy(child)
        if sectPr is not None:
            sectPr.addprevious(el)
        else:
            new_body.append(el)

# ---- 嵌入字体标记 ----
settings = doc.settings.element
ets = settings.find(qn('w:embedTrueTypeFonts'))
if ets is None:
    ets = OxmlElement('w:embedTrueTypeFonts'); settings.append(ets)
ets.text = '1'

doc.save(out)
print("已生成:", out)
