"""笔记持久化、增量同步游标与资源文件存储。

持久化说明：
- 单条笔记使用独立插件数据 key 保存，索引只保留摘要信息；
- 索引文档额外维护全局版本号 ``seq`` 与删除墓碑 ``deleted``，支撑增量同步。
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import ACTIVE_STATUSES, NoteRecord, NoteStatus, now_text
from .urlutils import guess_extension

# 游标格式版本号，未来协议变更时递增，客户端应按不透明字符串处理
CURSOR_VERSION = "v1"

# 保留的删除墓碑条数上限
MAX_TOMBSTONES = 200


def markdown_hash(markdown: str) -> str:
    """计算 Markdown 内容摘要，作为同步端的 ETag。

    :param markdown: Markdown 文本
    :return: 16 位十六进制摘要
    """
    return hashlib.sha256((markdown or "").encode("utf-8")).hexdigest()[:16]


def encode_cursor(updated_at: str, content_id: str) -> str:
    """生成不透明同步游标。

    :param updated_at: 记录更新时间
    :param content_id: 记录标识
    :return: 游标字符串
    """
    if not updated_at or not content_id:
        return ""
    return "|".join((CURSOR_VERSION, updated_at, content_id))


def decode_cursor(cursor: str) -> Tuple[str, str]:
    """解析同步游标，兼容直接传入时间戳的调试用法。

    :param cursor: 游标字符串
    :return: (更新时间, 记录标识)，记录标识可为空
    """
    value = (cursor or "").strip()
    if not value:
        return "", ""
    if value.startswith(f"{CURSOR_VERSION}|"):
        parts = value.split("|", 2)
        if len(parts) == 3:
            return parts[1], parts[2]
        return "", ""
    return value, ""


class AssetStore:
    """资源文件（图片/视频封面）本地存储。"""

    def __init__(self, base_dir: Path):
        """初始化资源存储目录。

        :param base_dir: 资源文件保存目录
        """
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)

    def path_of(self, asset_hash: str) -> Optional[Path]:
        """根据哈希定位已存在的资源文件。

        :param asset_hash: 资源哈希
        :return: 文件路径，未找到时返回 None
        """
        if not asset_hash:
            return None
        for suffix in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg"):
            candidate = self.base_dir / f"{asset_hash}{suffix}"
            if candidate.exists():
                return candidate
        return None

    def write(self, asset_hash: str, data: bytes, content_type: str = "", url: str = "") -> Path:
        """写入资源文件。

        :param asset_hash: 资源哈希
        :param data: 文件内容
        :param content_type: 响应类型
        :param url: 原始地址，用于推断扩展名
        :return: 写入后的文件路径
        """
        extension = guess_extension(url, content_type)
        path = self.base_dir / f"{asset_hash}{extension}"
        path.write_bytes(data)
        return path

    def delete(self, asset_hash: str) -> None:
        """删除资源文件。

        :param asset_hash: 资源哈希
        """
        path = self.path_of(asset_hash)
        if path and path.exists():
            path.unlink()


class NoteStorage:
    """笔记数据持久化与增量同步索引。"""

    INDEX_KEY = "notes_index"
    NOTE_PREFIX = "note_"

    def __init__(self, store: Any):
        """初始化存储。

        :param store: 提供 get_data / save_data / del_data 的对象，通常是插件实例
        """
        self.store = store

    @classmethod
    def note_key(cls, content_id: str) -> str:
        """返回笔记数据的存储 key。

        :param content_id: 笔记标识
        :return: 存储 key
        """
        return f"{cls.NOTE_PREFIX}{content_id}"

    def _load_document(self) -> Dict[str, Any]:
        """读取完整索引文档。

        :return: 含 items / seq / deleted 的索引文档
        """
        data = self.store.get_data(self.INDEX_KEY)
        document: Dict[str, Any] = {"items": {}, "seq": 0, "deleted": []}
        if isinstance(data, dict):
            raw_items = data.get("items")
            if isinstance(raw_items, dict):
                document["items"] = {
                    str(key): value for key, value in raw_items.items() if isinstance(value, dict)
                }
            else:
                # 兼容仅存 items 的历史结构
                document["items"] = {
                    str(key): value for key, value in data.items() if isinstance(value, dict)
                }
            try:
                document["seq"] = int(data.get("seq") or 0)
            except (TypeError, ValueError):
                document["seq"] = 0
            deleted = data.get("deleted")
            if isinstance(deleted, list):
                document["deleted"] = [item for item in deleted if isinstance(item, dict)]
        return document

    def _save_document(self, document: Dict[str, Any]) -> None:
        """保存完整索引文档。

        :param document: 索引文档
        """
        deleted = document.get("deleted") or []
        document["deleted"] = deleted[-MAX_TOMBSTONES:]
        self.store.save_data(self.INDEX_KEY, document)

    def load_index(self) -> Dict[str, Dict[str, Any]]:
        """读取笔记索引摘要。

        :return: content_id 到摘要的映射
        """
        return self._load_document()["items"]

    def save_index(self, index: Dict[str, Dict[str, Any]]) -> None:
        """保存笔记索引摘要（保留 items 之外的状态）。

        :param index: content_id 到摘要的映射
        """
        document = self._load_document()
        document["items"] = index
        self._save_document(document)

    def get(self, content_id: str) -> Optional[NoteRecord]:
        """读取单条笔记。

        :param content_id: 笔记标识
        :return: 笔记记录，不存在时返回 None
        """
        if not content_id:
            return None
        return NoteRecord.from_dict(self.store.get_data(self.note_key(content_id)))

    def save(self, record: NoteRecord) -> NoteRecord:
        """保存笔记并更新索引，同时递增版本号。

        :param record: 笔记记录
        :return: 保存后的笔记记录
        """
        document = self._load_document()
        record.updated_at = now_text()
        document["seq"] = int(document.get("seq") or 0) + 1
        record.revision = document["seq"]
        if record.markdown:
            record.content_hash = markdown_hash(record.markdown)
        # 先写笔记内容，再写索引，避免索引指向不存在的数据
        self.store.save_data(self.note_key(record.content_id), record.to_dict())
        document["items"][record.content_id] = record.summary()
        self._save_document(document)
        return record

    def delete(self, content_id: str) -> bool:
        """删除笔记并写入删除墓碑。

        :param content_id: 笔记标识
        :return: 是否删除成功
        """
        if not content_id:
            return False
        document = self._load_document()
        self.store.del_data(self.note_key(content_id))
        document["items"].pop(content_id, None)
        document["deleted"] = [
            item for item in document.get("deleted") or [] if item.get("content_id") != content_id
        ]
        document["deleted"].append({"content_id": content_id, "deleted_at": now_text()})
        self._save_document(document)
        return True

    def tombstones(self, since: str = "") -> List[Dict[str, Any]]:
        """查询指定游标之后的删除墓碑。

        :param since: 同步游标
        :return: 删除记录列表
        """
        since_time, _ = decode_cursor(since)
        deleted = self._load_document().get("deleted") or []
        if not since_time:
            return list(deleted)
        return [item for item in deleted if str(item.get("deleted_at") or "") > since_time]

    @staticmethod
    def _filter(items: List[Dict[str, Any]], status: Optional[str], platform: Optional[str]) -> List[Dict[str, Any]]:
        """按状态与平台过滤摘要列表。"""
        if status:
            wanted = {item.strip() for item in str(status).split(",") if item.strip()}
            items = [item for item in items if item.get("status") in wanted]
        if platform:
            wanted_platform = {item.strip() for item in str(platform).split(",") if item.strip()}
            items = [item for item in items if item.get("platform") in wanted_platform]
        return items

    def list_notes(
        self,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        since: Optional[str] = None,
        page: int = 1,
        count: int = 20,
        descending: bool = True,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """按偏移分页查询笔记摘要（浏览用）。

        :param status: 状态过滤
        :param platform: 平台过滤
        :param since: 仅返回该时间之后更新的记录
        :param page: 页码
        :param count: 每页数量
        :param descending: 是否按时间倒序
        :return: (摘要列表, 总数)
        """
        items = list(self.load_index().values())
        items = self._filter(items, status, platform)
        if since:
            items = [item for item in items if str(item.get("updated_at") or "") > str(since)]
        items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=descending)
        total = len(items)
        page = max(1, int(page or 1))
        count = max(1, min(100, int(count or 20)))
        start = (page - 1) * count
        return items[start:start + count], total

    def list_notes_since(
        self,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        since: str = "",
        limit: int = 20,
    ) -> Tuple[List[Dict[str, Any]], str, bool, int]:
        """按游标增量查询笔记摘要（同步用）。

        以 ``(updated_at, content_id)`` 元组作为 keyset 游标，按升序返回，
        保证分页期间的新增与更新都不会被跳过或重复。

        :param status: 状态过滤
        :param platform: 平台过滤
        :param since: 上次同步返回的游标
        :param limit: 本页最大条数
        :return: (摘要列表, 下一页游标, 是否还有更多, 游标之后剩余总数)
        """
        cursor_time, cursor_id = decode_cursor(since)
        items = self._filter(list(self.load_index().values()), status, platform)
        if cursor_time:
            items = [
                item
                for item in items
                if (
                    str(item.get("updated_at") or ""),
                    str(item.get("content_id") or ""),
                )
                > (cursor_time, cursor_id)
            ]
        items.sort(
            key=lambda item: (
                str(item.get("updated_at") or ""),
                str(item.get("content_id") or ""),
            )
        )
        remaining = len(items)
        limit = max(1, min(100, int(limit or 20)))
        page = items[:limit]
        has_more = len(items) > limit
        next_cursor = since
        if page:
            last = page[-1]
            next_cursor = encode_cursor(
                str(last.get("updated_at") or ""), str(last.get("content_id") or "")
            )
        return page, next_cursor, has_more, remaining

    def active_records(self, limit: int = 3) -> List[NoteRecord]:
        """按先进先出取出待处理的笔记。

        :param limit: 最大数量
        :return: 笔记记录列表
        """
        candidates = [
            item
            for item in self.load_index().values()
            if item.get("status") in ACTIVE_STATUSES
        ]
        candidates.sort(key=lambda item: str(item.get("created_at") or ""))
        records: List[NoteRecord] = []
        for item in candidates[: max(1, limit)]:
            record = self.get(str(item.get("content_id") or ""))
            if record:
                records.append(record)
        return records

    def stats(self) -> Dict[str, Any]:
        """统计各状态笔记数量。

        :return: 统计数据
        """
        counts: Dict[str, int] = {status.value: 0 for status in NoteStatus}
        platform_counts: Dict[str, int] = {}
        latest = ""
        for item in self.load_index().values():
            status = str(item.get("status") or "")
            if status in counts:
                counts[status] += 1
            platform = str(item.get("platform") or "")
            if platform:
                platform_counts[platform] = platform_counts.get(platform, 0) + 1
            created = str(item.get("created_at") or "")
            if created > latest:
                latest = created
        total = sum(counts.values())
        return {
            "total": total,
            "by_status": counts,
            "by_platform": platform_counts,
            "latest_created_at": latest,
        }

    def backfill(self) -> Dict[str, Any]:
        """为历史数据补齐版本号与内容摘要。

        仅补齐缺失字段，重复执行不会再次递增版本号，避免同步端被无意义翻新。

        :return: 补齐结果统计
        """
        document = self._load_document()
        items = [
            (str(item.get("updated_at") or ""), str(item.get("content_id") or ""))
            for item in document["items"].values()
        ]
        items.sort()
        filled = 0
        skipped = 0
        seq = int(document.get("seq") or 0)
        for _, content_id in items:
            summary = document["items"].get(content_id)
            if not summary:
                continue
            record = self.get(content_id)
            if not record:
                continue
            current_hash = markdown_hash(record.markdown)
            if record.revision > 0 and record.content_hash:
                # 已有版本号，仅同步缺失的摘要字段
                summary["revision"] = record.revision
                summary["content_hash"] = record.content_hash
                summary["assets_count"] = len(record.assets or [])
                summary["markdown_size"] = len(record.markdown or "")
                skipped += 1
                continue
            seq += 1
            record.revision = seq
            record.content_hash = current_hash
            self.store.save_data(self.note_key(content_id), record.to_dict())
            summary["revision"] = record.revision
            summary["content_hash"] = record.content_hash
            summary["assets_count"] = len(record.assets or [])
            summary["markdown_size"] = len(record.markdown or "")
            filled += 1
        document["seq"] = seq
        self._save_document(document)
        return {"filled": filled, "skipped": skipped, "seq": seq}
