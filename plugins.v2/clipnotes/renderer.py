"""Obsidian 风格 Markdown 渲染器。"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from .models import ClipNotesOptions, NoteRecord, now_text

# 文件名中不允许出现的字符
ILLEGAL_FILENAME_CHARS = r'[\\/:*?"<>|\r\n\t]'

# 文件名最大长度
MAX_FILENAME_LENGTH = 80

# 可安全作为 YAML 纯量输出的字符集合
SAFE_SCALAR_PATTERN = re.compile(
    r"^[A-Za-z0-9\u4e00-\u9fff]"
    r"[A-Za-z0-9\u4e00-\u9fff .,_\-:()（）/！？，。、；：“”‘’…—·]*$"
)


def render_markdown(record: NoteRecord, options: Optional[ClipNotesOptions] = None) -> str:
    """将笔记记录渲染为 Markdown 文本。

    :param record: 笔记记录
    :param options: 插件运行参数
    :return: Markdown 文本
    """
    options = options or ClipNotesOptions()
    item = record.item or {}
    ai = record.ai or {}
    title = str(ai.get("title") or item.get("title") or record.source_url).strip()
    tags = [str(tag) for tag in (ai.get("tags") or []) if str(tag).strip()]

    lines: List[str] = ["---"]
    lines.append(f"标题: {_scalar(title)}")
    lines.append(f"原始标题: {_scalar(item.get('title') or '')}")
    lines.append(f"来源: {_scalar(record.platform_label)}")
    lines.append(f"URL: {_scalar(record.source_url)}")
    lines.append(f"作者: {_scalar(item.get('author') or '')}")
    lines.append(f"发布时间: {_scalar(item.get('publish_time') or '')}")
    lines.append(f"分类: {_scalar(ai.get('category') or 'Inbox')}")
    lines.append(f"状态: {_scalar(options.default_status)}")
    if tags:
        lines.append("标签:")
        lines.extend([f"  - {_scalar(tag)}" for tag in tags])
    else:
        lines.append("标签: []")
    lines.append(f"摘要: {_scalar(ai.get('summary') or '')}")
    lines.append(f"content_id: {_scalar(record.content_id)}")
    lines.append(f"抓取时间: {_scalar(record.created_at or now_text())}")
    lines.append(f"整理时间: {_scalar(record.updated_at or now_text())}")
    lines.append("---")
    lines.append("")

    if title:
        lines.append(f"# {title}")
        lines.append("")
    summary = str(ai.get("summary") or "").strip()
    if summary:
        lines.append(f"> {summary}")
        lines.append("")

    body = str(item.get("content") or "").strip()
    if body:
        lines.append(body)
        lines.append("")
    else:
        lines.append("> 未抓取到正文内容，请点击原文链接查看。")
        lines.append("")

    video = str(item.get("video") or "").strip()
    if video:
        lines.append(f"视频：[{video}]({video})")
        lines.append("")

    images = [str(image) for image in (item.get("images") or []) if str(image).strip()]
    if images and len(images) > 3:
        lines.append("## 图片")
        lines.append("")
        for image in images:
            lines.append(f"![]({image})")
        lines.append("")

    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip() + "\n"


def build_filename(record: NoteRecord) -> str:
    """生成建议的 Markdown 文件名（供未来的 Obsidian 插件参考）。

    :param record: 笔记记录
    :return: 安全的文件名（不含扩展名）
    """
    ai = record.ai or {}
    item = record.item or {}
    title = str(ai.get("title") or item.get("title") or record.content_id).strip()
    name = re.sub(ILLEGAL_FILENAME_CHARS, " ", title)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > MAX_FILENAME_LENGTH:
        name = name[:MAX_FILENAME_LENGTH].strip()
    return name or record.content_id


def _scalar(value: Any) -> str:
    """将值序列化为 YAML 标量。

    :param value: 待序列化内容
    :return: YAML 标量文本
    """
    text = "" if value is None else str(value).replace("\r\n", " ").replace("\n", " ").strip()
    if not text:
        return '""'
    if SAFE_SCALAR_PATTERN.match(text) and ": " not in text and not text.endswith(":"):
        return text
    return json.dumps(text, ensure_ascii=False)
