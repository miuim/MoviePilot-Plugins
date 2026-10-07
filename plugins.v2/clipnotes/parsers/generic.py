"""通用网页解析器（兜底）。

仅提取页面标题、描述与主图，保证任何链接都能落库。
"""
from __future__ import annotations

from typing import Dict, List

from ..models import ContentItem, Platform
from ..urlutils import normalize_asset_url
from .base import BaseParser, ParseError


class GenericParser(BaseParser):
    """通用网页兜底解析器。"""

    platform = Platform.GENERIC.value
    domains = ()
    site_name = "其它"

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """兜底解析器始终可用。

        :param url: 目标链接
        :return: 始终返回 True
        """
        return True

    async def parse(self, url: str) -> ContentItem:
        """抓取页面并提取基础信息。

        :param url: 目标链接
        :return: 标准化内容
        """
        html = await self.fetch_text(url)
        soup = self.soup(html)
        title = self._pick(
            self.meta_content(soup, "og:title", "twitter:title"),
            self.text_of(soup.select_one("title")),
            self.text_of(soup.select_one("h1")),
        )
        description = self.meta_content(
            soup, "og:description", "twitter:description", "description"
        )
        image = normalize_asset_url(
            self.meta_content(soup, "og:image", "twitter:image"), base_url=url
        )
        content_node = soup.select_one("article") or soup.select_one("main")
        content = ""
        images: List[str] = []
        if content_node is not None:
            content = self.converter.convert(content_node)
            images = self.converter.extract_images(content_node, base_url=url)
        if len(content) < 40:
            content = description
        if not content and not title:
            raise ParseError(self.platform, "未能提取到任何有效内容", url)
        body_parts = [part for part in (content or title,) if part]
        metadata: Dict[str, str] = {
            "site": self.site_name,
            "description": description,
            "partial": "true",
        }
        return ContentItem(
            source_url=url,
            platform=self.platform,
            title=title,
            content="\n\n".join(dict.fromkeys(body_parts)),
            images=[image] if image else images,
            metadata={key: value for key, value in metadata.items() if value},
        )

    @staticmethod
    def _pick(*values: str) -> str:
        """返回第一个非空文本。"""
        for value in values:
            if value and str(value).strip():
                return str(value).strip()
        return ""
