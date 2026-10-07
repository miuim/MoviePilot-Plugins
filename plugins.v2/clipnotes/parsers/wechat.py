"""微信公众号文章解析器。"""
from __future__ import annotations

import re
from typing import Any, Dict, List

from bs4 import Tag

from ..models import ContentItem, Platform
from .base import BaseParser, ParseError


class WechatParser(BaseParser):
    """微信公众号文章解析器。"""

    platform = Platform.WECHAT.value
    domains = ("mp.weixin.qq.com", "weixin.qq.com")
    site_name = "微信公众号"

    # 正文中需要剔除的装饰性节点
    DROP_SELECTORS = (
        "mp-common-profile",
        "mp-common-mpaudio",
        "mp-common-videosnap",
        "mpvoice",
        "qqmusic",
        "iframe",
        "script",
        "style",
        "ul.code-snippet__line-index",
        ".code-snippet__line-index",
    )

    async def parse(self, url: str) -> ContentItem:
        """抓取并解析微信公众号文章。

        :param url: 文章链接
        :return: 标准化内容
        """
        html = await self.fetch_text(url)
        soup = self.soup(html)
        content_node = soup.select_one("#js_content") or soup.select_one("#page-content")
        if content_node is None:
            if self._is_blocked(soup):
                raise ParseError(self.platform, "页面无法直接访问，可能需要验证或文章已删除", url)
            raise ParseError(self.platform, "未找到正文节点，站点结构可能已变更", url)

        self._sanitize(content_node)
        title = self._pick(
            self.text_of(soup.select_one("#activity-name")),
            self.meta_content(soup, "og:title"),
            self.text_of(soup.select_one("h1.rich_media_title")),
        )
        author = self._pick(
            self.text_of(soup.select_one("#js_name")),
            self._regex_group(html, r'var\s+nickname\s*=\s*"([^"]+)"'),
            self.meta_content(soup, "og:article:author"),
        )
        publish_time = self._pick(
            self.text_of(soup.select_one("#publish_time")),
            self._format_timestamp(self._regex_group(html, r'var\s+ct\s*=\s*"?(\d{9,13})"?')),
        )
        content = self.converter.convert(content_node)
        content = self.ensure_content(content, url)
        images = self.converter.extract_images(content_node, base_url=url)

        metadata: Dict[str, Any] = {
            "site": self.site_name,
            "biz": self._regex_group(html, r'var\s+biz\s*=\s*"([^"]+)"'),
            "user_name": self._regex_group(html, r'var\s+user_name\s*=\s*"([^"]+)"'),
            "description": self.meta_content(soup, "og:description", "description"),
        }
        return ContentItem(
            source_url=url,
            platform=self.platform,
            title=title,
            author=author,
            publish_time=publish_time,
            content=content,
            images=images,
            metadata={key: value for key, value in metadata.items() if value},
        )

    @staticmethod
    def _pick(*values: str) -> str:
        """返回第一个非空文本。"""
        for value in values:
            if value and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _regex_group(html: str, pattern: str) -> str:
        """按正则提取第一个分组内容。"""
        match = re.search(pattern, html or "")
        return match.group(1).strip() if match else ""

    @staticmethod
    def _format_timestamp(value: str) -> str:
        """将时间戳转换为可读时间。"""
        if not value:
            return ""
        try:
            from datetime import datetime

            timestamp = int(value)
            if timestamp > 10_000_000_000:
                timestamp = timestamp // 1000
            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (TypeError, ValueError, OSError):
            return ""

    @staticmethod
    def _is_blocked(soup: Any) -> bool:
        """判断是否为无法访问的提示页面。"""
        text = soup.get_text(" ", strip=True) if soup else ""
        keywords = ("环境异常", "完成验证后即可继续访问", "该内容已被发布者删除", "此内容因违规无法查看")
        return any(keyword in text for keyword in keywords)

    def _sanitize(self, node: Any) -> None:
        """清理正文节点中的装饰性内容与隐藏节点。"""
        for selector in self.DROP_SELECTORS:
            for item in node.select(selector):
                item.decompose()
        for item in node.find_all():
            # bs4 的 Doctype/Declaration 等节点 attrs 为 None，需要跳过
            if not isinstance(item, Tag) or item.attrs is None:
                continue
            style = str(item.get("style") or "").lower()
            if "display:none" in style.replace(" ", "") or "visibility:hidden" in style.replace(" ", ""):
                item.decompose()
