from __future__ import annotations

import asyncio
import logging
from datetime import datetime, date

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, func, and_, or_

from services.database import get_db
from services.models import MonitoringTask, CrawlResult, NewsSourceRegistry, HotspotItem, Project, User, Document, DocumentType
from services.llm_factory import get_llm_gateway
from core.skill_engine.base import SkillContext
from services.middleware.rbac_middleware import get_current_user_optional
from services.middleware.api_key import require_any_auth, AuthPrincipal
from services.news.skills.news_crawler_skill import NewsCrawlerSkill
from services.news.source_registry import get_sources_by_codes

router = APIRouter()
logger = logging.getLogger(__name__)


class MonitorTaskCreate(BaseModel):
    name: str
    keywords: str
    exclude_keywords: str = ""
    must_contain_keywords: str = ""
    sites: list[str] = []
    interval_minutes: int = 60


class MonitorTaskUpdate(BaseModel):
    """编辑监控任务：仅更新传入的非空字段。"""
    name: str | None = None
    keywords: str | None = None
    exclude_keywords: str | None = None
    must_contain_keywords: str | None = None
    sites: list[str] | None = None
    enabled: bool | None = None
    interval_minutes: int | None = None


@router.get("/tasks")
async def list_monitor_tasks(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(MonitoringTask).order_by(MonitoringTask.created_at.desc()))
    tasks = result.scalars().all()
    return {"tasks": [
        {
            "id": str(t.id),
            "name": t.name,
            "keywords": t.keywords,
            "exclude_keywords": t.exclude_keywords,
            "must_contain_keywords": t.must_contain_keywords,
            "sites": t.sites,
            "interval_minutes": t.interval_minutes,
            "enabled": t.enabled,
            "last_run_at": t.last_run_at.isoformat() if t.last_run_at else None,
        }
        for t in tasks
    ]}


@router.post("/tasks")
async def create_monitor_task(
    task: MonitorTaskCreate,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    user_id = await _resolve_user_id(db, current_user=current_user)
    new_task = MonitoringTask(
        user_id=user_id,
        name=task.name,
        keywords=task.keywords,
        exclude_keywords=task.exclude_keywords,
        must_contain_keywords=task.must_contain_keywords,
        sites=task.sites,
        interval_minutes=task.interval_minutes,
    )
    db.add(new_task)
    await db.flush()
    return {
        "id": str(new_task.id),
        "name": new_task.name,
        "keywords": new_task.keywords,
        "enabled": new_task.enabled,
        "user_id": user_id,
    }


@router.patch("/tasks/{task_id}")
async def update_monitor_task(
    task_id: str,
    body: MonitorTaskUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MonitoringTask).where(MonitoringTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="监控任务不存在")

    if body.name is not None:
        task.name = body.name
    if body.keywords is not None:
        task.keywords = body.keywords
    if body.exclude_keywords is not None:
        task.exclude_keywords = body.exclude_keywords
    if body.must_contain_keywords is not None:
        task.must_contain_keywords = body.must_contain_keywords
    if body.sites is not None:
        task.sites = body.sites
    if body.enabled is not None:
        task.enabled = body.enabled
    if body.interval_minutes is not None:
        task.interval_minutes = body.interval_minutes
    await db.flush()
    await db.commit()

    return {
        "id": str(task.id),
        "name": task.name,
        "enabled": task.enabled,
        "keywords": task.keywords,
        "exclude_keywords": task.exclude_keywords,
        "must_contain_keywords": task.must_contain_keywords,
        "sites": task.sites,
    }


@router.delete("/tasks/{task_id}")
async def delete_monitor_task(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitoringTask).where(MonitoringTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="监控任务不存在")

    await db.delete(task)
    await db.flush()
    return {"success": True}


async def _save_crawl_results(db: AsyncSession, task_id: str, items: list[dict]):
    for item in items:
        existing = await db.execute(
            select(CrawlResult).where(CrawlResult.url == item.get("url", ""))
        )
        if existing.scalar_one_or_none():
            continue

        title = item.get("title", "")
        hot_score = item.get("keyword_score", 0) * 0.4 + item.get("relevance_score", 0) * 0.6
        is_hot = hot_score >= 0.5

        category = "general"
        hot_keywords = ["紧急", "限时", "截止", "今日", "最新", "公告", "变更", "更正"]
        biz_keywords = ["招标", "采购", "中标", "成交", "询价", "竞争性", "磋商", "邀请"]
        for kw in hot_keywords:
            if kw in title:
                category = "hot"
                break
        if category == "general":
            for kw in biz_keywords:
                if kw in title:
                    category = "business"
                    break

        crawl_item = CrawlResult(
            task_id=task_id,
            title=title,
            url=item.get("url", ""),
            source=item.get("source", ""),
            pub_date=item.get("pub_date", ""),
            content=item.get("content", ""),
            keyword_score=item.get("keyword_score", 0),
            relevance_score=item.get("relevance_score", 0),
            category=category,
            is_hot=is_hot,
            hot_score=hot_score,
        )
        db.add(crawl_item)
    await db.flush()


@router.post("/tasks/{task_id}/run")
async def run_monitor_task(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitoringTask).where(MonitoringTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="监控任务不存在")

    from services.news.source_registry import get_sources_by_codes
    from services.news.fetchers import get_fetcher
    from services.news.skills.news_crawler_skill import NewsCrawlerSkill

    # 解析 sites: 兼容 code 列表 (来自 4 步创建向导) 和 URL 列表 (旧格式)
    sites = task.sites or []
    source_by_code = {s.get("code"): s for s in get_sources_by_codes(sites)}

    # 按源类型路由到对应 fetcher（RSS→feedparser / API→GitHub / crawl→HTML），
    # 不再用 HTML 解析器硬套 RSS，避免 RSS 源返回 XML 却当 HTML 解析导致 0 条。
    configs: list[dict] = []
    unresolved: list[str] = []
    for s in sites:
        if not isinstance(s, str):
            continue
        if s in source_by_code:
            src = source_by_code[s]
            configs.append({
                "name": src.get("name", s), "code": s,
                "type": (src.get("type") or "rss").lower(),
                "url": src.get("url", ""),
                "industry": src.get("industry", ""),
            })
        elif s.startswith(("http://", "https://")):
            # 旧格式: 直接是 URL, 默认为 RSS
            configs.append({"name": s, "code": s, "type": "rss", "url": s, "industry": ""})
        else:
            unresolved.append(s)
    if unresolved:
        logger.warning(
            f"监控任务 {task_id} 中 {len(unresolved)} 个 site 既不是合法 code 也不是 URL,已忽略: {unresolved[:5]}"
        )

    configs = [c for c in configs if c.get("url")][:5]

    all_items: list[dict] = []
    errors: list[dict] = []
    for cfg in configs:
        try:
            fetcher = get_fetcher(cfg.get("type", "rss"))
            fetched = await fetcher.fetch(cfg)
            all_items.extend([it.to_dict() for it in fetched if it is not None])
        except Exception as e:
            name = cfg.get("name") or cfg.get("url")
            errors.append({"site": name, "error": str(e)})
            logger.warning(f"监控任务抓取源 {name} 失败: {e}")

    # 关键词过滤（与 NewsCrawlerSkill 同一套逻辑）
    filtered = NewsCrawlerSkill()._filter_results(
        all_items, task.keywords, task.exclude_keywords, task.must_contain_keywords
    )

    task.last_run_at = datetime.now()
    await _save_crawl_results(db, task_id, filtered)
    await db.flush()

    return {
        "success": True,
        "data": {
            "total_crawled": len(all_items),
            "after_filter": len(filtered),
            "results": filtered[:50],
            "errors": errors,
            "crawled_at": datetime.now().isoformat(),
        },
        "error": None,
        "resolved_sites": len(configs),
        "unresolved_sites": unresolved,
        "skipped_api_sites": [],
        "crawl_errors": errors,
    }


@router.post("/tasks/{task_id}/semantic-filter")
async def semantic_filter_results(
    task_id: str,
    company_profile: str = "",
    threshold: float = 0.6,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(MonitoringTask).where(MonitoringTask.id == task_id)
    )
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="监控任务不存在")

    crawler_skill = NewsCrawlerSkill()
    gateway = get_llm_gateway()
    # 同样解析 sites: code -> URL
    sites = task.sites or []
    source_by_code = {s.get("code"): s for s in get_sources_by_codes(sites)}
    resolved_urls: list[str] = []
    for s in sites:
        if not isinstance(s, str):
            continue
        if s in source_by_code:
            src = source_by_code[s]
            if (src.get("type") or "rss").lower() == "api":
                continue  # API 源不在监控任务里处理
            url = src.get("url", "")
            if url:
                resolved_urls.append(url)
        elif s.startswith(("http://", "https://")):
            resolved_urls.append(s)

    ctx = SkillContext(
        project_id="",
        db=db,
        llm=gateway,
        parameters={
            "task_id": task_id,
            "keywords": task.keywords,
            "exclude_keywords": task.exclude_keywords,
            "must_contain_keywords": task.must_contain_keywords,
            "sites": resolved_urls,
        },
    )
    crawl_result = await crawler_skill.safe_execute(ctx)

    if not crawl_result.success:
        return {"success": False, "error": crawl_result.error}

    items = crawl_result.data.get("results", [])

    from services.news.skills.news_crawler_skill import AISemanticFilterSkill

    filter_skill = AISemanticFilterSkill()
    filter_ctx = SkillContext(
        project_id="",
        db=db,
        llm=gateway,
        parameters={
            "items": items,
            "company_profile": company_profile,
            "threshold": threshold,
        },
    )
    filter_result = await filter_skill.safe_execute(filter_ctx)

    if filter_result.success and filter_result.data:
        filtered_items = filter_result.data.get("filtered", [])
        await _save_crawl_results(db, task_id, filtered_items)

    return {
        "success": filter_result.success,
        "data": filter_result.data,
        "error": filter_result.error,
    }


@router.get("/tasks/{task_id}/results")
async def list_task_results(task_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(CrawlResult)
        .where(CrawlResult.task_id == task_id)
        .order_by(CrawlResult.created_at.desc())
        .limit(100)
    )
    items = result.scalars().all()
    return {
        "task_id": task_id,
        "results": [
            {
                "id": str(r.id),
                "title": r.title,
                "url": r.url,
                "source": r.source,
                "pub_date": r.pub_date,
                "content": r.content[:200] if r.content else "",
                "keyword_score": r.keyword_score,
                "relevance_score": r.relevance_score,
                "category": r.category,
                "is_hot": r.is_hot,
                "hot_score": r.hot_score,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in items
        ],
    }


@router.get("/today-hot")
async def get_today_hot(
    category: str = Query("all", description="all|hot|business"),
    limit: int = Query(50, description="返回条数"),
    include_aggregated: bool = Query(True, description="是否合并聚合热点(AI媒体/通用资讯)"),
    db: AsyncSession = Depends(get_db),
    _principal: AuthPrincipal = Depends(require_any_auth),
):
    """今日热点 (监控任务结果 + 聚合热点 合并)

    - 监控任务结果来自 MonitoringTask -> CrawlResult (招投标垂类)
    - 聚合热点来自 HotspotItem (复用 AI媒体/科技 等通用资讯源)
    - 桌面端用户用 API Key 访问，Web 端用户用 Bearer Token
    """
    from services.news.classify import get_industry_name, get_industry_icon

    today_start = datetime.combine(date.today(), datetime.min.time())

    # === A. 监控任务结果 (传统流程) ===
    conditions = [CrawlResult.created_at >= today_start]
    if category == "hot":
        conditions.append(CrawlResult.category == "hot")
    elif category == "business":
        conditions.append(CrawlResult.category == "business")

    crawl_items = []
    total_crawl = 0
    count_result = await db.execute(
        select(func.count(CrawlResult.id)).where(and_(*conditions))
    )
    total_crawl = count_result.scalar() or 0

    result = await db.execute(
        select(CrawlResult)
        .where(and_(*conditions))
        .order_by(CrawlResult.hot_score.desc(), CrawlResult.created_at.desc())
        .limit(limit)
    )
    for r in result.scalars().all():
        crawl_items.append({
            "id": str(r.id),
            "title": r.title,
            "url": r.url,
            "source": r.source,
            "source_type": "monitor",
            "pub_date": r.pub_date,
            "content": (r.content or "")[:300],
            "category": r.category,
            "is_hot": r.is_hot,
            "hot_score": round(r.hot_score or 0, 2),
            "industry_code": "",
            "industry_name": "",
            "industry_icon": "",
            "created_at": r.created_at.isoformat() if r.created_at else None,
        })

    # === B. 聚合热点 (复用 AI媒体/通用资讯源) ===
    agg_items = []
    total_agg = 0
    if include_aggregated:
        agg_conditions = [HotspotItem.created_at >= today_start]
        if category == "hot":
            agg_conditions.append(HotspotItem.is_hot == True)

        agg_count_result = await db.execute(
            select(func.count(HotspotItem.id)).where(and_(*agg_conditions))
        )
        total_agg = agg_count_result.scalar() or 0

        agg_result = await db.execute(
            select(HotspotItem)
            .where(and_(*agg_conditions))
            .order_by(HotspotItem.score_total.desc(), HotspotItem.created_at.desc())
            .limit(limit)
        )
        for h in agg_result.scalars().all():
            agg_items.append({
                "id": str(h.id),
                "title": h.title,
                "url": h.url,
                "source": h.source,
                "source_type": "aggregated",
                "pub_date": h.pub_date,
                "content": (h.content or "")[:300],
                "category": "hot" if h.is_hot else "info",
                "is_hot": h.is_hot,
                "hot_score": h.score_total,
                "industry_code": h.industry_code,
                "industry_name": get_industry_name(h.industry_code),
                "industry_icon": get_industry_icon(h.industry_code[:2]),
                "created_at": h.created_at.isoformat() if h.created_at else None,
            })

    # === C. 混合排序 (hot_score 降序) ===
    merged = crawl_items + agg_items
    merged.sort(key=lambda x: (x.get("hot_score", 0), x.get("created_at") or ""), reverse=True)
    merged = merged[:limit]

    hot_count = sum(1 for i in merged if i.get("is_hot") or i.get("category") == "hot")
    biz_count = sum(1 for i in merged if i.get("category") == "business")

    return {
        "date": date.today().isoformat(),
        "total": total_crawl + total_agg,
        "total_monitor": total_crawl,
        "total_aggregated": total_agg,
        "hot_count": hot_count,
        "business_count": biz_count,
        "category": category,
        "items": merged,
    }


@router.post("/refresh-hot")
async def refresh_today_hot(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(MonitoringTask).where(MonitoringTask.enabled == True)
    )
    tasks = result.scalars().all()

    if not tasks:
        return {"success": True, "message": "没有启用的监控任务", "items_count": 0}

    gateway = get_llm_gateway()
    total_new = 0

    for task in tasks:
        try:
            from services.news.skills.news_crawler_skill import NewsCrawlerSkill

            skill = NewsCrawlerSkill()
            ctx = SkillContext(
                project_id="",
                db=db,
                llm=gateway,
                parameters={
                    "task_id": str(task.id),
                    "keywords": task.keywords,
                    "exclude_keywords": task.exclude_keywords,
                    "must_contain_keywords": task.must_contain_keywords,
                    "sites": task.sites,
                    "max_pages": 2,
                },
            )
            skill_result = await skill.safe_execute(ctx)

            if skill_result.success and skill_result.data:
                items = skill_result.data.get("results", [])
                await _save_crawl_results(db, str(task.id), items)
                total_new += len(items)

                task.last_run_at = datetime.now()
        except Exception:
            continue

    await db.flush()

    return {
        "success": True,
        "message": f"已刷新 {len(tasks)} 个监控任务，获取 {total_new} 条新结果",
        "tasks_count": len(tasks),
        "items_count": total_new,
    }


# =============================================================================
# Phase 1: 8 个新端点 (数据源 + 行业 + 聚合 + 智能推荐)
# =============================================================================


@router.get("/industries")
async def get_industries(_principal: AuthPrincipal = Depends(require_any_auth)):
    """1) 行业分类树 (前端选择器专用)"""
    from services.news.classify import list_industries_tree
    return {"industries": list_industries_tree()}


@router.get("/sources")
async def list_sources(
    industry: str | None = Query(None, description="行业 code, 空则全部"),
    enabled_only: bool = Query(False),
    db: AsyncSession = Depends(get_db),
    _principal: AuthPrincipal = Depends(require_any_auth),
):
    """2) 数据源列表 (可按行业筛选)"""
    query = select(NewsSourceRegistry)
    if industry and industry != "all":
        query = query.where(NewsSourceRegistry.industry_code == industry)
    if enabled_only:
        query = query.where(NewsSourceRegistry.enabled == True)
    query = query.order_by(
        NewsSourceRegistry.industry_code.asc(),
        NewsSourceRegistry.weight.desc(),
    )
    result = await db.execute(query)
    rows = result.scalars().all()
    return {
        "sources": [
            {
                "id": str(r.id),
                "code": r.code,
                "name": r.name,
                "type": r.type,
                "url": r.url,
                "industry_code": r.industry_code,
                "weight": r.weight,
                "enabled": r.enabled,
                "description": r.description,
                "last_crawled_at": r.last_crawled_at.isoformat() if r.last_crawled_at else None,
                "last_status": r.last_status,
            }
            for r in rows
        ],
        "total": len(rows),
    }


@router.post("/sources/sync")
async def sync_sources(db: AsyncSession = Depends(get_db)):
    """3) 重新同步 YAML -> DB (管理员手动触发)"""
    from services.news.source_registry import sync_sources_to_db, reload_sources_yaml
    reload_sources_yaml()
    synced = await sync_sources_to_db(db)
    await db.commit()
    return {"success": True, "synced": synced, "message": f"已同步 {synced} 个新源"}


class SourceToggle(BaseModel):
    enabled: bool


class SourceUpdate(BaseModel):
    """编辑数据源信息：仅更新传入的非空字段。"""
    name: str | None = None
    url: str | None = None
    description: str | None = None
    industry_code: str | None = None
    weight: float | None = None
    enabled: bool | None = None


@router.patch("/sources/{code}")
async def toggle_source_endpoint(
    code: str,
    body: SourceToggle,
    db: AsyncSession = Depends(get_db),
):
    """4) 启用/禁用某个数据源"""
    from services.news.source_registry import toggle_source
    ok = await toggle_source(db, code, body.enabled)
    if not ok:
        raise HTTPException(status_code=404, detail=f"数据源 {code} 不存在")
    await db.commit()
    return {"success": True, "code": code, "enabled": body.enabled}


@router.put("/sources/{code}")
async def update_source_endpoint(
    code: str,
    body: SourceUpdate,
    db: AsyncSession = Depends(get_db),
):
    """数据源管理：修改名称/地址/说明/行业/权重/启用状态，并写回 sources.yaml。"""
    from services.models import NewsSourceRegistry
    from services.news.source_registry import update_source_in_yaml

    result = await db.execute(
        select(NewsSourceRegistry).where(NewsSourceRegistry.code == code)
    )
    row = result.scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail=f"数据源 {code} 不存在")

    if body.name is not None:
        row.name = body.name
    if body.url is not None:
        from services.news.fetchers import validate_source_url
        try:
            validate_source_url(body.url)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        row.url = body.url
    if body.description is not None:
        row.description = body.description
    if body.industry_code is not None:
        row.industry_code = body.industry_code
    if body.weight is not None:
        row.weight = body.weight
    if body.enabled is not None:
        row.enabled = body.enabled
    await db.commit()

    # 写回 sources.yaml，保证「从YAML同步」不会还原编辑
    yaml_fields: dict = {}
    if body.name is not None:
        yaml_fields["name"] = body.name
    if body.url is not None:
        yaml_fields["url"] = body.url
    if body.description is not None:
        yaml_fields["description"] = body.description
    if body.industry_code is not None:
        yaml_fields["industry"] = body.industry_code
    if body.weight is not None:
        yaml_fields["weight"] = body.weight
    if body.enabled is not None:
        yaml_fields["enabled"] = body.enabled
    if yaml_fields:
        update_source_in_yaml(code, yaml_fields)

    return {
        "success": True,
        "code": code,
        "message": f"数据源 {code} 已更新",
    }


class AggregateRequest(BaseModel):
    source_codes: list[str] = []
    industry_code: str | None = None
    company_profile: dict | None = None
    is_hot_threshold: float = 60.0
    persist: bool = True


@router.post("/aggregate")
async def aggregate_hotspots(
    req: AggregateRequest,
    db: AsyncSession = Depends(get_db),
    _principal: AuthPrincipal = Depends(require_any_auth),
):
    """5) 一站式聚合: 多源抓取 + 去重 + 行业分类 + 评分 + 入库

    - source_codes 为空时,默认抓取所有 enabled 源
    - industry_code 配合 source_codes 进一步筛选
    - persist=True 时,结果写库 (HotspotItem)
    """
    from services.news.source_registry import get_sources_by_codes, get_sources_by_industry
    from services.news.fetchers import get_fetcher
    from services.news.skills.hotspot_aggregate_skill import HotspotAggregateSkill

    # 1) 选择数据源
    if req.source_codes:
        yaml_sources = get_sources_by_codes(req.source_codes)
    elif req.industry_code:
        yaml_sources = get_sources_by_industry(req.industry_code)
    else:
        yaml_sources = []
        enabled_rows = (await db.execute(
            select(NewsSourceRegistry).where(NewsSourceRegistry.enabled == True)
        )).scalars().all()
        for r in enabled_rows:
            cfg = {
                "name": r.name,
                "code": r.code,
                "type": r.type,
                "url": r.url,
                "industry": r.industry_code,
                "weight": r.weight,
                "config": r.extra_config or {},
            }
            yaml_sources.append(cfg)

    if not yaml_sources:
        return {"success": True, "total": 0, "saved": 0, "items": [], "message": "无可用数据源"}

    # 2) 抓取 (并发但限并发数)
    all_items: list[dict] = []
    errors: list[dict] = []
    sem = asyncio.Semaphore(4)

    async def _one(src: dict) -> list[dict]:
        async with sem:
            try:
                fetcher = get_fetcher(src.get("type", "rss"))
                items = await fetcher.fetch(src)
                return [it.to_dict() for it in items]
            except Exception as e:
                errors.append({"code": src.get("code", ""), "error": str(e)})
                return []

    results = await asyncio.gather(*[_one(s) for s in yaml_sources])
    for r in results:
        all_items.extend(r)

    # 3) 走聚合 Skill (去重+分类+评分+入库)
    gateway = get_llm_gateway()
    skill = HotspotAggregateSkill()
    ctx = SkillContext(
        project_id="",
        db=db,
        llm=gateway,
        parameters={
            "items": all_items,
            "company_profile": req.company_profile or {},
            "is_hot_threshold": req.is_hot_threshold,
            "persist": req.persist,
        },
    )
    skill_result = await skill.safe_execute(ctx)
    await db.commit()

    data = skill_result.data or {}
    return {
        "success": skill_result.success,
        "total": data.get("total", 0),
        "saved": data.get("saved", 0),
        "items": data.get("items", []),
        "errors": errors,
        "error": skill_result.error,
    }


@router.get("/hotspots")
async def list_hotspots(
    industry_code: str | None = Query(None, description="行业 code"),
    region: str | None = Query(None, description="地域关键词"),
    min_score: float = Query(0.0, description="最低综合分"),
    is_hot: bool | None = Query(None),
    keyword: str | None = Query(None, description="标题/内容关键词"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
    _principal: AuthPrincipal = Depends(require_any_auth),
):
    """6) 智能推荐: 多维过滤的聚合热点列表"""
    conditions = []
    if industry_code and industry_code != "all":
        conditions.append(HotspotItem.industry_code.like(f"{industry_code}%"))
    if region:
        conditions.append(HotspotItem.region.like(f"%{region}%"))
    if min_score > 0:
        conditions.append(HotspotItem.score_total >= min_score)
    if is_hot is not None:
        conditions.append(HotspotItem.is_hot == is_hot)
    if keyword:
        kw = f"%{keyword}%"
        conditions.append(or_(HotspotItem.title.like(kw), HotspotItem.content.like(kw)))

    where_clause = and_(*conditions) if conditions else None

    count_q = select(func.count(HotspotItem.id))
    if where_clause is not None:
        count_q = count_q.where(where_clause)
    total = (await db.execute(count_q)).scalar() or 0

    q = select(HotspotItem)
    if where_clause is not None:
        q = q.where(where_clause)
    q = q.order_by(HotspotItem.score_total.desc(), HotspotItem.created_at.desc())
    q = q.limit(limit).offset(offset)
    rows = (await db.execute(q)).scalars().all()

    return {
        "total": total,
        "limit": limit,
        "offset": offset,
        "items": [
            {
                "id": str(r.id),
                "title": r.title,
                "url": r.url,
                "source": r.source,
                "sources": r.sources or [],
                "pub_date": r.pub_date,
                "industry_code": r.industry_code,
                "region": r.region,
                "amount": r.amount,
                "bid_deadline": r.bid_deadline,
                "owner_org": r.owner_org,
                "project_code": r.project_code,
                "score_total": r.score_total,
                "is_hot": r.is_hot,
                "is_converted": r.is_converted,
                "converted_project_id": r.converted_project_id,
                "created_at": r.created_at.isoformat() if r.created_at else None,
            }
            for r in rows
        ],
    }


@router.get("/hotspots/{hotspot_id}")
async def get_hotspot_detail(
    hotspot_id: str,
    db: AsyncSession = Depends(get_db),
    _principal: AuthPrincipal = Depends(require_any_auth),
):
    """7a) 单个热点的完整详情: 原文 content + 评分 + 关键字段

    - 返回原始抓取内容 (Markdown / 纯文本)
    - 返回 5 维评分细项
    - 返回 source / sources / 关联项目信息
    """
    row = (await db.execute(
        select(HotspotItem).where(HotspotItem.id == hotspot_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="热点不存在")

    from services.news.classify import get_industry_name, get_industry_icon

    # 关联项目信息
    project_info = None
    if row.converted_project_id:
        proj = (await db.execute(
            select(Project).where(Project.id == row.converted_project_id)
        )).scalar_one_or_none()
        if proj:
            project_info = {
                "id": str(proj.id),
                "name": proj.name,
                "status": proj.status,
            }

    return {
        "id": str(row.id),
        "title": row.title,
        "url": row.url,
        "source": row.source,
        "sources": row.sources or [],
        "pub_date": row.pub_date,
        "industry_code": row.industry_code,
        "industry_name": get_industry_name(row.industry_code),
        "industry_icon": get_industry_icon(row.industry_code[:2]),
        "region": row.region,
        "amount": row.amount,
        "bid_deadline": row.bid_deadline,
        "owner_org": row.owner_org,
        "project_code": row.project_code,
        # === 详情正文 (供前端详情弹窗展示) ===
        "content": row.content or "",
        "content_length": len(row.content or ""),
        "extra": row.extra or {},
        # === 评分 ===
        "score": {
            "total": row.score_total,
            "urgency": row.score_urgency,
            "match": row.score_match,
            "amount": row.score_amount,
            "region": row.score_region,
            "freshness": row.score_freshness,
        },
        "is_hot": row.is_hot,
        "is_converted": row.is_converted,
        "converted_project_id": row.converted_project_id,
        "converted_project": project_info,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


@router.get("/hotspots/{hotspot_id}/score")
async def get_hotspot_score(
    hotspot_id: str,
    db: AsyncSession = Depends(get_db),
    _principal: AuthPrincipal = Depends(require_any_auth),
):
    """7) 单个热点的 5 维评分细项"""
    row = (await db.execute(
        select(HotspotItem).where(HotspotItem.id == hotspot_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="热点不存在")

    from services.news.classify import get_industry_name, get_industry_icon
    return {
        "id": str(row.id),
        "title": row.title,
        "url": row.url,
        "industry_code": row.industry_code,
        "industry_name": get_industry_name(row.industry_code),
        "industry_icon": get_industry_icon(row.industry_code[:2]),
        "region": row.region,
        "amount": row.amount,
        "bid_deadline": row.bid_deadline,
        "owner_org": row.owner_org,
        "score": {
            "total": row.score_total,
            "urgency": row.score_urgency,
            "match": row.score_match,
            "amount": row.score_amount,
            "region": row.score_region,
            "freshness": row.score_freshness,
        },
        "is_hot": row.is_hot,
        "is_converted": row.is_converted,
        "converted_project_id": row.converted_project_id,
    }


class ConvertToBidRequest(BaseModel):
    project_name: str | None = None
    # 兼容旧字段,实际由 current_user 或系统兜底用户决定
    user_id: str | None = None


async def _resolve_user_id(
    db: AsyncSession,
    current_user: User | None = None,
    user_id_hint: str | None = None,
) -> str:
    """解析 user_id,优先级:

    1. 已登录的 current_user (RBAC 中间件)
    2. 调用方提供的 user_id_hint (如请求体字段),且数据库中存在
    3. 数据库中任意一个可用用户 (兜底)
    4. 自动创建一个 'system@bidmaster.local' 默认用户 (最终兜底)
    """
    # 1) 优先 RBAC 当前用户
    if current_user is not None and getattr(current_user, "id", None):
        return str(current_user.id)

    # 2) 调用方传入的 user_id_hint,需校验存在
    if user_id_hint:
        try:
            result = await db.execute(select(User).where(User.id == user_id_hint))
            u = result.scalar_one_or_none()
            if u is not None:
                return str(u.id)
        except Exception:
            pass

    # 3) 数据库中任意一个用户 (按创建时间升序第一个)
    try:
        result = await db.execute(select(User).order_by(User.created_at.asc()).limit(1))
        u = result.scalar_one_or_none()
        if u is not None:
            return str(u.id)
    except Exception:
        pass

    # 4) 创建默认系统用户
    try:
        from services.models import UserRole
        default_user = User(
            email="system@bidmaster.local",
            name="系统默认",
            role=UserRole.WRITER.value,
        )
        db.add(default_user)
        await db.flush()
        return str(default_user.id)
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"无法解析或创建用户所属,数据库无用户且创建默认用户失败: {e}",
        )


@router.post("/hotspots/{hotspot_id}/convert-to-bid")
async def convert_hotspot_to_bid(
    hotspot_id: str,
    req: ConvertToBidRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User | None = Depends(get_current_user_optional),
):
    """8) 商机一键转标书: 创建项目,作为后续投标生成管线的入口

    - 创建一个 Project,关联到该热点
    - user_id 解析: RBAC > 请求体 > 数据库首用户 > 系统默认用户
    - 把热点的 url / 业主 / 行业 / 金额写入项目 config
    - 标记 HotspotItem.is_converted = True
    """
    row = (await db.execute(
        select(HotspotItem).where(HotspotItem.id == hotspot_id)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="热点不存在")
    if row.is_converted and row.converted_project_id:
        return {
            "success": True,
            "project_id": row.converted_project_id,
            "message": "该商机已转换为项目,直接返回原项目",
        }

    user_id = await _resolve_user_id(db, current_user=current_user, user_id_hint=req.user_id)

    name = req.project_name or f"【{row.title[:30]}】投标项目"
    project = Project(
        user_id=user_id,
        name=name,
        status="created",
        config={
            "from_hotspot_id": str(row.id),
            "source": row.source,
            "url": row.url,
            "industry_code": row.industry_code,
            "region": row.region,
            "amount": row.amount,
            "bid_deadline": row.bid_deadline,
            "owner_org": row.owner_org,
            "project_code": row.project_code,
        },
    )
    db.add(project)
    await db.flush()

    # 热点自带招标/商机详情( content )时，自动落一份 tender 文档并关联到项目，
    # 使得无需再上传+解析招标文件即可直接进行大纲/正文生成。
    hotspot_content = (row.content or "").strip()
    if hotspot_content:
        doc = Document(
            project_id=str(project.id),
            type=DocumentType.TENDER.value,
            file_path=f"hotspot://{row.id}",
            original_name=row.title[:200] or "资讯来源",
            parsed_content=hotspot_content,
            doc_metadata={
                "source": row.source,
                "from_hotspot": True,
                "url": row.url,
                "pub_date": row.pub_date,
            },
        )
        db.add(doc)
        await db.flush()
        project.tender_doc_id = str(doc.id)
        project.config = {**(project.config or {}), "tender_from_hotspot": True}

    row.is_converted = True
    row.converted_project_id = str(project.id)
    await db.commit()

    return {
        "success": True,
        "project_id": str(project.id),
        "project_name": name,
        "user_id": user_id,
        "message": "商机已转换为投标项目,可在项目管理中继续生成标书",
    }
