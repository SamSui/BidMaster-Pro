"""数据抓取器抽象层 (借鉴 AIMedia spider_all.py + article-gen RSS 哲学)

提供统一接口 BaseFetcher,支持:
- RSSFetcher: 抓取 RSS 订阅源 (最稳定)
- APIFetcher: 抓取 GitHub 等 API 接口
- HTMLFetcher: 抓取 HTML 页面 (预留扩展点)
- BrowserFetcher: 浏览器自动化 (二期实现)
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field, asdict
from typing import List, Optional
from datetime import datetime, timedelta
from urllib.parse import urlparse
import ipaddress
import socket
import requests
import feedparser

# SSRF 防护：禁止抓取内网/回环/链路本地/保留地址的数据源
_DENY_NETWORKS = [
    ipaddress.ip_network(net)
    for net in [
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16",
        "::1/128", "::/128", "fc00::/7", "fe80::/10", "2001:db8::/32",
    ]
]


def _is_denied_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
    except ValueError:
        return True
    return any(ip in net for net in _DENY_NETWORKS)


def _resolve_ips(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        return sorted({i[4][0] for i in infos})
    except socket.gaierror:
        return []


def validate_source_url(url: str) -> str:
    """校验数据源 URL：仅允许 http(s)，且解析后不得指向内网/回环/保留地址。

    返回原 URL；非法则抛 ValueError。抓取前调用，防止服务器被诱导访问内部服务（SSRF）。
    """
    p = urlparse(url)
    if p.scheme not in ("http", "https"):
        raise ValueError("数据源地址仅支持 http/https")
    host = (p.hostname or "").rstrip(".").lower()
    if not host:
        raise ValueError("数据源地址缺少有效域名")

    # 字面 IP
    try:
        ip = ipaddress.ip_address(host)
        if _is_denied_ip(str(ip)):
            raise ValueError("数据源地址指向内网/回环/保留地址，已拦截")
        return url
    except ValueError:
        if host == "localhost":
            raise ValueError("数据源地址不支持 localhost")

    # 域名：解析所有 IP，任一命中禁止段即拒绝
    ip_list = _resolve_ips(host)
    if not ip_list:
        raise ValueError(f"无法解析数据源域名: {host}")
    if any(_is_denied_ip(ip) for ip in ip_list):
        raise ValueError("数据源域名解析到内网/回环/保留地址，已拦截")
    return url


@dataclass
class NewsItem:
    """统一抓取结果"""
    title: str = ""
    url: str = ""
    source: str = ""
    pub_date: str = ""
    content: str = ""
    source_code: str = ""
    industry_code: str = ""
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        d = asdict(self)
        return {k: v for k, v in d.items() if v or k == "title"}


class FetchError(Exception):
    """抓取失败"""
    pass


class BaseFetcher(ABC):
    """抓取器基类"""
    @abstractmethod
    async def fetch(self, source_config: dict) -> List[NewsItem]:
        """根据源配置抓取,返回 NewsItem 列表"""
        raise NotImplementedError


class RSSFetcher(BaseFetcher):
    """RSS 抓取器 (借鉴 article-generation-skill)

    - 第一步:抓取 RSS feed 获取 title/url/pub_date/summary
    - 第二步 (可选):抓取详情页正文,丰富 content 字段
    """

    def __init__(self, max_age_hours: int = 168, max_items: int = 30, fetch_detail: bool = True):
        self.max_age_hours = max_age_hours
        self.max_items = max_items
        self.fetch_detail = fetch_detail

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        url = source_config.get("url", "")
        if not url:
            return []
        try:
            validate_source_url(url)
        except ValueError as e:
            raise FetchError(f"数据源地址校验未通过: {e}")

        try:
            resp = requests.get(
                url,
                timeout=15,
                headers={"User-Agent": "BidMaster-Pro/1.0 (Tender Monitor)"},
            )
            resp.raise_for_status()
            # 重定向后再次校验最终地址，防止跳转到内网
            try:
                validate_source_url(resp.url)
            except ValueError as e:
                raise FetchError(f"数据源重定向到不安全地址: {e}")
        except requests.exceptions.Timeout:
            raise FetchError(f"RSS 抓取超时: {url}")
        except requests.exceptions.RequestException as e:
            raise FetchError(f"RSS 抓取失败: {e}")

        feed = feedparser.parse(resp.content)
        items: List[NewsItem] = []
        cutoff = datetime.now() - timedelta(hours=self.max_age_hours)

        for entry in feed.entries[: self.max_items]:
            published = entry.get("published_parsed")
            if published:
                pub_time = datetime(*published[:6])
            else:
                pub_time = datetime.now()

            if pub_time < cutoff:
                continue

            entry_url = entry.get("link", "").strip()
            summary = (entry.get("summary", "") or "")[:500]

            # 第一步:基础信息
            item = NewsItem(
                title=entry.get("title", "").strip(),
                url=entry_url,
                source=source_config.get("name", ""),
                pub_date=pub_time.isoformat(),
                content=summary,
                source_code=source_config.get("code", ""),
                industry_code=source_config.get("industry", ""),
                extra={"fetch_type": "rss"},
            )

            # 第二步:抓取详情页正文(借鉴 AIMedia spider_all.py)
            if self.fetch_detail and entry_url:
                detail_content = await self._fetch_detail_content(entry_url, summary)
                if detail_content and len(detail_content) > len(summary):
                    item.content = detail_content
                    item.extra["detail_fetched"] = True
                    item.extra["content_length"] = len(detail_content)

            items.append(item)

        return items

    async def _fetch_detail_content(self, url: str, fallback: str) -> str:
        """抓取详情页正文(借鉴 NewsCrawlerSkill._extract_content)

        优先尝试 article / content / news 正文容器,
        失败时回退到 RSS summary。
        """
        try:
            resp = requests.get(
                url,
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"},
            )
            resp.raise_for_status()
            html = resp.text
        except Exception:
            return fallback

        # 借鉴 NewsCrawlerSkill 的 EXTRACT_PATTERNS
        import re
        extract_patterns = [
            r'<article[^>]*>(.*?)</article>',
            r'<div[^>]*class="[^"]*content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*article[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*id="[^"]*content[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*detail[^"]*"[^>]*>(.*?)</div>',
            r'<div[^>]*class="[^"]*news[^"]*"[^>]*>(.*?)</div>',
        ]
        re_tag = re.compile(r'<[^>]+>')
        re_ws = re.compile(r'\s+')

        for pattern in extract_patterns:
            try:
                matches = re.findall(pattern, html, re.DOTALL | re.IGNORECASE)
            except re.error:
                continue
            if matches:
                longest = max(matches, key=len)
                text = re_tag.sub('', longest)
                text = re_ws.sub(' ', text).strip()
                if len(text) > 200:
                    return text[:8000]

        # 兜底:RSS summary
        return fallback


class APIFetcher(BaseFetcher):
    """API 抓取器 (GitHub Search API 等)"""

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        code = source_config.get("code", "")
        if code == "github_tender":
            url = source_config.get("url", "https://api.github.com/search/repositories")
            try:
                validate_source_url(url)
            except ValueError as e:
                raise FetchError(f"数据源地址校验未通过: {e}")
            return await self._fetch_github_trending(source_config)
        return []

    async def _fetch_github_trending(self, config: dict) -> List[NewsItem]:
        cfg = config.get("config") or config.get("extra_config") or {}
        languages = " ".join(
            f"language:{lang}" for lang in cfg.get("languages", ["python"])
        )
        topics = " ".join(f"topic:{t}" for t in cfg.get("topics", []))
        query = f"{languages} {topics} stars:>={cfg.get('min_stars', 5)}"

        try:
            resp = requests.get(
                config.get("url", "https://api.github.com/search/repositories"),
                params={"q": query, "sort": "stars", "per_page": cfg.get("max_results", 20)},
                timeout=30,
                headers={"Accept": "application/vnd.github+json"},
            )
            resp.raise_for_status()
        except Exception as e:
            raise FetchError(f"GitHub API 抓取失败: {e}")

        data = resp.json()
        items: List[NewsItem] = []
        cutoff = datetime.now() - timedelta(hours=168)

        # 排除自身仓库 (避免 GitHub 自我命中)
        # 读取 source_config 顶层 exclude_repos 或 cfg.exclude_repos
        exclude_repos = (
            config.get("exclude_repos")
            or cfg.get("exclude_repos")
            or ["bidmaster-pro", "BidMaster-Pro", "bidmaster_pro"]  # 默认排除本项目
        )
        # 排除关键字 (title/url 中包含则过滤)
        exclude_keywords = (
            config.get("exclude_keywords")
            or cfg.get("exclude_keywords")
            or ["bidmaster"]
        )

        for repo in data.get("items", []):
            try:
                updated = datetime.strptime(repo["updated_at"], "%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                updated = datetime.now()
            if updated < cutoff:
                continue

            full_name = (repo.get("full_name") or "").lower()
            html_url = (repo.get("html_url") or "").lower()
            name = (repo.get("name") or "").lower()
            desc = (repo.get("description") or "").lower()

            # 过滤: 仓库名命中排除列表
            if any(ex.lower() in full_name or ex.lower() in name for ex in exclude_repos):
                continue
            # 过滤: 关键字命中
            if any(kw.lower() in name or kw.lower() in html_url or kw.lower() in desc for kw in exclude_keywords):
                continue

            items.append(
                NewsItem(
                    title=repo["name"],
                    url=repo["html_url"],
                    source="GitHub Trending",
                    pub_date=updated.isoformat(),
                    content=(repo.get("description") or "")[:500],
                    source_code=config.get("code", ""),
                    industry_code="12",
                    extra={
                        "fetch_type": "api",
                        "stars": repo.get("stargazers_count", 0),
                        "language": repo.get("language", ""),
                    },
                )
            )
        return items


class HTMLFetcher(BaseFetcher):
    """HTML 列表页抓取器（列表+详情双步解析）

    复用 NewsCrawlerSkill 的 HTML 解析逻辑，对真实招标/公告列表页生效，
    替代此前直接返回 [] 的占位实现。
    """

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        url = source_config.get("url", "")
        if not url:
            return []
        try:
            validate_source_url(url)
        except ValueError as e:
            raise FetchError(f"数据源地址校验未通过: {e}")

        from services.news.skills.news_crawler_skill import NewsCrawlerSkill

        raw_items = await NewsCrawlerSkill()._crawl_site(
            url, "", "", "", 3, None
        )
        items: List[NewsItem] = []
        for it in raw_items:
            items.append(NewsItem(
                title=it.get("title", ""),
                url=it.get("url", ""),
                source=source_config.get("name", ""),
                pub_date=it.get("pub_date", ""),
                content=it.get("content", ""),
                source_code=source_config.get("code", ""),
                industry_code=source_config.get("industry", source_config.get("industry_code", "12")),
                extra={"fetch_type": "html"},
            ))
        return items


class BrowserFetcher(BaseFetcher):
    """浏览器自动化抓取器 (二期实现)"""

    async def fetch(self, source_config: dict) -> List[NewsItem]:
        return []


FETCHER_REGISTRY = {
    "rss": RSSFetcher,
    "api": APIFetcher,
    "html": HTMLFetcher,
    "crawl": HTMLFetcher,  # 同 HTML
    "browser": BrowserFetcher,
}


def get_fetcher(source_type: str) -> BaseFetcher:
    fetcher_cls = FETCHER_REGISTRY.get(source_type)
    if not fetcher_cls:
        raise FetchError(f"不支持的抓取类型: {source_type}")
    return fetcher_cls()
