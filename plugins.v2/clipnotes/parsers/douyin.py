"""抖音视频解析器。

抖音视频页是客户端渲染的，普通 UA 只能拿到空壳页面；改用搜索引擎爬虫 UA 请求
同一个地址会触发服务端渲染的 SEO 版本，可以直接从 meta 信息中拿到完整文案、
话题标签与封面图。官方 JSON 接口需要签名校验，无法直接调用。
"""
from __future__ import annotations

import html as html_lib
import re
from typing import List, Optional, Tuple

import httpx

from ..models import ContentItem, Platform
from ..urlutils import normalize_asset_url
from .base import BaseParser, ParseError

# 触发 SEO 渲染的爬虫 UA，按顺序尝试
SPIDER_USER_AGENTS = (
    "Baiduspider",
    "Googlebot/2.1 (+http://www.google.com/bot.html)",
)

# 分享页地址模板，用于直接抓取视频页失败时回退
SHARE_PAGE_TEMPLATE = "https://www.iesdouyin.com/share/video/{aweme_id}/?from_ssr=1"

# 从链接中提取视频 ID
AWEME_ID_PATTERN = re.compile(r"/(?:share/)?video/(\d{6,})")

# 文案尾部的「- 作者于20261006发布在抖音……」统计串
DESC_TRAILER_PATTERN = re.compile(
    r"\s*[-–—]\s*(?P<author>[^-–—]{1,60}?)于(?P<date>\d{8})发布在抖音.*$"
)

# 统计串中的点赞数
LIKES_PATTERN = re.compile(r"已经收获了(?P<likes>[^，,。]{1,20}?)个喜欢")

# 文案中的话题标签
TAG_PATTERN = re.compile(r"#([^\s#]+)")

# SEO 页面中的封面图
COVER_PATTERN = re.compile(r"https://p\d+[^\s\"'<>]*pcweb_cover[^\s\"'<>]*")

# keywords meta 中不属于话题的固定词
KEYWORD_NOISE = {"抖音", "抖音短视频", "抖音官网"}


class DouyinParser(BaseParser):
    """抖音视频解析器。"""

    platform = Platform.DOUYIN.value
    domains = ("douyin.com", "iesdouyin.com")
    site_name = "抖音"

    async def parse(self, url: str) -> ContentItem:
        """抓取并解析抖音视频页面。

        :param url: 视频分享链接或视频页链接
        :return: 标准化内容
        :raises ParseError: 所有抓取方式都未拿到文案
        """
        errors: List[str] = []
        for agent in SPIDER_USER_AGENTS:
            try:
                page, final_url = await self._fetch_with(agent, url)
            except ParseError as err:
                errors.append(f"{agent} 抓取失败：{err.message}")
                continue
            item = self._build_item(url, final_url, page)
            if item is not None:
                return item
            errors.append(f"{agent} 未返回服务端渲染的文案")
            aweme_id = self._aweme_id(final_url) or self._aweme_id(url)
            if not aweme_id:
                continue
            try:
                page, final_url = await self._fetch_with(
                    agent, SHARE_PAGE_TEMPLATE.format(aweme_id=aweme_id)
                )
            except ParseError as err:
                errors.append(f"{agent} 分享页抓取失败：{err.message}")
                continue
            item = self._build_item(url, final_url, page)
            if item is not None:
                return item
            errors.append(f"{agent} 分享页同样未返回文案")
        raise ParseError(self.platform, "；".join(errors) or "解析失败", url)

    async def _fetch_with(self, agent: str, url: str) -> Tuple[str, str]:
        """使用指定 User-Agent 抓取页面，同时返回最终跳转地址。

        :param agent: 请求使用的 User-Agent
        :param url: 目标链接
        :return: (页面 HTML, 最终地址)
        :raises ParseError: 请求失败
        """
        headers = self.default_headers({"User-Agent": agent})
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.options.request_timeout,
                proxy=self.proxy,
            ) as client:
                response = await client.get(url, headers=headers)
        except Exception as err:
            raise ParseError(self.platform, f"请求失败：{err}", url) from err
        if response.status_code >= 400:
            raise ParseError(
                self.platform,
                "目标站点返回错误状态",
                url,
                status_code=response.status_code,
            )
        return response.text or "", str(response.url or url)

    def _build_item(self, url: str, final_url: str, page: str) -> Optional[ContentItem]:
        """从 SEO 页面中构造内容对象。

        :param url: 原始链接
        :param final_url: 跳转后的最终地址
        :param page: 页面 HTML
        :return: 标准化内容，页面未渲染时返回 None
        """
        soup = self.soup(page)
        description = self._normalize(self.meta_content(soup, "description", "og:description"))
        if not description or self._is_placeholder(description):
            return None
        body, author, publish_time, likes = self._split_description(description)
        tags = self._tags(body) or self._keywords(soup)
        title = self._title(body, tags, author)
        metadata = {
            "site": self.site_name,
            "aweme_id": self._aweme_id(final_url) or self._aweme_id(url),
            "parser": "seo_meta",
            "final_url": final_url if final_url != url else "",
            "likes": likes,
        }
        return ContentItem(
            source_url=url,
            platform=self.platform,
            title=title,
            author=author,
            publish_time=publish_time,
            content=body or title,
            images=self._cover(page),
            metadata={key: value for key, value in metadata.items() if value},
        )

    def _split_description(self, description: str) -> Tuple[str, str, str, str]:
        """拆解页面描述文案。

        :param description: meta 中的描述文本
        :return: (文案, 作者, 发布时间, 点赞数)
        """
        author = ""
        publish_time = ""
        likes = ""
        body = description
        match = DESC_TRAILER_PATTERN.search(description)
        if match:
            author = match.group("author").strip()
            publish_time = self._format_date(match.group("date"))
            body = description[: match.start()].strip()
            likes_match = LIKES_PATTERN.search(match.group(0))
            if likes_match:
                likes = likes_match.group("likes").strip()
        return body, author, publish_time, likes

    @staticmethod
    def _is_placeholder(description: str) -> bool:
        """判断是否为未渲染的占位文案。

        :param description: meta 中的描述文本
        :return: 是否为占位文案
        """
        if DESC_TRAILER_PATTERN.search(description):
            return False
        return (
            description.lstrip().startswith("于")
            and "发布在抖音" in description
            and len(description) < 80
        )

    @staticmethod
    def _tags(body: str) -> List[str]:
        """提取文案中的话题标签。

        :param body: 文案正文
        :return: 去重后的话题标签列表
        """
        tags: List[str] = []
        for tag in TAG_PATTERN.findall(body or ""):
            cleaned = tag.strip(" \t。，,、")
            if cleaned and cleaned not in tags:
                tags.append(cleaned)
        return tags

    def _keywords(self, soup) -> List[str]:
        """从 keywords meta 中提取标签。

        :param soup: BeautifulSoup 实例
        :return: 去重后的标签列表
        """
        tags: List[str] = []
        for part in re.split(r"[,，]", self.meta_content(soup, "keywords") or ""):
            cleaned = part.strip()
            if cleaned and cleaned not in KEYWORD_NOISE and cleaned not in tags:
                tags.append(cleaned)
        return tags

    @staticmethod
    def _title(body: str, tags: List[str], author: str) -> str:
        """生成标题，优先使用话题标签之前的文案。

        :param body: 文案正文
        :param tags: 话题标签列表
        :param author: 作者名称
        :return: 标题文本
        """
        text = re.sub(r"\s+", " ", TAG_PATTERN.sub(" ", body or "")).strip(" -–—·|")
        if text:
            return text[:80]
        if tags:
            return tags[0]
        return f"{author}的抖音视频" if author else "抖音视频"

    @staticmethod
    def _cover(page: str) -> List[str]:
        """提取封面图地址。

        :param page: 页面 HTML
        :return: 封面图地址列表
        """
        match = COVER_PATTERN.search(page or "")
        if not match:
            return []
        url = normalize_asset_url(html_lib.unescape(match.group(0)))
        return [url] if url else []

    @staticmethod
    def _format_date(value: str) -> str:
        """把 20261006 形式的日期转换为标准格式。

        :param value: 原始日期文本
        :return: YYYY-MM-DD 形式的日期
        """
        if len(value) != 8 or not value.isdigit():
            return ""
        return f"{value[0:4]}-{value[4:6]}-{value[6:8]}"

    @staticmethod
    def _aweme_id(url: str) -> str:
        """提取视频 ID。

        :param url: 链接
        :return: 视频 ID，未匹配时返回空字符串
        """
        match = AWEME_ID_PATTERN.search(url or "")
        return match.group(1) if match else ""

    @staticmethod
    def _normalize(text: str) -> str:
        """压缩文本中的空白字符。

        :param text: 原始文本
        :return: 处理后的文本
        """
        return re.sub(r"\s+", " ", str(text or "")).strip()
