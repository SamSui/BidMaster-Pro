from __future__ import annotations

import json

from core.skill_engine.base import Skill, SkillContext, SkillResult


class ScoringMatrixSkill(Skill):
    name = "scoring_matrix"
    description = "构建评分矩阵"
    category = "interpret"
    version = "1.1.0"
    triggers = ["评分矩阵", "评分标准"]

    # 常见包装键，兼容不同 LLM 返回习惯
    _WRAPPER_KEYS = (
        "rows", "items", "matrix", "scoring_matrix", "scoring_items",
        "data", "result", "list", "score_items", "score_rows",
    )

    @staticmethod
    def _extract_rows(result):
        """从 LLM 返回中鲁棒提取评分项行列表。

        collect_json 强制 json_object 模式，返回必然是 dict；
        这里同时兼容 list，并处理各种包装键，避免拿到空数据。
        """
        if isinstance(result, list):
            return result
        if isinstance(result, dict):
            # 1) 常见包装键
            for k in ScoringMatrixSkill._WRAPPER_KEYS:
                v = result.get(k)
                if isinstance(v, list):
                    return v
            # 2) 任意值为 list 的字段（兜底）
            for v in result.values():
                if isinstance(v, list):
                    return v
        return []

    async def execute(self, ctx: SkillContext) -> SkillResult:
        scoring_data = ctx.parameters.get("scoring_data", {})
        if not scoring_data:
            return SkillResult(success=False, error="评分数据为空，请先解析/上传招标文件")

        messages = [
            {
                "role": "system",
                "content": """你是投标文件评分矩阵构建专家。
将下方评分标准转换为结构化评分矩阵，每个评分项一行。
返回一个 JSON 对象，唯一键为 rows，形如：
{"rows": [
  {
    "seq": 1,
    "category": "技术",
    "item": "评分项名称",
    "score": 20,
    "criteria": "评分标准描述",
    "response_section": "建议应答章节，如 3.1技术方案",
    "status": "pending"
  }
]}
注意：必须且只能返回包含 rows 数组的 JSON 对象，不要返回裸数组，也不要添加其他顶层字段。""",
            },
            {
                "role": "user",
                "content": f"评分标准数据：\n{json.dumps(scoring_data, ensure_ascii=False)}",
            },
        ]

        result = await ctx.llm.collect_json(messages=messages, temperature=0.1)

        matrix_rows = [r for r in self._extract_rows(result) if isinstance(r, dict)]
        if not matrix_rows:
            return SkillResult(
                success=False,
                error="未从评分标准中提取到评分项，请检查招标文件是否包含评分细则，或重试",
            )

        total_score = sum(row.get("score", 0) or 0 for row in matrix_rows)
        category_scores = {}
        for row in matrix_rows:
            cat = row.get("category") or "未分类"
            category_scores[cat] = category_scores.get(cat, 0) + (row.get("score", 0) or 0)

        return SkillResult(
            success=True,
            data={
                "rows": matrix_rows,
                "total_score": total_score,
                "category_scores": category_scores,
                "row_count": len(matrix_rows),
            },
        )