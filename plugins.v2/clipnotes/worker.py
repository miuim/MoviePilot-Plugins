"""后台处理流程：抓取 → 图片落盘 → AI 整理 → Markdown 渲染。"""
from __future__ import annotations

import traceback
from typing import Callable, List, Optional

import httpx

from app.log import logger

from .enricher import AIEnricher, EnrichError
from .models import AssetRef, ClipNotesOptions, ContentItem, NoteRecord, NoteStatus
from .parsers import build_generic_parser, build_parsers, select_parser
from .parsers.base import DEFAULT_USER_AGENT, ParseError
from .renderer import render_markdown
from .storage import AssetStore, NoteStorage
from .urlutils import asset_key_url, content_hash, resolve_proxy

# 单条笔记最多落盘的图片数量
MAX_ASSETS = 20

# 通知类型
NOTIFY_READY = "ready"
NOTIFY_FAILED = "failed"


class NoteWorker:
    """笔记后台处理服务。"""

    def __init__(
        self,
        storage: NoteStorage,
        options: ClipNotesOptions,
        asset_store: AssetStore,
        notifier: Optional[Callable[[NoteRecord, str], None]] = None,
    ):
        """初始化后台处理服务。

        :param storage: 笔记存储
        :param options: 插件运行参数
        :param asset_store: 资源文件存储
        :param notifier: 结果通知回调
        """
        self.storage = storage
        self.options = options
        self.asset_store = asset_store
        self.notifier = notifier
        self.parsers = build_parsers(options)
        self.generic_parser = build_generic_parser(options)
        self.enricher = AIEnricher(options)

    async def run_once(self, limit: int = 3) -> int:
        """处理一批待办笔记。

        :param limit: 单次处理的最大数量
        :return: 实际处理的笔记数量
        """
        processed = 0
        for record in self.storage.active_records(limit):
            await self.process(record)
            processed += 1
        return processed

    async def process(self, record: NoteRecord) -> None:
        """完成单条笔记的完整处理流程。

        :param record: 笔记记录
        """
        record.status = NoteStatus.PARSING.value
        record.error = ""
        self.storage.save(record)

        item = await self._parse(record)
        if item is None:
            return
        record.item = item.to_dict()
        record.status = NoteStatus.AI_PENDING.value
        self.storage.save(record)

        if self.options.download_images:
            record.assets = await self._download_assets(record)
            self.storage.save(record)

        ai_result = await self._enrich(record)
        if ai_result is None:
            return
        record.ai = ai_result
        record.markdown = render_markdown(record, self.options)
        record.status = NoteStatus.READY.value
        record.attempts = 0
        record.error = ""
        self.storage.save(record)
        self._notify(record, NOTIFY_READY)

    async def _parse(self, record: NoteRecord) -> Optional[ContentItem]:
        """抓取并解析内容，失败时记录失败状态。

        :param record: 笔记记录
        :return: 标准化内容，失败时返回 None
        """
        parser = select_parser(record.source_url, self.parsers) or self.generic_parser
        try:
            return await parser.parse(record.source_url)
        except ParseError as err:
            self._fail(record, str(err))
            return None
        except Exception as err:  # 兜底，避免任何解析异常影响插件运行
            logger.error(f"ClipNotes 解析 {record.source_url} 出现未预期异常：{traceback.format_exc()}")
            self._fail(record, f"解析异常：{err}")
            return None

    async def _enrich(self, record: NoteRecord) -> Optional[dict]:
        """执行 AI 整理，失败时按重试上限降级。

        :param record: 笔记记录
        :return: 结构化整理结果，未完成时返回 None
        """
        try:
            return await self.enricher.enrich(ContentItem.from_dict(record.item))
        except EnrichError as err:
            record.attempts += 1
            record.error = f"AI 整理失败：{err.message}"
            if record.attempts < self.options.max_attempts:
                record.status = NoteStatus.AI_PENDING.value
                self.storage.save(record)
                return None
            record.ai = {}
            record.markdown = render_markdown(record, self.options)
            record.status = NoteStatus.READY.value
            record.error = f"{record.error}（已达重试上限，跳过 AI 整理）"
            self.storage.save(record)
            self._notify(record, NOTIFY_READY)
            return None

    async def _download_assets(self, record: NoteRecord) -> List[dict]:
        """下载正文图片并落盘。

        :param record: 笔记记录
        :return: 资源引用列表
        """
        item = record.item or {}
        images = [str(url) for url in (item.get("images") or []) if str(url).strip()]
        assets: List[dict] = []
        for url in images[:MAX_ASSETS]:
            asset_hash = content_hash(asset_key_url(url))
            existing = self.asset_store.path_of(asset_hash)
            if existing and existing.exists():
                assets.append(
                    AssetRef(
                        hash=asset_hash,
                        source_url=url,
                        filename=existing.name,
                        downloaded=True,
                        size=existing.stat().st_size,
                    ).to_dict()
                )
                continue
            try:
                data, content_type = await self._fetch_asset(url, record.source_url)
            except Exception as err:
                assets.append(
                    AssetRef(
                        hash=asset_hash,
                        source_url=url,
                        filename="",
                        error=str(err),
                    ).to_dict()
                )
                continue
            path = self.asset_store.write(asset_hash, data, content_type, url)
            assets.append(
                AssetRef(
                    hash=asset_hash,
                    source_url=url,
                    filename=path.name,
                    downloaded=True,
                    size=len(data),
                    content_type=content_type,
                ).to_dict()
            )
        return assets

    async def _fetch_asset(self, url: str, referer: str) -> tuple:
        """下载单个资源文件。

        :param url: 资源地址
        :param referer: 防盗链来源地址
        :return: (内容字节, 响应类型)
        """
        headers = {
            "User-Agent": self.options.user_agent or DEFAULT_USER_AGENT,
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
            "Referer": referer or "",
        }
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=self.options.request_timeout,
            proxy=resolve_proxy(),
        ) as client:
            response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.content, response.headers.get("content-type", "")

    def _fail(self, record: NoteRecord, message: str) -> None:
        """标记笔记处理失败。

        :param record: 笔记记录
        :param message: 失败原因
        """
        record.status = NoteStatus.FAILED.value
        record.error = message
        self.storage.save(record)
        self._notify(record, NOTIFY_FAILED)

    def _notify(self, record: NoteRecord, kind: str) -> None:
        """触发结果通知。

        :param record: 笔记记录
        :param kind: 通知类型
        """
        if record.notified:
            return
        record.notified = True
        self.storage.save(record)
        if self.notifier:
            try:
                self.notifier(record, kind)
            except Exception:
                pass
