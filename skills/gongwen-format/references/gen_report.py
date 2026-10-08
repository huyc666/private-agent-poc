# -*- coding: utf-8 -*-
"""
生成《关于数字国控一期培训工作情况的报告》
GB/T 9704-2012 公文格式，支持国标字体版 / 系统字体版两套输出。
用法：
  python gen_report.py gb     -> 国标字体版（方正小标宋简体/仿宋_GB2312/楷体_GB2312）
  python gen_report.py sys    -> 系统字体版（华文中宋/仿宋/楷体）
"""
import sys
from docx import Document
from docx.shared import Pt, Cm, RGBColor
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_LINE_SPACING
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.oxml.ns import qn
from docx.oxml import OxmlElement

# ---------------- 字体方案 ----------------
GB = dict(
    title="方正小标宋简体",
    body="仿宋_GB2312",
    h1="黑体",
    h2="楷体_GB2312",
)
SYS = dict(
    title="华文中宋",
    body="仿宋",
    h1="黑体",
    h2="楷体",
)

ASCII_FONT = "Times New Roman"

# ---------------- 页面参数（GB/T 9704-2012）----------------
TOP, BOTTOM, LEFT, RIGHT = Cm(3.7), Cm(3.5), Cm(2.8), Cm(2.6)
BODY_SIZE = Pt(16)          # 三号
TITLE_SIZE = Pt(22)         # 二号
TABLE_SIZE = Pt(10.5)       # 五号（表格内）
LINE_BODY = Pt(28)
LINE_TITLE = Pt(34)
INDENT = Pt(32)             # 首行缩进 2 字（三号字）


def set_run_font(run, ea, size=None, bold=None, ascii_font=ASCII_FONT):
    """python-docx 设置中文字体必须写 w:eastAsia"""
    rpr = run._element.get_or_add_rPr()
    rf = rpr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts')
        rpr.insert(0, rf)
    rf.set(qn('w:eastAsia'), ea)
    rf.set(qn('w:ascii'), ascii_font)
    rf.set(qn('w:hAnsi'), ascii_font)
    if size is not None:
        run.font.size = size
    if bold is not None:
        run.font.bold = bold


def add_para(doc, text, font, size=BODY_SIZE, bold=False, align=None,
             indent=INDENT, space_line=LINE_BODY, first_line_indent=True):
    p = doc.add_paragraph()
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = space_line
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    if first_line_indent:
        pf.first_line_indent = indent
    if align is not None:
        p.alignment = align
    r = p.add_run(text)
    set_run_font(r, font, size, bold)
    return p


def add_title(doc, text, font):
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    pf = p.paragraph_format
    pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    pf.line_spacing = LINE_TITLE
    pf.space_before = Pt(0)
    pf.space_after = Pt(0)
    r = p.add_run(text)
    set_run_font(r, font, TITLE_SIZE, False, ascii_font=font)
    return p


def set_cell_border(cell, **kwargs):
    tcPr = cell._tc.get_or_add_tcPr()
    borders = OxmlElement('w:tcBorders')
    for edge in ('top', 'left', 'bottom', 'right'):
        if edge in kwargs:
            spec = kwargs[edge]
            el = OxmlElement('w:' + edge)
            el.set(qn('w:val'), spec.get('val', 'single'))
            el.set(qn('w:sz'), str(spec.get('sz', 8)))
            el.set(qn('w:color'), spec.get('color', '000000'))
            borders.append(el)
    tcPr.append(borders)


def add_table(doc, header, rows, font, widths=None, aligns=None):
    t = doc.add_table(rows=1, cols=len(header))
    t.alignment = WD_TABLE_ALIGNMENT.CENTER
    t.autofit = False
    # 三线表：表头顶线粗、表头下线细、表底线粗，无竖线
    hdr = t.rows[0].cells
    for i, h in enumerate(header):
        hdr[i].text = ''
        p = hdr[i].paragraphs[0]
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
        p.paragraph_format.line_spacing = Pt(20)
        p.paragraph_format.space_before = Pt(2)
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(h)
        set_run_font(r, font, TABLE_SIZE, True)
        set_cell_border(hdr[i], top={'sz': 12}, bottom={'sz': 6})

    for ri, row in enumerate(rows):
        cells = t.add_row().cells
        for i, v in enumerate(row):
            cells[i].text = ''
            p = cells[i].paragraphs[0]
            p.alignment = (aligns[i] if aligns else WD_ALIGN_PARAGRAPH.CENTER)
            p.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            p.paragraph_format.line_spacing = Pt(20)
            p.paragraph_format.space_before = Pt(2)
            p.paragraph_format.space_after = Pt(2)
            r = p.add_run(v)
            set_run_font(r, font, TABLE_SIZE, False)
            if ri == len(rows) - 1:
                set_cell_border(cells[i], bottom={'sz': 12})
            else:
                set_cell_border(cells[i])
    if widths:
        for row in t.rows:
            for i, w in enumerate(widths):
                row.cells[i].width = Cm(w)
    # 表后空一行
    sp = doc.add_paragraph()
    sp.paragraph_format.line_spacing_rule = WD_LINE_SPACING.EXACTLY
    sp.paragraph_format.line_spacing = Pt(14)
    sp.paragraph_format.space_before = Pt(0)
    sp.paragraph_format.space_after = Pt(0)
    return t


def _page_field_run(font_page):
    fld = OxmlElement('w:fldSimple')
    fld.set(qn('w:instr'), 'PAGE \\* MERGEFORMAT')
    r2 = OxmlElement('w:r')
    rpr = OxmlElement('w:rPr')
    sz = OxmlElement('w:sz'); sz.set(qn('w:val'), '28'); rpr.append(sz)
    rf = OxmlElement('w:rFonts')
    rf.set(qn('w:eastAsia'), font_page)
    rf.set(qn('w:ascii'), ASCII_FONT)
    rf.set(qn('w:hAnsi'), ASCII_FONT)
    rpr.append(rf)
    r2.append(rpr)
    fld.append(r2)
    return fld


def add_page_number(doc, font_page="宋体"):
    """页码：— 1 —，宋体四号；奇数页居右空一字，偶数页居左空一字"""
    # 开启奇偶页不同
    settings = doc.settings.element
    if settings.find(qn('w:evenAndOddHeaders')) is None:
        eoh = OxmlElement('w:evenAndOddHeaders')
        anchor = settings.find(qn('w:defaultTableStyle'))
        if anchor is not None:
            anchor.addnext(eoh)
        else:
            settings.append(eoh)

    for section in doc.sections:
        for footer, align, side_indent in (
            (section.footer, WD_ALIGN_PARAGRAPH.RIGHT, 'right'),      # 奇数页
            (section.even_page_footer, WD_ALIGN_PARAGRAPH.LEFT, 'left'),  # 偶数页
        ):
            p = footer.paragraphs[0]
            p.alignment = align
            p.paragraph_format.space_before = Pt(0)
            p.paragraph_format.space_after = Pt(0)
            if side_indent == 'right':
                p.paragraph_format.right_indent = Pt(16)
            else:
                p.paragraph_format.left_indent = Pt(16)
            r1 = p.add_run("— ")
            set_run_font(r1, font_page, Pt(14), False)
            p._p.append(_page_field_run(font_page))
            r3 = p.add_run(" —")
            set_run_font(r3, font_page, Pt(14), False)


def build(mode, zhusong=None, luokuan=None, date_str=None, jieyu=False,
           title="关于数字国控一期培训工作情况的报告"):
    """生成一份报告文种公文（GB/T 9704-2012）。

    title/org 等正文为示例内容，调用方可按需替换为实际文稿；输出路径由调用方决定，
    本函数不写死任何文档路径与文件名。"""
    F = GB if mode == 'gb' else SYS
    doc = Document()

    sec = doc.sections[0]
    sec.top_margin, sec.bottom_margin = TOP, BOTTOM
    sec.left_margin, sec.right_margin = LEFT, RIGHT

    st = doc.styles['Normal']
    st.font.size = BODY_SIZE
    rpr = st.element.find(qn('w:rPr'))
    if rpr is None:
        rpr = OxmlElement('w:rPr'); st.element.append(rpr)
    rf = rpr.find(qn('w:rFonts'))
    if rf is None:
        rf = OxmlElement('w:rFonts'); rpr.insert(0, rf)
    rf.set(qn('w:eastAsia'), F['body'])
    rf.set(qn('w:ascii'), ASCII_FONT)
    rf.set(qn('w:hAnsi'), ASCII_FONT)

    C = WD_ALIGN_PARAGRAPH.CENTER
    L = WD_ALIGN_PARAGRAPH.LEFT
    R = WD_ALIGN_PARAGRAPH.RIGHT

    # 标题
    add_title(doc, title, F['title'])

    # 主送机关（默认不添加，仅用户明确要求时写）
    if zhusong:
        add_para(doc, zhusong, F['body'], align=L, first_line_indent=False)

    # 引言
    add_para(doc, "数字国控一期项目自2025年10月启动系统上线以来，培训工作与系统建设同步推进。截至2026年9月18日，各项培训已按计划组织实施完毕。现将有关情况报告如下。", F['body'])

    # 一
    add_para(doc, "一、培训工作总体情况", F['h1'], align=L)
    add_para(doc, "一期培训共纳入计划45项，实际实施34场，计划执行率75.6%；累计培训时长约48小时，覆盖16类业务系统；培训周期自2025年10月10日至2026年9月2日，历时约11个月。总体情况见下表。", F['body'])
    add_table(doc,
              ["项目", "数量", "项目", "数量"],
              [["纳入计划培训", "45项", "累计培训时长", "约48小时"],
               ["已实施培训", "34场", "覆盖业务系统", "16类"],
               ["计划执行率", "75.6%", "培训周期", "2025年10月至2026年9月"]],
              F['body'], widths=[3.6, 2.6, 3.6, 2.6])

    # 二
    add_para(doc, "二、集中培训组织实施情况", F['h1'], align=L)

    add_para(doc, "（一）分阶段推进，与系统上线节奏相匹配", F['h2'], align=L)
    add_para(doc, "一是启动阶段（2025年10月至12月，4场）。围绕公文管理、统一门户、固定资产、智能辅助分析等基础应用开展培训，为系统上线打好基础。", F['body'])
    add_para(doc, "二是全面铺开阶段（2026年1月至5月，22场）。配合各业务模块集中上线，完成不动产、人力资源、法律合规、投资管理、采购、党务党宣、官网、数据管理平台等模块培训，其中1月单月组织13场，为培训峰值。", F['body'])
    add_para(doc, "三是深化补强阶段（2026年8月至9月，3场）。结合前期使用反馈，针对统一门户、信息化项目管理、智能辅助分析开展复训与场景深化培训，重点解决实际操作中的难点问题。", F['body'])

    add_para(doc, "（二）重点模块安排多轮次培训", F['h2'], align=L)
    add_para(doc, "针对业务复杂、使用角色多的模块，采取分角色、分层次的多轮次培训方式。不动产模块按业务员、财务、楼管等不同角色组织4场；人力资源模块按组织人员薪酬、流程、绩效培训薪酬分三次递进组织4场。各模块培训场次分布如下。", F['body'])
    add_table(doc,
              ["序号", "培训模块", "场次", "序号", "培训模块", "场次"],
              [["1", "资产管理（不动产）", "4", "9", "采购", "1"],
               ["2", "人力资源", "4", "10", "办公用品及车辆管理", "1"],
               ["3", "智能辅助分析", "3", "11", "战略规划", "1"],
               ["4", "信息化项目管理", "3", "12", "投资管理", "1"],
               ["5", "资产管理（固定资产）", "2", "13", "官网后台维护", "1"],
               ["6", "法律合规", "2", "14", "党务党宣", "1"],
               ["7", "统一门户", "2", "15", "数据管理平台", "1"],
               ["8", "OA系统（公文管理、业务招待）", "2", "合计", "—", "29"]],
              F['body'], widths=[1.3, 4.5, 1.2, 1.3, 4.5, 1.2])
    add_para(doc, "此外，网络安全意识培训已列入计划，由第三方江西神舟承担，具体时间尚待确定。", F['body'])

    add_para(doc, "（三）培训对象实现分层分类覆盖", F['h2'], align=L)
    add_para(doc, "培训范围覆盖集团本部各部室、各二级子公司及部分三级单位，涉及建工集团、置业集团、交易集团、基金公司、联晟电子、省进出口、联晟投资、商投、吉成物业、商务投资集团等单位；参训角色涵盖系统管理员、财务、人力资源、风控法务、党群、战略投资、采购、办公室、业务员、楼管等各类人员。培训以线下集中为主，党务党宣、三级单位人力资源等2场采用线上会议方式开展。", F['body'])

    # 三
    add_para(doc, "三、部门自主培训开展情况", F['h1'], align=L)
    add_para(doc, "为强化各部门自主应用能力，计划另安排15项由部门自行组织、指定主讲人的自主培训，已完成5场、累计5.5小时，整体完成率33%。具体情况如下。", F['body'])
    add_table(doc,
              ["类别", "数量", "涉及模块"],
              [["已完成", "5项", "不动产、办公用品及车辆、投资管理、信息化项目管理、人力资源"],
               ["调整为自主学习", "2项", "采购（发放操作手册自学）、战略规划（结合业务开展进度安排）"],
               ["尚未开展", "8项", "法律合规、固定资产、官网、党务党宣、数据管理平台、智能辅助分析、投资管理、统一门户"]],
              F['body'], widths=[2.8, 1.6, 8.6], aligns=[C, C, L])

    # 四
    add_para(doc, "四、存在的主要问题", F['h1'], align=L)
    add_para(doc, "（一）个别模块培训尚未落地。受业务开展进度、部门工作安排等因素影响，尚有11项培训未完成，占计划总量24.4%。其中智能辅助分析、投资管理、统一门户3项已由8月至9月的集中培训实质覆盖，需与相关部门确认后予以核销。", F['body'])
    add_para(doc, "（二）自主培训推进不够均衡。15项自主培训中完成5项，完成率明显低于集中培训。部分部门因业务启动较晚或人员变动较为频繁，培训时间难以固定。", F['body'])
    add_para(doc, "（三）培训成效量化支撑不足。目前主要记录培训场次与时间，签到、考核、满意度等台账尚未系统建立，覆盖率与参训人次等量化指标有待补充完善。", F['body'])

    # 五
    add_para(doc, "五、下一步工作安排", F['h1'], align=L)
    add_para(doc, "（一）逐项核销并明确关闭时限。9月底前完成智能辅助分析、投资管理、统一门户3项核销；对法律合规、固定资产、官网后台维护、采购4项，采取短训或发放操作手册方式完成。", F['body'])
    add_para(doc, "（二）对确需延后的事项明确时间节点。党务党宣培训拟采用录制操作视频方式，规避人员变动带来的影响；数据管理平台工商数据应用培训结合业务进度另行安排；网络安全意识培训商请第三方明确排期。", F['body'])
    add_para(doc, "（三）固化培训成果。将已完成34场培训的课件、操作手册、录屏资料统一归档，纳入一期项目验收交付物，同时支撑新进人员自主学习。", F['body'])
    add_para(doc, "（四）完善培训成效评估。建立签到与满意度台账，将培训场次进一步量化为参训人次与覆盖率，为后续培训安排提供数据支撑。", F['body'])

    # 结语（文末结束语默认不保留，需求1；仅显式 --jie 时添加）
    if jieyu:
        add_para(doc, "以上报告，请审阅。", F['body'])

    # 落款（右空四字）——默认不添加，仅用户明确要求时写，绝不臆造署名与日期
    if luokuan or date_str:
        for txt in [t for t in (luokuan, date_str) if t]:
            pp = doc.add_paragraph()
            pp.alignment = R
            pf = pp.paragraph_format
            pf.line_spacing_rule = WD_LINE_SPACING.EXACTLY
            pf.line_spacing = LINE_BODY
            pf.space_before = Pt(0)
            pf.space_after = Pt(0)
            pf.right_indent = Pt(64)
            r = pp.add_run(txt)
            set_run_font(r, F['body'], BODY_SIZE)

    add_page_number(doc)
    return doc


def _opt(argv, key):
    if key in argv:
        i = argv.index(key) + 1
        if i < len(argv):
            return argv[i]
    return None


if __name__ == '__main__':
    # 用法：python gen_report.py [gb|sys] [--out 输出路径] [--title 标题]
    #       [--zs 主送] [--luo 署名] [--date 日期] [--no-jie]
    # 红头文件/主送机关/落款/日期：默认不添加，仅显式传参才加（强制原则）
    # 输出路径、标题均由参数决定，不写死任何具体文档。
    args = sys.argv[1:]
    pos = [a for a in args if not a.startswith('--')]
    mode = pos[0] if pos else 'gb'
    FONTS = GB if mode == 'gb' else SYS
    zs = _opt(sys.argv, "--zs")
    luo = _opt(sys.argv, "--luo")
    date_str = _opt(sys.argv, "--date")
    jieyu = "--jie" in sys.argv          # 默认不保留文末结束语（需求1）
    title = _opt(sys.argv, "--title") or "关于数字国控一期培训工作情况的报告"
    out = _opt(sys.argv, "--out")
    if not out:
        out = ("%s（国标字体版）.docx" if mode == 'gb' else "%s（系统字体版）.docx") % title
    d = build(mode, zhusong=zs, luokuan=luo, date_str=date_str, jieyu=jieyu, title=title)
    d.save(out)
    print("saved:", out)
    print("fonts:", FONTS)
    print("主送=%s 落款=%s 日期=%s 结语=%s" % (zs, luo, date_str, jieyu))
