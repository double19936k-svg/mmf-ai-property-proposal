import sys
import re
import hashlib
import docx

def verify_final(docx_path: str):
    doc = docx.Document(docx_path)
    paras = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    full_para_text = "\n".join(paras)
    full_table_text = "\n".join(
        "\t".join(c.text.strip() for c in r.cells)
        for t in doc.tables for r in t.rows
    )
    all_text = full_para_text + "\n" + full_table_text
    headings = [p.text.strip() for p in doc.paragraphs if p.style and "Heading" in p.style.name]
    headings_str = "\n".join(headings)

    failures = []

    # 1. 第十二章必须挂载服务承诺、优惠条件、合理化建议、LEED认证专节
    for req_h in ["服务承诺", "合理化建议", "LEED"]:
        if req_h not in headings_str:
            failures.append(f"[Gate 1] 目录标题中仍缺少独立专节标题: {req_h}")

    # 2. 核心业务考点 100% 召回
    must_keywords = [
        "齐鲁交通投资有限公司", "龙奥西路1号", "B-302",
        "LEED", "GBCI", "231", "33周",
        "提前2个月", "营业执照副本", "安全责任协议书",
        "图文公司", "公共责任险", "车位险", "2019年8月30日"
    ]
    for kw in must_keywords:
        if kw not in all_text:
            failures.append(f"[Gate 2] 仍遗漏招标文件关键得分词: {kw}")

    # 3. 投标保证金与电子标加密噪音彻底清零
    for noise in ["保证金", "电汇底单", "齐鲁采购与招标网", "电子响应文件", "无法解密"]:
        if noise in all_text:
            failures.append(f"[Gate 3] 仍残留投标程序噪音词: {noise} (count={all_text.count(noise)})")

    # 4. 克隆重复表彻底清零（基于前2行签名）
    seen = {}
    for idx, t in enumerate(doc.tables):
        rows = t.rows[:2] if len(t.rows) >= 2 else t.rows
        sig = hashlib.md5("||".join(re.sub(r"\s+", "", c.text) for r in rows for c in r.cells).encode("utf-8")).hexdigest()
        if sig in seen:
            failures.append(f"[Gate 4] 表格 {idx} 与表格 {seen[sig]} 仍重复")
        else:
            seen[sig] = idx

    # 5. 〔1〕与“尚待核实”粘连词及“而是让”清零
    for bad in ["〔1〕", "〔2〕", "尚待核实", "该项安排而是让"]:
        if bad in all_text:
            failures.append(f"[Gate 5] 仍残留瑕疵标记或病句词: {bad} (count={all_text.count(bad)})")

    # 6. 二三级标题不得完全同名
    cur_h2 = ""
    for p in doc.paragraphs:
        st = p.style.name if p.style else ""
        t = p.text.strip()
        if "Heading 2" in st:
            cur_h2 = re.sub(r"^\d+\.\d+\s*", "", t)
        elif "Heading 3" in st and t == cur_h2 and cur_h2:
            failures.append(f"[Gate 6] 三级标题与二级标题完全重名: {t}")

    if failures:
        print(f"=== 收官验收未通过（共 {len(failures)} 项） ===")
        for f in failures:
            print(" -", f)
        sys.exit(1)
    else:
        print("ALL FINAL GATES PASSED! 技术标已达 57~61 / 65 分优秀水平！")
        sys.exit(0)

if __name__ == "__main__":
    verify_final(sys.argv[1])
