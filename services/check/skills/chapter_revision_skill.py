"""章节修订生成 Skill：根据投标检查发现的修改建议，定向重写章节。

只修改建议点名的部分，其余内容尽量保留，保持章节标题结构与字数。
"""
from __future__ import annotations

import logging

from core.skill_engine.base import Skill, SkillContext, SkillResult

logger = logging.getLogger(__name__)

# 视为「已不符 / 高风险」的状态
_NON_COMPLIANT_STATUS = {
    "non_compliant", "partial", "missing", "fail", "failed", "answered", "warning", "warn", "unsupported",
}


def _is_actionable_finding(item: dict) -> bool:
    """一个发现项是否进入修订范围：须有修改建议，且属于不符/部分/高风险。"""
    if not isinstance(item, dict):
        return False
    suggestion = (item.get("suggestion") or item.get("recommendation") or "").strip()
    if not suggestion:
        return False
    status = str(item.get("status") or item.get("response_status") or "").lower()
    high_risk = str(item.get("risk_level") or item.get("severity") or "").lower() == "high"
    return high_risk or status in _NON_COMPLIANT_STATUS


class ChapterRevisionSkill(Skill):
    name = "chapter_revision"
    description = "章节修订生成(按投标检查建议重写章节)"
    category = "generate"
    version = "1.0.0"
    triggers = ["修订", "按建议重写", "修改章节", "修改标书"]

    async def execute(self, ctx: SkillContext) -> SkillResult:
        title = ctx.parameters.get("chapter_title", "")
        original = ctx.parameters.get("original_content", "") or ""
        findings = ctx.parameters.get("findings", []) or []

        if not original.strip():
            return SkillResult(success=False, error="缺少章节原文")

        actionable = [f for f in findings if _is_actionable_finding(f)][:20]
        if not actionable:
            return SkillResult(
                success=True,
                data={"content": original, "changed": False, "reason": "该章节无可用修订建议"},
            )

        original = original.strip()
        target_words = len(original)

        findings_block = []
        for i, f in enumerate(actionable, 1):
            requirement = (f.get("requirement") or f.get("required") or f.get("check_name")
                           or f.get("title") or f.get("scoring_item") or "").strip()
            response = (f.get("response") or f.get("actual") or f.get("detail") or "").strip()
            suggestion = (f.get("suggestion") or f.get("recommendation") or f.get("fix") or "").strip()
            findings_block.append(
                f"{i}. 招标要求：{requirement}\n"
                f"   当前响应：{response}\n"
                f"   修改建议：{suggestion}"
            )
        findings_text = "\n".join(findings_block)

        system_prompt = (
            f"你是资深投标文件修订专家。请依据下面『修改建议』修订章节《{title}》。\n\n"
            f"【任务】\n"
            f"只在建议点名的范围内修改正文，其余内容尽量保持原样；逐项落实每条建议，"
            f"把不满足/部分满足/高风险的要求改成满足招标要求。建议点名的内容若原文确实缺失，"
            f"则补充一段指向该要求改写的完整内容。\n\n"
            f"【硬性约束】\n"
            f"- 保留原有标题结构（### / #### 等层级不变），不得增删节标题。\n"
            f"- 修订后总字数与原文相当（偏差控制在 ±15% 以内），原文字数约 {target_words} 字。\n"
            f"- 不得编造企业名称/资质/证书/案例/金额等事实，不得引入与本次修订无关的新内容。\n"
            f"- 不要输出章节一级标题，直接从 ### 二级节开始。\n"
            f"- 直接输出修订后的完整章节正文，不要任何解释、说明或前后缀。\n\n"
            f"【修改建议】\n{findings_text}"
        )

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": f"章节原文：\n{original[:12000]}"},
        ]

        try:
            content = await ctx.llm.chat(messages=messages, temperature=0.3, max_tokens=8192)
        except Exception as e:
            logger.error(f"[章节修订] LLM 调用失败: {e}")
            return SkillResult(success=False, error=f"修订生成失败: {e}")

        content = (content or "").strip()
        if not content:
            return SkillResult(success=False, error="修订生成为空")

        return SkillResult(
            success=True,
            data={
                "content": content,
                "changed": content != original,
                "target_words": target_words,
                "word_count": len(content),
                "findings_applied": len(actionable),
            },
        )