"""小红书笔记解析器。

优先使用直接 HTTP 请求解析页面中的 ``window.__INITIAL_STATE__``，
失败后按顺序回退到可选第三方解析服务，最终保留链接与标题并记录失败原因。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from ..models import ContentItem, Platform
from ..urlutils import normalize_asset_url
from .base import BaseParser, ParseError


class XiaohongshuParser(BaseParser):
    """小红书笔记解析器。"""

    platform = Platform.XIAOHONGSHU.value
    domains = ("xiaohongshu.com", "xhslink.com")
    site_name = "小红书"

    async def parse(self, url: str) -> ContentItem:
        """抓取并解析小红书笔记。

        :param url: 笔记链接
        :return: 标准化内容
        """
        errors: List[str] = []
        item = await self._parse_from_html(url, errors)
        if item is None and self.options.third_party_api:
            item = await self._parse_from_third_party(url, errors)
        if item is None:
            raise ParseError(self.platform, "；".join(errors) or "解析失败", url)
        return item

    async def _parse_from_html(self, url: str, errors: List[str]) -> Optional[ContentItem]:
        """直接请求页面并解析初始状态数据。"""
        try:
            html = await self.fetch_text(url, cookie=self.options.xiaohongshu_cookie)
        except ParseError as err:
            errors.append(f"直接请求失败：{err.message}")
            return None
        state = self._extract_initial_state(html)
        if state:
            item = self._build_from_state(url, state)
            if item:
                return item
            errors.append("初始状态中未找到笔记详情")
        else:
            errors.append("页面中未找到 __INITIAL_STATE__ 数据")
        fallback = self._build_from_meta(url, html)
        if fallback:
            return fallback
        return None

    async def _parse_from_third_party(self, url: str, errors: List[str]) -> Optional[ContentItem]:
        """调用可选的第三方解析服务。"""
        api = self.options.third_party_api
        if "{url}" in api:
            target = api.replace("{url}", url)
            params = None
        else:
            target = api
            params = {"url": url}
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.options.request_timeout,
                proxy=self.proxy,
            ) as client:
                response = await client.get(
                    target,
                    params=params,
                    headers=self.default_headers(),
                )
            response.raise_for_status()
            payload = response.json()
        except Exception as err:
            errors.append(f"第三方解析服务调用失败：{err}")
            return None
        if not isinstance(payload, dict):
            errors.append("第三方解析服务返回格式不正确")
            return None
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        title = str(data.get("title") or "").strip()
        content = str(data.get("content") or data.get("desc") or "").strip()
        images = data.get("images") or []
        if not isinstance(images, list):
            images = [images]
        if not title and not content:
            errors.append("第三方解析服务未返回有效内容")
            return None
        return ContentItem(
            source_url=url,
            platform=self.platform,
            title=title,
            author=str(data.get("author") or "").strip(),
            publish_time=str(data.get("publish_time") or "").strip(),
            content=content,
            images=[str(image).strip() for image in images if str(image).strip()],
            video=str(data.get("video") or "").strip(),
            metadata={"site": self.site_name, "parser": "third_party"},
        )

    def _extract_initial_state(self, html: str) -> Optional[Dict[str, Any]]:
        """从页面 HTML 中提取 ``window.__INITIAL_STATE__`` 字典。"""
        marker = "window.__INITIAL_STATE__"
        index = (html or "").find(marker)
        if index < 0:
            return None
        start = html.find("{", index)
        if start < 0:
            return None
        depth = 0
        in_string = False
        quote = ""
        escaped = False
        end = -1
        for position in range(start, len(html)):
            char = html[position]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    in_string = False
                continue
            if char in ("'", '"'):
                in_string = True
                quote = char
                continue
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = position
                    break
        if end < 0:
            return None
        raw = html[start:end + 1].replace("undefined", "null")
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return None

    def _build_from_state(self, url: str, state: Dict[str, Any]) -> Optional[ContentItem]:
        """根据初始状态数据构造内容对象。"""
        note = self._find_note(state)
        if not note:
            return None
        desc = str(note.get("desc") or "").strip()
        title = str(note.get("title") or "").strip() or desc.splitlines()[0][:60]
        user = note.get("user") if isinstance(note.get("user"), dict) else {}
        # 标题已由 frontmatter 与 H1 承载，正文只保留正文描述
        content = desc or title
        images = self._collect_images(note)
        video = self._collect_video(note)
        metadata: Dict[str, Any] = {
            "site": self.site_name,
            "note_id": str(note.get("noteId") or note.get("id") or ""),
            "type": str(note.get("type") or ""),
            "parser": "initial_state",
        }
        publish_time = self._format_timestamp(note.get("time"))
        if publish_time:
            metadata["publish_time_raw"] = str(note.get("time"))
        return ContentItem(
            source_url=url,
            platform=self.platform,
            title=title,
            author=str(user.get("nickname") or user.get("nickName") or "").strip(),
            publish_time=publish_time,
            content=content,
            images=images,
            video=video,
            metadata={key: value for key, value in metadata.items() if value},
        )

    @staticmethod
    def _find_note(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """在初始状态中定位笔记详情。"""
        note_root = state.get("note")
        if not isinstance(note_root, dict):
            return None
        detail_map = note_root.get("noteDetailMap")
        if isinstance(detail_map, dict):
            first_note_id = note_root.get("firstNoteId")
            candidates = []
            if first_note_id and isinstance(detail_map.get(first_note_id), dict):
                candidates.append(detail_map[first_note_id])
            candidates.extend(
                value for key, value in detail_map.items() if key != first_note_id and isinstance(value, dict)
            )
            for candidate in candidates:
                note = candidate.get("note")
                if isinstance(note, dict):
                    return note
        note = note_root.get("noteDetail")
        if isinstance(note, dict):
            return note
        return None

    def _collect_images(self, note: Dict[str, Any]) -> List[str]:
        """收集笔记图片地址。"""
        images: List[str] = []
        for entry in note.get("imageList") or []:
            if isinstance(entry, str):
                url = entry
            elif isinstance(entry, dict):
                url = (
                    entry.get("urlDefault")
                    or entry.get("urlPre")
                    or entry.get("url")
                    or self._pick_from_info_list(entry.get("infoList"))
                )
            else:
                url = ""
            url = normalize_asset_url(str(url) if url else "")
            if url and url not in images:
                images.append(url)
        return images

    @staticmethod
    def _pick_from_info_list(info_list: Any) -> str:
        """从图片信息列表中挑选默认尺寸地址。"""
        if not isinstance(info_list, list):
            return ""
        for entry in info_list:
            if isinstance(entry, dict) and entry.get("url"):
                return str(entry.get("url"))
        return ""

    @staticmethod
    def _collect_video(note: Dict[str, Any]) -> str:
        """收集笔记视频地址。"""
        video = note.get("video")
        if not isinstance(video, dict):
            return ""
        media = video.get("media")
        if not isinstance(media, dict):
            return ""
        stream = media.get("stream")
        if not isinstance(stream, dict):
            return ""
        for codec in ("h264", "h265", "av1"):
            entries = stream.get(codec)
            if isinstance(entries, list):
                for entry in entries:
                    if isinstance(entry, dict) and entry.get("masterUrl"):
                        return str(entry.get("masterUrl"))
        return ""

    @staticmethod
    def _format_timestamp(value: Any) -> str:
        """将毫秒时间戳转换为可读时间。"""
        try:
            timestamp = int(value)
        except (TypeError, ValueError):
            return ""
        if timestamp <= 0:
            return ""
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        try:
            from datetime import datetime

            return datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            return ""

    def _build_from_meta(self, url: str, html: str) -> Optional[ContentItem]:
        """使用页面 meta 信息兜底构造内容对象。"""
        soup = self.soup(html)
        title = self._pick(
            self.meta_content(soup, "og:title"),
            self.text_of(soup.select_one("title")),
        )
        description = self.meta_content(soup, "og:description", "description")
        image = normalize_asset_url(self.meta_content(soup, "og:image"), base_url=url)
        if not title and not description:
            return None
        content = "\n\n".join(part for part in (title, description) if part)
        return ContentItem(
            source_url=url,
            platform=self.platform,
            title=title,
            content=content,
            images=[image] if image else [],
            metadata={"site": self.site_name, "parser": "meta_only", "partial": True},
        )

    @staticmethod
    def _pick(*values: str) -> str:
        """返回第一个非空文本。"""
        for value in values:
            if value and str(value).strip():
                return str(value).strip()
        return ""
