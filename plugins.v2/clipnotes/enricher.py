"""基于 MoviePilot 现有 LLM 能力的结构化整理器。

只负责把已抓取的内容整理为固定结构，不参与网页抓取。
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .models import ClipNotesOptions, ContentItem

# AI 输出的字段说明
OUTPUT_FIELDS = ("title", "summary", "category", "tags", "language")

# 标签数量上限
MAX_TAGS = 8


class EnrichError(Exception):
    """AI 整理失败异常。"""

    def __init__(self, message: str):
        """初始化异常。

        :param message: 失败原因
        """
        super().__init__(message)
        self.message = message


class AIEnricher:
    """使用系统 LLM 对内容做结构化整理。"""

    def __init__(self, options: Optional[ClipNotesOptions] = None):
        """初始化整理器。

        :param options: 插件运行参数
        """
        self.options = options or ClipNotesOptions()

    async def enrich(self, item: ContentItem) -> Dict[str, Any]:
        """整理内容并返回结构化结果。

        :param item: 已抓取的内容
        :return: 结构化整理结果
        :raises EnrichError: 调用或解析失败
        """
        if item is None:
            raise EnrichError("缺少待整理内容")
        prompt = self.build_prompt(item)
        llm = await self._get_llm()
        try:
            response = await llm.ainvoke(prompt)
        except Exception as err:
            raise EnrichError(f"LLM 调用失败：{err}") from err
        text = self._message_text(response)
        payload = self._parse_json(text)
        if not payload:
            raise EnrichError("LLM 未返回可解析的 JSON 结果")
        return self.normalize(payload, item)

    async def _get_llm(self) -> Any:
        """获取 MoviePilot 当前配置的 LLM 实例。

        :return: LangChain 模型实例
        :raises EnrichError: 获取失败
        """
        try:
            from app.agent.llm.helper import LLMHelper
        except Exception as err:
            raise EnrichError(f"无法加载 MoviePilot LLM 模块：{err}") from err
        kwargs: Dict[str, Any] = {
            "streaming": False,
            "temperature": self.options.llm_temperature,
        }
        if self.options.llm_provider:
            kwargs["provider"] = self.options.llm_provider
        if self.options.llm_model:
            kwargs["model"] = self.options.llm_model
        try:
            return await LLMHelper.get_llm(**kwargs)
        except Exception as err:
            raise EnrichError(f"获取 LLM 实例失败：{err}") from err

    def build_prompt(self, item: ContentItem) -> str:
        """构造整理提示词。

        :param item: 已抓取的内容
        :return: 提示词
        """
        categories = "、".join(self.options.categories)
        excerpt = (item.content or "").strip()[: self.options.ai_max_chars]
        headings = self._extract_headings(item.content)
        parts = [
            "你是一名中文内容整理助手。请阅读下面的文章信息，输出整理结果。",
            "要求：",
            "1. 只输出一个 JSON 对象，不要输出解释、Markdown 代码块或多余文字。",
            '2. JSON 字段固定为：{"title": "...", "summary": "...", "category": "...", "tags": ["..."], "language": "..."}。',
            "3. title：去掉标题党、夸张语气与表情符号，用简洁准确的中文概括文章内容，不超过 40 字。",
            "4. summary：1~3 句中文摘要，说明文章讲了什么、结论是什么，不超过 150 字。",
            f"5. category：必须从以下列表中精确选择一个，不得自创新分类：{categories}。",
            f"6. tags：2~{MAX_TAGS} 个中文关键词。",
            '7. language：文章主体语言，如 "zh-CN"。',
            "",
            f"原始标题：{item.title or '（无）'}",
            f"来源平台：{item.platform_label}",
            f"作者：{item.author or '（无）'}",
        ]
        if headings:
            parts.append(f"正文小标题：{headings}")
        parts.append(f"正文节选（最多 {self.options.ai_max_chars} 字）：")
        parts.append(excerpt or "（正文为空，请仅依据标题整理）")
        return "\n".join(parts)

    @staticmethod
    def _extract_headings(content: str) -> str:
        """提取正文小标题，帮助模型快速把握结构。

        :param content: Markdown 正文
        :return: 小标题拼接文本
        """
        headings = re.findall(r"^#{1,4}\s+(.+)$", content or "", flags=re.MULTILINE)
        cleaned = [item.strip() for item in headings if item.strip()]
        return " / ".join(cleaned[:12])

    @staticmethod
    def _message_text(response: Any) -> str:
        """从 LangChain 响应中提取文本。

        :param response: LLM 响应
        :return: 文本内容
        """
        if response is None:
            return ""
        content = getattr(response, "content", response)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: List[str] = []
            for entry in content:
                if isinstance(entry, str):
                    parts.append(entry)
                elif isinstance(entry, dict) and entry.get("text"):
                    parts.append(str(entry.get("text")))
            return "\n".join(parts)
        return str(content)

    @staticmethod
    def _parse_json(text: str) -> Optional[Dict[str, Any]]:
        """从模型输出中提取 JSON 对象。

        :param text: 模型输出文本
        :return: 解析结果，失败时返回 None
        """
        if not text:
            return None
        cleaned = text.strip()
        cleaned = re.sub(r"^```(?:json)?", "", cleaned).strip()
        cleaned = re.sub(r"```$", "", cleaned).strip()
        try:
            payload = json.loads(cleaned)
            return payload if isinstance(payload, dict) else None
        except json.JSONDecodeError:
            pass
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        if start < 0 or end <= start:
            return None
        try:
            payload = json.loads(cleaned[start:end + 1])
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None

    def normalize(self, payload: Dict[str, Any], item: ContentItem) -> Dict[str, Any]:
        """校验并规范化模型输出。

        :param payload: 模型返回的字典
        :param item: 原始内容
        :return: 规范化后的整理结果
        """
        title = str(payload.get("title") or "").strip() or (item.title or "").strip()
        summary = str(payload.get("summary") or "").strip()
        category = str(payload.get("category") or "").strip()
        if category not in self.options.categories:
            category = "Inbox" if "Inbox" in self.options.categories else self.options.categories[0]
        tags: List[str] = []
        raw_tags = payload.get("tags")
        if isinstance(raw_tags, str):
            raw_tags = re.split(r"[,，、;；\s]+", raw_tags)
        if isinstance(raw_tags, list):
            for tag in raw_tags:
                text = str(tag).strip()
                if text and text not in tags:
                    tags.append(text)
        language = str(payload.get("language") or "").strip() or "zh-CN"
        return {
            "title": title,
            "summary": summary,
            "category": category,
            "tags": tags[:MAX_TAGS],
            "language": language,
            "model": self.options.llm_model or "default",
        }
