from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

# 检查类型 → 中文标签（与前端 CheckPage 的 checkOptions 保持一致）
_CHECK_TYPE_LABELS = {
    "compliance": "合规性检查",
    "disqualification": "废标项检查",
    "qualification": "资质核查",
    "pricing": "报价核查",
    "fitScore": "贴合度评分",
    "deposit": "保证金核查",
    "signature": "签章核查",
    "validity": "有效期核查",
    "consistency": "一致性校验",
    "duplicate": "标书查重",
    "mandatoryReq": "★▲参数对照",
    "docIntegrity": "文件完整性",
    "aiTextCheck": "AI文本检查",
    "riskScore": "风险评分",
    "crossCheck": "交叉比对",
    "sampleReport": "样品/检测报告",
    "jointBid": "联合投标协议",
    "ebidSubmit": "电子投标提交",
    "pricingLogic": "报价逻辑闭环",
    "selfcheck": "废标自查",
    "full_check": "全面检查",
    "fullCheck": "全面检查",
}

# 各检查 skill 结果中常见的明细列表字段
_LIST_KEYS = ("items", "checks", "findings", "issues", "problems", "check_items")
_SUGGESTION_KEYS = ("suggestions", "recommendations", "fixes", "actions")

# 汇总标量字段 → 中文标签（用于概览表格）
_SUMMARY_FIELD_LABELS = {
    "total_requirements": "总要求数",
    "compliant": "满足",
    "non_compliant": "不满足",
    "score": "得分",
    "missing": "缺失",
    "warning_count": "警告数",
    "duplicate_ratio": "重复率",
    "business_weight": "商务权重",
    "technical_weight": "技术权重",
    "price_weight": "价格权重",
}

# 明细字段 key → 中文标签
_FIELD_LABELS = {
    "requirement": "招标要求",
    "required": "招标要求",
    "actual": "实际",
    "response": "实际响应",
    "response_location": "响应位置",
    "source_location": "招标位置",
    "suggestion": "建议",
    "fix": "建议",
    "recommendation": "建议",
    "severity": "严重度",
    "score": "分值",
    "criteria": "评分标准",
    "scoring_criteria": "评分标准",
    "detail": "说明",
    "reason": "原因",
    "description": "说明",
    "analysis": "分析",
    "gap_analysis": "差距分析",
    "location_a": "位置A", "value_a": "值A",
    "location_b": "位置B", "value_b": "值B",
    "response_content": "响应内容",
    "response_status": "响应状态",
    "category": "类别",
    "seq": "序号",
    "clause_number": "条款号",
    "status": "状态",
}

# 状态/级别值 → 中文
_STATUS_LABELS = {
    "pass": "通过", "passed": "通过", "ok": "通过", "compliant": "满足",
    "answered": "已响应", "true": "是",
    "fail": "不通过", "failed": "不通过", "non_compliant": "不满足",
    "missing": "缺失", "false": "否",
    "partial": "部分", "warning": "警告", "warn": "警告",
    "high": "高", "medium": "中", "low": "低",
    "critical": "严重", "major": "重要", "minor": "轻微",
    "pending": "待处理", "generated": "已生成",
}

_STATUS_ICONS = {
    "pass": "✅", "passed": "✅", "compliant": "✅", "answered": "✅", "ok": "✅", "true": "✅",
    "fail": "❌", "failed": "❌", "non_compliant": "❌", "missing": "❌", "false": "❌",
    "partial": "⚠️", "warning": "⚠️", "warn": "⚠️",
}
_RISK_EMOJI = {"high": "🔴", "medium": "🟡", "low": "🟢"}


def _truncate(text, max_len: int = 200) -> str:
    text = str(text)
    return text if len(text) <= max_len else text[:max_len] + "…"


def _cn_status(value) -> str:
    if value is None:
        return ""
    k = str(value).lower()
    return _STATUS_LABELS.get(k, str(value))


def _cell(text) -> str:
    """清洗 markdown 表格单元格：去掉会破坏表格结构的字符。

    - `|` 替换为全角「｜」，避免撑破列
    - 换行/回车折叠为空格，多空格折叠为一个
    """
    s = str(text)
    s = s.replace("|", "｜").replace("\r", " ").replace("\n", " ")
    return " ".join(s.split())


_INLINE_BOLD_RE = re.compile(r'\*\*(.+?)\*\*', re.DOTALL)
_INLINE_CODE_RE = re.compile(r'`([^`]+)`')
_INLINE_EM_RE = re.compile(r'(?<!\*)\*([^\s*][^*]*?)\*(?!\*)')


def _inline(text) -> str:
    """把 markdown 内联标记转成 HTML：**加粗**、`代码`、*斜体*。

    注意顺序：先转 **（粗）再用单 * 转斜体时避开已生成的 <strong>，
    避免误伤标签本身。code 最后做以保留反引号内部。
    """
    s = str(text)
    s = _INLINE_BOLD_RE.sub(r'<strong>\1</strong>', s)
    s = _INLINE_EM_RE.sub(r'<em>\1</em>', s)
    s = _INLINE_CODE_RE.sub(r'<code>\1</code>', s)
    return s


class CheckReportExportSkill(Skill):
    name = "check_report_export"
    description = "检查报告导出(Markdown/PDF/HTML)"
    category = "check"
    version = "1.2.0"
    triggers = ["导出报告", "报告导出", "检查报告"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        report_data = ctx.parameters.get("report_data", {})
        format_type = ctx.parameters.get("format", "markdown")
        project_name = ctx.parameters.get("project_name", "未命名项目")

        if not report_data:
            return SkillResult(success=False, error="无报告数据")

        if format_type == "html":
            content = self._to_html(report_data, project_name)
        elif format_type == "json":
            content = json.dumps(report_data, ensure_ascii=False, indent=2)
        else:
            content = self._to_markdown(report_data, project_name)

        return SkillResult(
            success=True,
            data={
                "content": content,
                "format": format_type,
                "size": len(content.encode("utf-8")),
                "generated_at": datetime.now().isoformat(),
            },
        )

    # ------------------------------------------------------------------
    # 结构识别
    # ------------------------------------------------------------------

    @staticmethod
    def _is_full_check_results(data) -> bool:
        if not isinstance(data, dict) or not data:
            return False
        if not set(data.keys()) <= set(_CHECK_TYPE_LABELS):
            return False
        return all(
            isinstance(v, dict) and ("success" in v or "data" in v or "error" in v)
            for v in data.values()
        )

    # ------------------------------------------------------------------
    # Markdown
    # ------------------------------------------------------------------

    def _to_markdown(self, data: dict, project_name: str) -> str:
        lines: list[str] = []
        lines.append("# 投标文件检查报告")
        lines.append("")
        lines.append(f"- **项目名称**：{project_name}")
        lines.append(f"- **生成时间**：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append("")

        if self._is_full_check_results(data):
            self._render_full_check(data, lines)
        else:
            self._render_single_check(data, lines, include_title=False)

        lines.append("---")
        lines.append("*报告由 BidMaster Pro 自动生成*")
        return "\n".join(lines)

    def _render_full_check(self, data: dict, lines: list[str]) -> None:
        rows: list[tuple[str, str, bool]] = []
        for key, wrapper in data.items():
            success = bool(wrapper.get("success"))
            rows.append((key, _CHECK_TYPE_LABELS.get(key, key), success))

        ok_count = sum(1 for _, _, s in rows if s)
        err_count = len(rows) - ok_count

        lines.append("## 检查概览")
        lines.append("")
        lines.append("| 状态 | 数量 |")
        lines.append("|---|---|")
        lines.append(f"| ✅ 通过 | {ok_count} |")
        lines.append(f"| ⚠️ 异常 | {err_count} |")
        lines.append(f"| 合计 | {len(rows)} |")
        lines.append("")

        lines.append("## 各项检查结果")
        lines.append("")
        for key, label, success in rows:
            wrapper = data[key]
            inner = wrapper.get("data") if isinstance(wrapper.get("data"), dict) else {}
            is_high = bool(inner.get("has_critical_issues")) or inner.get("risk_level") == "high"
            icon = "❌" if (is_high and success) else "⚠️" if not success else "✅"
            lines.append(f"### {icon} {label}")
            lines.append("")
            if not success:
                lines.append(f"> ⚠️ 该项检查执行失败：{_truncate(wrapper.get('error') or '未知错误', 300)}")
                lines.append("")
                continue
            if not inner:
                lines.append("> 该项检查未返回数据")
                lines.append("")
                continue
            self._render_single_check(inner, lines, include_title=False)

    def _render_single_check(self, data: dict, lines: list[str], include_title: bool = True) -> None:
        if include_title:
            lines.append("## 检查结果")
            lines.append("")

        # 1) 结论行
        risk = str(data.get("risk_level", "")).lower()
        conclusions: list[str] = []
        if risk:
            conclusions.append(f"风险等级：{_RISK_EMOJI.get(risk, '⚪')} {_cn_status(risk)}")
        if data.get("has_critical_issues"):
            conclusions.append("存在严重问题")
        if data.get("can_submit") is False:
            conclusions.append("尚不可提交")
        if conclusions:
            lines.append(f"**结论**：{' ｜ '.join(conclusions)}")
            lines.append("")

        # 2) 关键指标表格
        table_rows: list[tuple[str, str]] = []
        summary_keys = ("total_requirements", "compliant", "non_compliant", "score", "missing",
                        "warning_count", "business_weight", "technical_weight", "price_weight")
        for k in summary_keys:
            if k in data and not isinstance(data[k], (list, dict)):
                table_rows.append((_SUMMARY_FIELD_LABELS.get(k, k), str(data[k])))
        if table_rows:
            lines.append("**关键指标**")
            lines.append("")
            lines.append("| 指标 | 数值 |")
            lines.append("|---|---|")
            for label, val in table_rows:
                lines.append(f"| {_cell(label)} | {_cell(val)} |")
            lines.append("")

        # 3) 明细列表
        for list_key in _LIST_KEYS:
            items = data.get(list_key)
            if not isinstance(items, list) or not items:
                continue
            heading = list_key
            if list_key == "items":
                heading = "检查项明细"
            elif list_key in ("checks", "check_items"):
                heading = "检查明细"
            elif list_key in ("findings", "issues", "problems"):
                heading = "发现的问题"
            lines.append(f"**{heading}（{len(items)} 项）**")
            lines.append("")
            for i, item in enumerate(items, 1):
                self._render_item(i, item, lines)
            lines.append("")

        # 4) 改进建议
        for sug_key in _SUGGESTION_KEYS:
            suggestions = data.get(sug_key)
            if not isinstance(suggestions, list) or not suggestions:
                continue
            lines.append("**改进建议**")
            lines.append("")
            for sug in suggestions:
                text = sug if isinstance(sug, str) else json.dumps(sug, ensure_ascii=False)
                lines.append(f"- {_truncate(text, 300)}")
            lines.append("")

        # 5) 兜底：剩余标量字段以表格展示，避免丢数据
        consumed = set(_SUMMARY_FIELD_LABELS) | set(_LIST_KEYS) | set(_SUGGESTION_KEYS) | {
            "risk_level", "has_critical_issues", "can_submit", "overall_assessment"}
        leftovers = {k: v for k, v in data.items()
                     if k not in consumed and not isinstance(v, (list, dict))}
        if leftovers:
            lines.append("**其他信息**")
            lines.append("")
            lines.append("| 字段 | 值 |")
            lines.append("|---|---|")
            for k, v in leftovers.items():
                lines.append(f"| {_cell(_FIELD_LABELS.get(k, k))} | {_cell(v)} |")
            lines.append("")

    def _render_item(self, idx: int, item, lines: list[str]) -> None:
        if not isinstance(item, dict):
            lines.append(f"{idx}. {_truncate(item)}")
            lines.append("")
            return

        title = ""
        for key in ("check_name", "name", "title", "item", "requirement",
                    "scoring_item", "issue", "category"):
            if item.get(key):
                title = str(item[key])
                break
        title = title or f"检查项 {idx}"

        status = ""
        for key in ("status", "response_status", "passed", "result"):
            if item.get(key) is not None and item.get(key) != "":
                status = _cn_status(item[key])
                break
        icon_raw = str(item.get("status") or item.get("response_status") or "").lower()
        icon = _STATUS_ICONS.get(icon_raw, "")
        head = f"{idx}. **{_truncate(title, 120)}**"
        if status:
            head += f"　{icon} {status}"
        lines.append(head)

        consumed = {"check_name", "name", "title", "item", "requirement",
                    "scoring_item", "issue", "category",
                    "status", "response_status", "passed", "result"}
        for k, v in item.items():
            if k in consumed or v is None or v == "":
                continue
            if isinstance(v, (list, dict)):
                v_text = json.dumps(v, ensure_ascii=False)[:150]
            else:
                v_text = str(v)
            v_text = v_text.strip()
            if not v_text:
                continue
            label = _FIELD_LABELS.get(k, k)
            # 状态/级别类值转中文（如 critical→严重、partial→部分），并折叠换行避免撑破行
            v_text = _cn_status(_cell(v_text))
            lines.append(f"　　{label}：{_truncate(v_text)}")
        lines.append("")

    # ------------------------------------------------------------------
    # HTML
    # ------------------------------------------------------------------

    def _to_html(self, data: dict, project_name: str) -> str:
        md_content = self._to_markdown(data, project_name)
        parts = [
            "<!DOCTYPE html>",
            "<html><head><meta charset='utf-8'>",
            f"<title>检查报告 - {project_name}</title>",
            "<style>",
            "body { font-family: 'Microsoft YaHei','PingFang SC',sans-serif; max-width: 900px; margin: 0 auto; padding: 20px; line-height: 1.7; color: #1f2937; }",
            "h1 { color: #1a56db; border-bottom: 2px solid #1a56db; padding-bottom: 8px; }",
            "h2 { color: #1f2937; margin-top: 24px; border-bottom: 1px solid #e5e7eb; padding-bottom: 4px; }",
            "h3 { color: #374151; margin-top: 18px; }",
            "table { border-collapse: collapse; width: 100%; margin: 10px 0; }",
            "th, td { border: 1px solid #d1d5db; padding: 6px 10px; text-align: left; font-size: 13px; vertical-align: top; }",
            "th { background: #f3f4f6; }",
            "tr:nth-child(even) td { background: #fafafa; }",
            "strong { color: #111827; }",
            "blockquote { border-left: 3px solid #f59e0b; margin: 8px 0; padding: 4px 12px; background: #fffbeb; color: #92400e; }",
            "hr { border: none; border-top: 1px solid #e5e7eb; margin: 20px 0; }",
            "</style></head><body>",
        ]

        lines = md_content.split("\n")
        i = 0
        in_list = False

        def close_list():
            nonlocal in_list
            if in_list:
                parts.append("</ul>")
                in_list = False

        def _split(row: str):
            return [c.strip() for c in row.strip().strip("|").split("|")]

        def _is_table_row(line: str):
            s = line.strip()
            return s.startswith("|") and s.endswith("|") and "|" in s[1:-1]

        while i < len(lines):
            line = lines[i]
            if line.startswith("```"):
                close_list()
                parts.append("<pre>")
                i += 1
                while i < len(lines) and not lines[i].startswith("```"):
                    parts.append(lines[i]); i += 1
                parts.append("</pre>")
                i += 1
                continue
            if _is_table_row(line) and i + 1 < len(lines) and _is_table_row(lines[i + 1]):
                block = []
                while i < len(lines) and _is_table_row(lines[i]):
                    cells = _split(lines[i])
                    if all(set(c) <= set("-: ") for c in cells if c):
                        i += 1
                        continue  # 分隔行
                    block.append(cells); i += 1
                if block:
                    parts.append("<table>")
                    parts.append("<thead><tr>" + "".join(f"<th>{_inline(c)}</th>" for c in block[0]) + "</tr></thead>")
                    parts.append("<tbody>")
                    for r in block[1:]:
                        parts.append("<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>")
                    parts.append("</tbody></table>")
                continue
            if line.startswith("#### "):
                close_list(); parts.append(f"<h4>{_inline(line[5:])}</h4>")
            elif line.startswith("### "):
                close_list(); parts.append(f"<h3>{_inline(line[4:])}</h3>")
            elif line.startswith("## "):
                close_list(); parts.append(f"<h2>{_inline(line[3:])}</h2>")
            elif line.startswith("# "):
                close_list(); parts.append(f"<h1>{_inline(line[2:])}</h1>")
            elif line.startswith("- "):
                if not in_list:
                    parts.append("<ul>"); in_list = True
                parts.append(f"<li>{_inline(line[2:])}</li>")
            elif line.startswith("> "):
                close_list(); parts.append(f"<blockquote>{_inline(line[2:])}</blockquote>")
            elif line.startswith("---"):
                close_list(); parts.append("<hr>")
            elif line.strip():
                close_list(); parts.append(f"<p>{_inline(line)}</p>")
            i += 1
        close_list()
        parts.append("</body></html>")
        return "".join(parts)