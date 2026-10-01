# -*- coding: utf-8 -*-
"""构造一份真实的 .docx 测试夹具。

现场用 zipfile 拼出来，**不依赖任何外部素材** —— 否则「样本文件找不到」
会让自检变成假绿。给 `tools/probe_convert.py` 和 `tests/selftest.py` 共用。

夹具刻意把两条标题识别路径都覆盖上：
  · 一级标题用 `pStyle=1`（中文版 Word 就是这么写的，不是 "Heading1"）
  · 二级标题用 `outlineLvl`（有些文档不设样式只给大纲级别）

以及：加粗 / 斜体 / 删除线、超链接、有序 + 无序列表（要查 numbering.xml）、
表格（含需要转义的竖线）、内嵌图片、`w:sdt` 内容控件、`w:br` 软换行、`w:tab`。
"""
from __future__ import annotations

import zipfile
from pathlib import Path

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/picture"
A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
WP = "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"

# 1×1 的透明 PNG，够用来验证「图片被抽出来并指向真实文件」
PNG_1PX = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000a49444154789c6360000002000100ffff03000006000557bfabd400"
    "00000049454e44ae426082"
)

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/numbering.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.numbering+xml"/>
</Types>"""

RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId10" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/image1.png"/>
<Relationship Id="rId11" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/hyperlink" Target="https://example.com/x" TargetMode="External"/>
</Relationships>"""

NUMBERING = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:numbering xmlns:w="{W}">
<w:abstractNum w:abstractNumId="7"><w:lvl w:ilvl="0"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>
<w:abstractNum w:abstractNumId="8"><w:lvl w:ilvl="0"><w:numFmt w:val="decimal"/></w:lvl>
<w:lvl w:ilvl="1"><w:numFmt w:val="lowerLetter"/></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="7"/></w:num>
<w:num w:numId="2"><w:abstractNumId w:val="8"/></w:num>
</w:numbering>"""

DOCUMENT = f"""<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A_NS}" xmlns:wp="{WP}">
<w:body>
<w:p><w:pPr><w:pStyle w:val="1"/></w:pPr><w:r><w:t>九月七日</w:t></w:r></w:p>
<w:p><w:pPr><w:outlineLvl w:val="1"/></w:pPr><w:r><w:t>傍晚</w:t></w:r></w:p>
<w:p><w:r><w:t>今天</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>很重要</w:t></w:r>
<w:r><w:t>，还有</w:t></w:r><w:r><w:rPr><w:i/></w:rPr><w:t>一点</w:t></w:r>
<w:r><w:t>和</w:t></w:r><w:r><w:rPr><w:strike/></w:rPr><w:t>删掉的</w:t></w:r>
<w:r><w:t>。见</w:t></w:r><w:hyperlink r:id="rId11"><w:r><w:t>链接</w:t></w:r></w:hyperlink></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>无序一</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>无序二</w:t></w:r></w:p>
<w:p><w:r><w:t>普通段落断开两个列表</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr><w:r><w:t>有序一</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="2"/></w:numPr></w:pPr><w:r><w:t>有序二</w:t></w:r></w:p>
<w:p><w:r><w:t>换行在</w:t></w:r><w:r><w:br/></w:r><w:r><w:t>下一行</w:t></w:r>
<w:r><w:tab/></w:r><w:r><w:t>制表之后</w:t></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><a:graphic><a:graphicData><a:blip r:embed="rId10"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:tbl>
<w:tr><w:tc><w:p><w:r><w:t>项目</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>值</w:t></w:r></w:p></w:tc></w:tr>
<w:tr><w:tc><w:p><w:r><w:t>含|竖线</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>42</w:t></w:r></w:p></w:tc></w:tr>
</w:tbl>
<w:sdt><w:sdtContent><w:p><w:r><w:t>内容控件里的字也要在</w:t></w:r></w:p></w:sdtContent></w:sdt>
</w:body></w:document>"""

HTML = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<title>七月十九日 · 某个网页</title>
<style>body{font:14px}</style><script>var x=1;</script></head>
<body>
<nav><a href="/">首页</a> <a href="/about">关于</a></nav>
<h1>正文标题</h1>
<p>第一段，包含 <strong>加粗</strong> 与 <em>斜体</em> 以及 <a href="https://example.com/a">外链</a>。</p>
<p>段落里还有 <a href="/relative">站内链接</a>，只要文字。</p>
<ul><li>条目甲</li><li>条目乙</li></ul>
<ol><li>第一</li><li>第二</li></ol>
<blockquote><p>引用的一句话。</p></blockquote>
<pre><code>print("hello")
print("world")</code></pre>
<hr/>
<table><tr><th>列一</th><th>列二</th></tr><tr><td>a</td><td>b</td></tr></table>
<p><img src="images/photo.jpg" alt="照片"/> 后面还有字</p>
<img src="spacer.gif" width="1" height="1"/>
<footer>版权 2026</footer>
</body></html>"""

TXT = "2026-09-02 txt 篇\n\n第一段。\n第二行紧接着。\n\n\n\n第二段。\n"

MD = ("---\ndate: 2026-09-01\ntitle: md 篇\n---\n\n# md 篇\n\n"
      "正文里的 **加粗**。\n\n![图](photo.png)\n")


def build_docx(dst: Path) -> Path:
    """把一份结构完整的 .docx 写到 dst。"""
    dst = Path(dst)
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", RELS)
        z.writestr("word/document.xml", DOCUMENT)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("word/numbering.xml", NUMBERING)
        z.writestr("word/media/image1.png", PNG_1PX)
    return dst


def build_mixed_vault(root: Path) -> Path:
    """建一个 md + txt + docx + html + 无关文件 的来源目录。"""
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "2026-09-01 md 篇.md").write_text(MD, encoding="utf-8")
    (root / "2026-09-02 txt 篇.txt").write_bytes(TXT.encode("gb18030"))
    build_docx(root / "2026-09-03 docx 篇.docx")
    (root / "2026-09-04 html 篇.html").write_text(HTML, encoding="utf-8")
    # md 里引用的图，验证附件解析没被格式开关影响
    (root / "photo.png").write_bytes(PNG_1PX)
    # 不该被当成来源的文件
    (root / "readme.pdf").write_bytes(b"%PDF-1.4 not really")
    return root
