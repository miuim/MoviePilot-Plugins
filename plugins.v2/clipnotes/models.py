"""ClipNotes 插件的数据模型定义。

解析器、AI 整理、Markdown 渲染与对外 API 都只依赖本模块定义的数据结构，
不直接依赖具体站点的 HTML 结构。
"""
from __future__ import annotations

import enum
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from pydantic import BaseModel


def now_text() -> str:
    """返回当前时间的标准文本表示。"""
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class Platform(str, enum.Enum):
    """内容来源平台。"""

    WECHAT = "wechat"
    XIAOHONGSHU = "xiaohongshu"
    DOUYIN = "douyin"
    GENERIC = "generic"

    @property
    def label(self) -> str:
        """返回平台的中文名称。"""
        return PLATFORM_LABELS.get(self.value, self.value)


# 平台标识到中文名称的映射
PLATFORM_LABELS: Dict[str, str] = {
    Platform.WECHAT.value: "微信",
    Platform.XIAOHONGSHU.value: "小红书",
    Platform.DOUYIN.value: "抖音",
    Platform.GENERIC.value: "其它",
}


class NoteStatus(str, enum.Enum):
    """笔记处理状态。"""

    RECEIVED = "received"
    PARSING = "parsing"
    AI_PENDING = "ai_pending"
    READY = "ready"
    SYNCED = "synced"
    FAILED = "failed"

    @property
    def label(self) -> str:
        """返回状态的中文名称。"""
        return STATUS_LABELS.get(self.value, self.value)


# 状态标识到中文名称的映射
STATUS_LABELS: Dict[str, str] = {
    NoteStatus.RECEIVED.value: "已接收",
    NoteStatus.PARSING.value: "抓取中",
    NoteStatus.AI_PENDING.value: "AI 整理中",
    NoteStatus.READY.value: "待同步",
    NoteStatus.SYNCED.value: "已同步",
    NoteStatus.FAILED.value: "失败",
}


# 需要后台继续处理的活跃状态
ACTIVE_STATUSES: tuple = (
    NoteStatus.RECEIVED.value,
    NoteStatus.PARSING.value,
    NoteStatus.AI_PENDING.value,
)


@dataclass
class ContentItem:
    """标准化后的文章内容。

    所有解析器的统一输出，后续 AI 整理、Markdown 渲染只处理该结构。
    """

    source_url: str
    platform: str
    title: str = ""
    author: str = ""
    publish_time: str = ""
    content: str = ""
    images: List[str] = field(default_factory=list)
    video: str = ""
    metadata: Dict[str, Any] = field(default_factory=dict)

    @property
    def platform_label(self) -> str:
        """返回平台中文名称。"""
        return PLATFORM_LABELS.get(self.platform, self.platform)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可持久化的字典。"""
        return {
            "source_url": self.source_url,
            "platform": self.platform,
            "title": self.title,
            "author": self.author,
            "publish_time": self.publish_time,
            "content": self.content,
            "images": list(self.images),
            "video": self.video,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["ContentItem"]:
        """从字典还原内容对象。"""
        if not data:
            return None
        return cls(
            source_url=str(data.get("source_url") or ""),
            platform=str(data.get("platform") or Platform.GENERIC.value),
            title=str(data.get("title") or ""),
            author=str(data.get("author") or ""),
            publish_time=str(data.get("publish_time") or ""),
            content=str(data.get("content") or ""),
            images=list(data.get("images") or []),
            video=str(data.get("video") or ""),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class AssetRef:
    """图片等资源文件引用。"""

    hash: str
    source_url: str
    filename: str
    downloaded: bool = False
    size: int = 0
    content_type: str = ""
    error: str = ""

    def to_dict(self) -> Dict[str, Any]:
        """转换为可持久化的字典。"""
        return {
            "hash": self.hash,
            "source_url": self.source_url,
            "filename": self.filename,
            "downloaded": self.downloaded,
            "size": self.size,
            "content_type": self.content_type,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["AssetRef"]:
        """从字典还原资源引用。"""
        if not data:
            return None
        return cls(
            hash=str(data.get("hash") or ""),
            source_url=str(data.get("source_url") or ""),
            filename=str(data.get("filename") or ""),
            downloaded=bool(data.get("downloaded")),
            size=int(data.get("size") or 0),
            content_type=str(data.get("content_type") or ""),
            error=str(data.get("error") or ""),
        )


@dataclass
class NoteRecord:
    """一条笔记的完整记录。"""

    content_id: str
    source_url: str
    platform: str
    status: str = NoteStatus.RECEIVED.value
    created_at: str = ""
    updated_at: str = ""
    attempts: int = 0
    error: str = ""
    ai: Optional[Dict[str, Any]] = None
    markdown: str = ""
    item: Optional[Dict[str, Any]] = None
    assets: List[Dict[str, Any]] = field(default_factory=list)
    submit_user: str = ""
    submit_channel: str = ""
    submit_source: str = ""
    synced_at: str = ""
    # 单调递增的版本号，每次保存自增，用于增量同步定位变化
    revision: int = 0
    # Markdown 内容摘要，作为同步端的 ETag，内容未变时不重复拉取
    content_hash: str = ""
    notified: bool = False

    @property
    def title(self) -> str:
        """返回用于展示的标题，优先使用 AI 整理后的标题。"""
        if self.ai and self.ai.get("title"):
            return str(self.ai.get("title"))
        if self.item and self.item.get("title"):
            return str(self.item.get("title"))
        return self.source_url

    @property
    def origin_title(self) -> str:
        """返回原始标题。"""
        if self.item and self.item.get("title"):
            return str(self.item.get("title"))
        return ""

    @property
    def platform_label(self) -> str:
        """返回平台中文名称。"""
        return PLATFORM_LABELS.get(self.platform, self.platform)

    @property
    def status_label(self) -> str:
        """返回状态中文名称。"""
        return STATUS_LABELS.get(self.status, self.status)

    def to_dict(self) -> Dict[str, Any]:
        """转换为可持久化的字典。"""
        return {
            "content_id": self.content_id,
            "source_url": self.source_url,
            "platform": self.platform,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "attempts": self.attempts,
            "error": self.error,
            "ai": dict(self.ai) if self.ai else None,
            "markdown": self.markdown,
            "item": dict(self.item) if self.item else None,
            "assets": list(self.assets),
            "submit_user": self.submit_user,
            "submit_channel": self.submit_channel,
            "submit_source": self.submit_source,
            "synced_at": self.synced_at,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "notified": self.notified,
        }

    def summary(self) -> Dict[str, Any]:
        """返回用于索引的摘要信息。"""
        return {
            "content_id": self.content_id,
            "source_url": self.source_url,
            "platform": self.platform,
            "status": self.status,
            "title": self.title,
            "category": (self.ai or {}).get("category", ""),
            "tags": (self.ai or {}).get("tags", []),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "error": self.error,
            "submit_user": self.submit_user,
            "revision": self.revision,
            "content_hash": self.content_hash,
            "assets_count": len(self.assets or []),
            "markdown_size": len(self.markdown or ""),
        }

    @classmethod
    def from_dict(cls, data: Optional[Dict[str, Any]]) -> Optional["NoteRecord"]:
        """从字典还原笔记记录。"""
        if not data:
            return None
        return cls(
            content_id=str(data.get("content_id") or ""),
            source_url=str(data.get("source_url") or ""),
            platform=str(data.get("platform") or Platform.GENERIC.value),
            status=str(data.get("status") or NoteStatus.RECEIVED.value),
            created_at=str(data.get("created_at") or ""),
            updated_at=str(data.get("updated_at") or ""),
            attempts=int(data.get("attempts") or 0),
            error=str(data.get("error") or ""),
            ai=dict(data.get("ai")) if data.get("ai") else None,
            markdown=str(data.get("markdown") or ""),
            item=dict(data.get("item")) if data.get("item") else None,
            assets=list(data.get("assets") or []),
            submit_user=str(data.get("submit_user") or ""),
            submit_channel=str(data.get("submit_channel") or ""),
            submit_source=str(data.get("submit_source") or ""),
            synced_at=str(data.get("synced_at") or ""),
            revision=int(data.get("revision") or 0),
            content_hash=str(data.get("content_hash") or ""),
            notified=bool(data.get("notified")),
        )

    @classmethod
    def new_from_url(cls, content_id: str, source_url: str, platform: str) -> "NoteRecord":
        """创建一条新的笔记记录。"""
        created = now_text()
        return cls(
            content_id=content_id,
            source_url=source_url,
            platform=platform,
            status=NoteStatus.RECEIVED.value,
            created_at=created,
            updated_at=created,
        )


# 默认的 Obsidian 顶层分类，AI 只允许在其中选择
DEFAULT_CATEGORIES: List[str] = [
    "Inbox",
    "NAS",
    "开发",
    "软件",
    "生活",
    "知识",
    "说明书",
    "项目",
    "Obsidian",
    "Archive",
]

# 微信菜单默认把本插件命令并入的分类（企业微信一级菜单只有 3 个位置）
DEFAULT_WECHAT_MENU_CATEGORY = "订阅"


@dataclass
class ClipNotesOptions:
    """插件运行参数集合。"""

    categories: List[str] = field(default_factory=lambda: list(DEFAULT_CATEGORIES))
    default_status: str = "待读"
    worker_interval: int = 30
    batch_size: int = 3
    max_attempts: int = 3
    request_timeout: int = 20
    ai_max_chars: int = 4000
    llm_provider: str = ""
    llm_model: str = ""
    llm_temperature: float = 0.2
    enable_wechat: bool = True
    enable_xiaohongshu: bool = True
    enable_douyin: bool = True
    xiaohongshu_cookie: str = ""
    download_images: bool = True
    third_party_api: str = ""
    user_agent: str = ""
    # 企业微信菜单中本插件命令并入的分类，留空则独立占用一个一级菜单
    wechat_menu_category: str = DEFAULT_WECHAT_MENU_CATEGORY

    @classmethod
    def from_config(cls, config: Optional[Dict[str, Any]]) -> "ClipNotesOptions":
        """从插件配置构建运行参数。"""
        config = config or {}

        def _bool(key: str, default: bool) -> bool:
            """读取布尔型配置。"""
            value = config.get(key)
            return default if value is None else bool(value)

        def _int(key: str, default: int) -> int:
            """读取整型配置。"""
            try:
                value = int(config.get(key))
                return value if value > 0 else default
            except (TypeError, ValueError):
                return default

        raw_categories = str(config.get("categories") or "").strip()
        if raw_categories:
            categories = [
                line.strip().lstrip("-").strip()
                for line in raw_categories.splitlines()
                if line.strip()
            ]
        else:
            categories = list(DEFAULT_CATEGORIES)

        try:
            temperature = float(config.get("llm_temperature") or 0.2)
        except (TypeError, ValueError):
            temperature = 0.2

        return cls(
            categories=categories or list(DEFAULT_CATEGORIES),
            default_status=str(config.get("default_status") or "待读").strip() or "待读",
            worker_interval=max(10, _int("worker_interval", 30)),
            batch_size=max(1, _int("batch_size", 3)),
            max_attempts=max(1, _int("max_attempts", 3)),
            request_timeout=max(5, _int("request_timeout", 20)),
            ai_max_chars=max(500, _int("ai_max_chars", 4000)),
            llm_provider=str(config.get("llm_provider") or "").strip(),
            llm_model=str(config.get("llm_model") or "").strip(),
            llm_temperature=temperature,
            enable_wechat=_bool("enable_wechat", True),
            enable_xiaohongshu=_bool("enable_xiaohongshu", True),
            enable_douyin=_bool("enable_douyin", True),
            xiaohongshu_cookie=str(config.get("xiaohongshu_cookie") or "").strip(),
            download_images=_bool("download_images", True),
            third_party_api=str(config.get("third_party_api") or "").strip(),
            user_agent=str(config.get("user_agent") or "").strip(),
            wechat_menu_category=str(
                config.get("wechat_menu_category", DEFAULT_WECHAT_MENU_CATEGORY) or ""
            ).strip(),
        )


class SubmitRequest(BaseModel):
    """API 提交链接请求体。"""

    url: str


class AckRequest(BaseModel):
    """API 同步确认请求体。"""

    status: str = NoteStatus.SYNCED.value
    note: Optional[str] = None
    # 客户端同步时对应的版本号与内容摘要，用于校验是否同步了旧版本
    revision: Optional[int] = None
    content_hash: Optional[str] = None
