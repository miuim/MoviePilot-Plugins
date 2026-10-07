"""ClipNotes 插件主体。

把微信、小红书、抖音等平台的收藏链接抓取为结构化 Markdown，
复用 MoviePilot 现有 LLM 能力完成整理，并提供 REST API 供后续 Obsidian 插件同步。
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from fastapi.responses import FileResponse

from app.core.event import Event, eventmanager
from app.log import logger
from app.plugins import _PluginBase
from app.schemas.types import ChainEventType, EventType, MessageChannel

from .models import (
    AckRequest,
    ClipNotesOptions,
    DEFAULT_CATEGORIES,
    DEFAULT_WECHAT_MENU_CATEGORY,
    NoteRecord,
    NoteStatus,
    PLATFORM_LABELS,
    SubmitRequest,
    now_text,
)
from .renderer import build_filename
from .storage import AssetStore, NoteStorage, markdown_hash
from .urlutils import (
    detect_platform,
    extract_first_url,
    is_short_link,
    is_valid_http_url,
    normalize_url,
    resolve_short_url,
    url_hash,
)
from .worker import NOTIFY_FAILED, NOTIFY_READY, NoteWorker

# 命令行子指令（统一使用英文，避免各渠道命令名兼容问题）
CMD_LIST = ("list",)
CMD_RETRY = ("retry",)
CMD_HELP = ("help", "?", "？")

# 仅发送命令时的链接输入窗口（秒）
INPUT_SESSION_TIMEOUT = 20

# 窗口结束后延迟清理会话的宽限秒数
INPUT_SESSION_GRACE = 1

# 输入会话清理服务的执行间隔（秒）
INPUT_CLEANUP_INTERVAL = 5

# 列表命令展示的最大条数
LIST_LIMIT = 10

# 同步协议版本，客户端应按不透明标识处理
SYNC_PROTOCOL_VERSION = "clipnotes-sync/1"

# 只接受 ASCII 机器人命令名的渠道（Telegram 的 BotCommand 规范）
ASCII_COMMAND_ORIGINS = {"telegram"}

# 机器人命令名规范：a-z0-9_，长度 1~32
BOT_COMMAND_PATTERN = re.compile(r"^[a-z0-9_]{1,32}$")

# 企业微信自定义菜单限制：一级菜单最多 3 个，每个一级菜单最多 5 个子项
WECHAT_MENU_MAX_CATEGORIES = 3
WECHAT_MENU_MAX_ITEMS = 5


class ClipNotes(_PluginBase):
    """收藏链接整理插件。"""

    # 插件名称
    plugin_name = "ClipNotes"
    # 插件描述
    plugin_desc = "收录微信、小红书、抖音链接，抓取正文并经 AI 整理为结构化 Markdown，提供同步 API。"
    # 插件图标
    plugin_icon = "clipnotes.png"
    # 插件版本
    plugin_version = "1.1.0"
    # 插件标签
    plugin_label = "知识管理"
    # 插件作者
    plugin_author = "local"
    # 作者主页
    author_url = ""
    # 插件配置项ID前缀
    plugin_config_prefix = "clipnotes_"
    # 加载顺序
    plugin_order = 200
    # 可使用的用户级别
    auth_level = 1

    # 运行状态
    _enabled: bool = False
    _notify: bool = True
    _notify_ready: bool = True
    _config: Dict[str, Any] = {}
    _running: bool = False
    # 已开启的链接输入窗口：key -> (截止时间, 用户, 渠道, 来源)
    _pending_inputs: Dict[str, Tuple[float, Any, Any, Any]] = {}

    def init_plugin(self, config: dict = None) -> None:
        """根据插件配置初始化运行状态。

        :param config: 插件配置
        """
        self._config = dict(config or {})
        self._enabled = bool(self._config.get("enabled"))
        self._notify = bool(self._config.get("notify", True))
        self._notify_ready = bool(self._config.get("notify_ready", True))
        self._running = False
        self._pending_inputs = {}
        if self._enabled:
            logger.info("ClipNotes 插件已启用")

    def get_state(self) -> bool:
        """获取插件启用状态。

        :return: 是否启用
        """
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """返回插件远程命令列表。

        ``/note`` 与 ``/笔记`` 完全等效，前者便于在只支持拉丁字符命令名的渠道使用。

        :return: 命令定义列表
        """
        return [
            {
                "cmd": "/笔记",
                "event": EventType.PluginAction,
                "desc": "收录链接到 ClipNotes",
                "category": "知识管理",
                "data": {"action": "clipnotes_submit"},
            },
            {
                "cmd": "/note",
                "event": EventType.PluginAction,
                "desc": "收录链接到 ClipNotes",
                "category": "知识管理",
                "data": {"action": "clipnotes_submit"},
            },
        ]

    def get_api(self) -> List[Dict[str, Any]]:
        """返回插件 API 列表。

        :return: API 定义列表
        """
        return [
            {
                "path": "/notes",
                "endpoint": self.api_list_notes,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "查询笔记列表",
                "description": "传入 since 游标（或 limit）走增量同步模式，按 (updated_at, content_id) 升序返回并给出 next_cursor；否则按 page/count 偏移浏览。",
            },
            {
                "path": "/notes",
                "endpoint": self.api_submit_note,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "提交链接",
                "description": "外部程序直接提交待收录链接，返回笔记标识与去重结果。",
            },
            {
                "path": "/notes/{note_id}",
                "endpoint": self.api_get_note,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "获取笔记详情",
                "description": "返回单条笔记的 Markdown、元数据、revision/content_hash 与图片资源列表。",
            },
            {
                "path": "/notes/{note_id}/ack",
                "endpoint": self.api_ack_note,
                "methods": ["POST"],
                "auth": "apikey",
                "summary": "同步确认",
                "description": "同步端写入本地后回写状态，可携带 content_hash 校验版本，旧版本会返回 stale。",
            },
            {
                "path": "/assets/{asset_hash}",
                "endpoint": self.api_get_asset,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "获取图片资源",
                "description": "按资源哈希下载已落盘的图片文件。",
            },
            {
                "path": "/stats",
                "endpoint": self.api_stats,
                "methods": ["GET"],
                "auth": "apikey",
                "summary": "统计信息",
                "description": "返回各状态笔记数量与运行参数概况。",
            },
        ]

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """返回插件配置表单与默认配置。

        :return: (表单配置, 默认配置)
        """
        form = [
            {
                "component": "VForm",
                "content": [
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enabled", "label": "启用插件"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify", "label": "命令执行回执"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "notify_ready", "label": "整理完成通知"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VAlert",
                        "props": {
                            "type": "info",
                            "variant": "tonal",
                            "density": "comfortable",
                            "class": "mb-3",
                            "text": "收录入口：发送 /笔记 <链接> 或 /note <链接>（也可只发 /note，再在 20 秒内发送链接）；查看记录：/note list；重新处理：/note retry <id>。",
                        },
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enable_wechat", "label": "启用微信解析"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enable_xiaohongshu", "label": "启用小红书解析"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "enable_douyin", "label": "启用抖音解析"},
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VSwitch",
                                        "props": {"model": "download_images", "label": "下载正文图片"},
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "default_status",
                                            "label": "Markdown 初始状态",
                                            "placeholder": "待读",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "worker_interval",
                                            "label": "后台处理间隔（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "batch_size",
                                            "label": "单次处理条数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "max_attempts",
                                            "label": "AI 整理最大重试次数",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 4},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "request_timeout",
                                            "label": "抓取超时（秒）",
                                            "type": "number",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "categories",
                                            "label": "允许的顶层分类（一行一个）",
                                            "rows": 6,
                                            "placeholder": "\n".join(DEFAULT_CATEGORIES),
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "ai_max_chars",
                                            "label": "AI 输入正文上限（字符）",
                                            "type": "number",
                                        },
                                    },
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "llm_provider",
                                            "label": "LLM 提供商（留空用系统配置）",
                                        },
                                    },
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "llm_model",
                                            "label": "LLM 模型（留空用系统配置）",
                                        },
                                    },
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "llm_temperature",
                                            "label": "LLM 温度",
                                            "type": "number",
                                        },
                                    },
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextarea",
                                        "props": {
                                            "model": "xiaohongshu_cookie",
                                            "label": "小红书 Cookie（可选）",
                                            "rows": 3,
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                    {
                        "component": "VRow",
                        "content": [
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "third_party_api",
                                            "label": "第三方解析服务（可选，支持 {url} 占位）",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "wechat_menu_category",
                                            "label": "微信菜单归属分类（留空则独立占一级菜单）",
                                            "placeholder": "订阅",
                                        },
                                    }
                                ],
                            },
                            {
                                "component": "VCol",
                                "props": {"cols": 12, "md": 6},
                                "content": [
                                    {
                                        "component": "VTextField",
                                        "props": {
                                            "model": "user_agent",
                                            "label": "抓取 User-Agent（留空用内置浏览器 UA）",
                                        },
                                    }
                                ],
                            },
                        ],
                    },
                ],
            }
        ]
        return form, self._default_config()

    def get_page(self) -> Optional[List[dict]]:
        """返回插件详情页面。

        :return: 页面配置，未启用时返回 None
        """
        if not self._enabled:
            return None
        storage = self._storage()
        stats = storage.stats()
        summary = (
            f"共 {stats.get('total', 0)} 条笔记："
            f"待同步 {stats.get('by_status', {}).get(NoteStatus.READY.value, 0)}，"
            f"处理中 {stats.get('by_status', {}).get(NoteStatus.RECEIVED.value, 0) + stats.get('by_status', {}).get(NoteStatus.PARSING.value, 0) + stats.get('by_status', {}).get(NoteStatus.AI_PENDING.value, 0)}，"
            f"已同步 {stats.get('by_status', {}).get(NoteStatus.SYNCED.value, 0)}，"
            f"失败 {stats.get('by_status', {}).get(NoteStatus.FAILED.value, 0)}"
        )
        items, _ = storage.list_notes(count=LIST_LIMIT)
        list_items = []
        for item in items:
            status_label = NoteStatus(str(item.get("status"))).label if item.get("status") in {
                status.value for status in NoteStatus
            } else str(item.get("status"))
            platform_label = PLATFORM_LABELS.get(str(item.get("platform")), str(item.get("platform")))
            list_items.append(
                {
                    "component": "VListItem",
                    "props": {
                        "title": str(item.get("title") or item.get("source_url") or ""),
                        "subtitle": f"[{status_label}] {platform_label} · {item.get('created_at', '')} · id={item.get('content_id', '')}",
                    },
                }
            )
        return [
            {
                "component": "VCard",
                "props": {"variant": "tonal", "class": "mb-3"},
                "content": [
                    {
                        "component": "VCardText",
                        "props": {"text": summary},
                    }
                ],
            },
            {
                "component": "VList",
                "props": {"density": "compact"},
                "content": list_items or [
                    {
                        "component": "VListItem",
                        "props": {"title": "暂无笔记记录", "subtitle": "发送 /笔记 <链接> 开始收录"},
                    }
                ],
            },
        ]

    def get_service(self) -> List[Dict[str, Any]]:
        """注册插件后台服务。

        :return: 服务定义列表
        """
        if not self._enabled:
            return []
        options = self._options()
        return [
            {
                "id": "ClipNotesWorker",
                "name": "ClipNotes 后台处理",
                "trigger": "interval",
                "func": self.worker_job,
                "kwargs": {"seconds": options.worker_interval},
            },
            {
                "id": "ClipNotesInputCleanup",
                "name": "ClipNotes 输入窗口清理",
                "trigger": "interval",
                "func": self.input_cleanup_job,
                "kwargs": {"seconds": INPUT_CLEANUP_INTERVAL},
            },
        ]

    def stop_service(self) -> None:
        """停止插件后台服务并释放资源。"""
        self._running = False
        logger.info("ClipNotes 插件服务已停止")

    @eventmanager.register(EventType.PluginAction)
    def on_plugin_action(self, event: Event) -> None:
        """处理插件命令事件。

        :param event: 事件对象
        """
        if not self._enabled:
            return
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("action") != "clipnotes_submit":
            return
        arg_str = str(event_data.get("arg_str") or "").strip()
        channel = event_data.get("channel")
        source = event_data.get("source")
        user = event_data.get("user")
        logger.info(f"ClipNotes 收到命令：{arg_str or '(无参数)'}")
        try:
            response = self._handle_command(arg_str, channel=channel, source=source, user=user)
        except Exception as err:
            logger.error(f"ClipNotes 命令处理失败：{err}")
            response = f"处理失败：{err}"
        if response and self._notify:
            self._reply(channel=channel, source=source, user=user, text=response)

    @eventmanager.register(ChainEventType.CommandRegister)
    def arrange_channel_commands(self, event: Event) -> None:
        """按渠道整理待注册的命令菜单。

        - Telegram：剔除不符合 BotCommand 规范（``a-z0-9_``）的命令名，
          否则整批 ``setMyCommands`` 会返回 400，导致该渠道菜单全部注册失败；
        - 企业微信：按菜单数量限制重排分类，保证本插件命令能出现在菜单里；
        - 命令链：声明自身为拦截源，使本插件重载时强制重新向各渠道注册菜单。

        :param event: 命令注册链式事件
        """
        if not self._enabled:
            return
        event_data = getattr(event, "event_data", None)
        if not event_data:
            return
        origin = str(getattr(event_data, "origin", "") or "").lower()
        if origin in ASCII_COMMAND_ORIGINS:
            self._filter_ascii_commands(event_data)
        elif origin == "wechat":
            self._arrange_wechat_menu(event_data)
        elif origin == "commandchain":
            # 本插件会调整各渠道的命令集，声明拦截源以便插件重载时强制重新注册
            event_data.source = self.__class__.__name__

    @staticmethod
    def _filter_ascii_commands(event_data: Any) -> None:
        """剔除不符合 ASCII 命令名规范的命令。

        :param event_data: 命令注册事件数据
        """
        commands = getattr(event_data, "commands", None)
        if not isinstance(commands, dict) or not commands:
            return
        removed = [
            name
            for name in list(commands)
            if not BOT_COMMAND_PATTERN.match(str(name).lstrip("/").lower())
        ]
        if not removed:
            return
        for name in removed:
            commands.pop(name, None)
        logger.info(f"ClipNotes：已为 Telegram 剔除不符合命令名规范的命令 {removed}")

    def _arrange_wechat_menu(self, event_data: Any) -> None:
        """按企业微信菜单限制重排命令分类。

        企业微信自定义菜单最多 3 个一级菜单、每个一级菜单最多 5 个子项，MoviePilot
        只按分类出现顺序取前 3 个，命令所属分类靠后时会整类消失。这里会：

        1. 把本插件命令的菜单名改为插件名，短且明确；
        2. 若配置了归属分类，则把本插件命令并入该分类（默认「订阅」），
           不再单独占用一级菜单位置；
        3. 同分类内按菜单名去重，超出数量限制的命令记录日志后丢弃。

        :param event_data: 命令注册事件数据
        """
        commands = getattr(event_data, "commands", None)
        if not isinstance(commands, dict) or not commands:
            return
        own = self._own_categories()
        target = self._options().wechat_menu_category
        for name, meta in commands.items():
            if not isinstance(meta, dict):
                continue
            if str(meta.get("category") or "").strip() in own:
                meta["description"] = self.plugin_name
                if target:
                    # 并入现有的某个一级菜单，避免挤掉其它分类
                    meta["category"] = target

        grouped: Dict[str, Dict[str, Any]] = {}
        for name, meta in commands.items():
            category = str((meta or {}).get("category") or "").strip()
            if category:
                grouped.setdefault(category, {})[name] = meta
        if not grouped:
            return
        priority = own if not target else []
        ordered = [category for category in priority if category in grouped]
        ordered += [category for category in grouped if category not in ordered]
        keep = ordered[:WECHAT_MENU_MAX_CATEGORIES]
        overflow = ordered[WECHAT_MENU_MAX_CATEGORIES:]

        rearranged: Dict[str, Any] = {}
        dropped: List[str] = []
        for category in keep:
            seen_desc = set()
            count = 0
            for name, meta in grouped[category].items():
                description = str((meta or {}).get("description") or "")
                if description in seen_desc or count >= WECHAT_MENU_MAX_ITEMS:
                    dropped.append(name)
                    continue
                seen_desc.add(description)
                rearranged[name] = meta
                count += 1
        for category in overflow:
            dropped.extend(grouped[category].keys())

        commands.clear()
        commands.update(rearranged)
        logger.info(
            f"ClipNotes：微信菜单一级菜单顺序为 {keep}，"
            f"本插件命令归入 {target or '独立分类'}，"
            f"未进入菜单的命令 {dropped}（仍可手动输入）"
        )

    def _own_categories(self) -> List[str]:
        """返回本插件命令使用的分类名称。

        :return: 分类名称列表
        """
        categories: List[str] = []
        for command in self.get_command() or []:
            category = str(command.get("category") or "").strip()
            if category and category not in categories:
                categories.append(category)
        return categories

    async def worker_job(self) -> None:
        """后台定时处理待办笔记。"""
        if not self._enabled or self._running:
            return
        self._running = True
        try:
            options = self._options()
            worker = self._build_worker(options)
            processed = await worker.run_once(options.batch_size)
            if processed:
                logger.info(f"ClipNotes 本轮处理 {processed} 条笔记")
        except Exception as err:
            logger.error(f"ClipNotes 后台处理异常：{err}")
        finally:
            self._running = False

    def _handle_command(self, arg_str: str, channel: Any, source: Any, user: Any) -> str:
        """解析并执行命令内容。

        :param arg_str: 命令参数
        :param channel: 消息渠道
        :param source: 消息来源
        :param user: 用户标识
        :return: 回执文本
        """
        if not arg_str or arg_str in CMD_HELP:
            if self._start_input_session(channel=channel, source=source, user=user):
                return (
                    f"{self._help_text()}\n\n"
                    f"也可以在 {INPUT_SESSION_TIMEOUT} 秒内直接发送链接，我会自动收录。"
                )
            return self._help_text()
        parts = arg_str.split(maxsplit=1)
        head = parts[0].strip().lower()
        rest = parts[1].strip() if len(parts) > 1 else ""
        if head in CMD_LIST:
            return self._render_list()
        if head in CMD_RETRY:
            return self._retry_note(rest)
        url = extract_first_url(arg_str)
        if not url:
            return "未识别到有效链接。\n" + self._help_text()
        return self.submit_url(url, channel=channel, source=source, user=user)

    def _start_input_session(self, channel: Any, source: Any, user: Any) -> bool:
        """为用户开启一次链接输入会话。

        依托 MoviePilot 的插件输入会话机制：会话在消息链路中先于全局智能体被消费，
        因此窗口期内直接发送的裸链接会进入本插件，而不是被智能体接管。

        :param channel: 消息渠道
        :param source: 消息来源
        :param user: 用户标识
        :return: 是否成功开启会话
        """
        if not user or channel is None:
            # 无渠道来源（如接口或智能体触发）时没有可回填的窗口，直接跳过
            return False
        try:
            from app.helper.interaction import plugin_input_interaction_manager
        except Exception as err:
            logger.error(f"ClipNotes 无法加载插件输入会话模块：{err}")
            return False
        try:
            plugin_input_interaction_manager.create_or_replace(
                user_id=user,
                plugin_id=self.__class__.__name__,
                channel=channel,
                source=source,
                username=None,
                timeout_seconds=INPUT_SESSION_TIMEOUT,
                payload={"action": "clipnotes_submit"},
            )
        except Exception as err:
            logger.error(f"ClipNotes 创建链接输入会话失败：{err}")
            return False
        self._pending_inputs[self._input_key(user, channel, source)] = (
            time.time() + INPUT_SESSION_TIMEOUT,
            user,
            channel,
            source,
        )
        return True

    @staticmethod
    def _input_key(user: Any, channel: Any, source: Any) -> str:
        """生成输入窗口的跟踪键。

        :param user: 用户标识
        :param channel: 消息渠道
        :param source: 消息来源
        :return: 跟踪键
        """
        return f"{user}|{getattr(channel, 'value', channel)}|{source}"

    def _forget_input_session(self, user: Any, channel: Any, source: Any) -> None:
        """清除指定用户在本渠道的输入会话与超时残留。

        :param user: 用户标识
        :param channel: 消息渠道
        :param source: 消息来源
        """
        if not user:
            return
        try:
            from app.helper.interaction import plugin_input_interaction_manager
        except Exception:
            return
        try:
            plugin_input_interaction_manager.pop_by_user(user, channel, source, None)
        except Exception as err:
            logger.error(f"ClipNotes 清理输入会话失败：{err}")

    def input_cleanup_job(self) -> None:
        """清理已结束的链接输入窗口。

        MoviePilot 在插件输入会话超时后会额外推送一条「插件输入已超时」提示，
        插件侧无法拦截这条消息。因此在窗口结束后主动清理会话，使后续命令
        不会再收到这条多余提示。
        """
        if not self._pending_inputs:
            return
        now = time.time()
        for key, entry in list(self._pending_inputs.items()):
            deadline, user, channel, source = entry
            if now < deadline + INPUT_SESSION_GRACE:
                continue
            self._pending_inputs.pop(key, None)
            self._forget_input_session(user, channel, source)

    @eventmanager.register(EventType.MessageAction)
    def on_plugin_input(self, event: Event) -> None:
        """处理链接输入会话中收到的内容。

        :param event: 消息交互事件
        """
        if not self._enabled:
            return
        event_data = getattr(event, "event_data", None) or {}
        if event_data.get("plugin_id") != self.__class__.__name__:
            return
        marker = str(event_data.get("text") or "")
        channel = event_data.get("channel")
        source = event_data.get("source")
        user = event_data.get("userid")
        if marker.startswith("plugin_input_expired"):
            # 会话已超时，顺手清掉超时残留，避免下次命令再收到超时提示
            self._pending_inputs.pop(self._input_key(user, channel, source), None)
            self._forget_input_session(user, channel, source)
            logger.info("ClipNotes 链接输入会话已超时，等待下次触发")
            return
        if marker.startswith("plugin_input_cancel"):
            self._pending_inputs.pop(self._input_key(user, channel, source), None)
            logger.info("ClipNotes 链接输入会话已取消")
            return
        if not marker.startswith("plugin_input|"):
            return
        input_text = str(event_data.get("input_text") or "").strip()
        if not input_text:
            return
        self._pending_inputs.pop(self._input_key(user, channel, source), None)
        if input_text.startswith("/"):
            # 窗口内的命令应优先按命令执行，重新派发命令事件，
            # 否则会被当成链接内容处理
            self.eventmanager.send_event(
                EventType.CommandExcute,
                {"cmd": input_text, "user": user, "channel": channel, "source": source},
            )
            return
        if not extract_first_url(input_text):
            head = input_text.split(maxsplit=1)[0].lower()
            if head not in CMD_LIST + CMD_RETRY + CMD_HELP:
                # 没识别到链接，重新给一次输入机会
                self._start_input_session(channel=channel, source=source, user=user)
        response = self._handle_command(input_text, channel=channel, source=source, user=user)
        if response and self._notify:
            self._reply(channel=channel, source=source, user=user, text=response)

    def submit_url(self, url: str, channel: Any = None, source: Any = None, user: Any = None) -> str:
        """提交链接并按需创建笔记任务。

        :param url: 目标链接
        :param channel: 消息渠道
        :param source: 消息来源
        :param user: 用户标识
        :return: 回执文本
        """
        if not is_valid_http_url(url):
            return f"链接无效：{url}"
        # 短链先解析真实地址，保证平台识别与去重正确
        target = resolve_short_url(url) if is_short_link(url) else url
        result = self._register_note(target=target, channel=channel, source=source, user=user)
        if result.get("invalid"):
            return f"链接无效：{url}"
        if result.get("exists"):
            if result.get("requeued"):
                return f"该链接此前处理失败，已重新排队。\nid：{result['content_id']}"
            return (
                f"该链接已收录，无需重复提交。\n"
                f"状态：{result.get('status_label')}\n"
                f"标题：{result.get('title')}\n"
                f"id：{result['content_id']}"
            )
        return (
            f"已收录，稍后自动抓取并整理。\n"
            f"平台：{result.get('platform_label')}\n"
            f"id：{result['content_id']}"
        )

    def _register_note(
        self,
        target: str,
        channel: Any = None,
        source: Any = None,
        user: Any = None,
    ) -> Dict[str, Any]:
        """规范化链接并创建或复用笔记记录。

        :param target: 已解析的真实链接
        :param channel: 消息渠道
        :param source: 消息来源
        :param user: 用户标识
        :return: 处理结果
        """
        normalized = normalize_url(target)
        if not is_valid_http_url(normalized):
            return {"invalid": True}
        content_id = url_hash(normalized)
        storage = self._storage()
        existing = storage.get(content_id)
        if existing:
            if existing.status == NoteStatus.FAILED.value:
                self._requeue(existing)
                return {"exists": True, "requeued": True, "content_id": content_id}
            return {
                "exists": True,
                "requeued": False,
                "content_id": content_id,
                "status": existing.status,
                "status_label": existing.status_label,
                "title": existing.title,
            }
        record = NoteRecord.new_from_url(
            content_id=content_id,
            source_url=normalized,
            platform=detect_platform(normalized),
        )
        record.submit_user = str(user or "")
        record.submit_channel = str(getattr(channel, "value", channel) or "")
        record.submit_source = str(source or "")
        storage.save(record)
        return {
            "exists": False,
            "content_id": content_id,
            "platform": record.platform,
            "platform_label": record.platform_label,
            "status": record.status,
        }

    def _requeue(self, record: NoteRecord) -> None:
        """把笔记重新放回待处理队列。

        :param record: 笔记记录
        """
        record.status = NoteStatus.RECEIVED.value
        record.attempts = 0
        record.error = ""
        record.notified = False
        self._storage().save(record)

    def _retry_note(self, note_id: str) -> str:
        """重新处理指定的笔记。

        :param note_id: 笔记标识，支持前缀匹配
        :return: 回执文本
        """
        note_id = (note_id or "").strip()
        if not note_id:
            return "请提供要重试的 id，例如：/笔记 重试 1a2b3c4d"
        storage = self._storage()
        index = storage.load_index()
        matches = [key for key in index if key == note_id or key.startswith(note_id)]
        if not matches:
            return f"未找到 id 为 {note_id} 的记录。"
        if len(matches) > 1:
            return f"匹配到多条记录，请提供更完整的 id：{', '.join(matches[:5])}"
        record = storage.get(matches[0])
        if not record:
            return f"记录 {matches[0]} 数据缺失，无法重试。"
        self._requeue(record)
        return f"已重新排队处理。\n标题：{record.title}\nid：{record.content_id}"

    def _render_list(self) -> str:
        """渲染最近笔记列表。

        :return: 列表文本
        """
        storage = self._storage()
        items, total = storage.list_notes(count=LIST_LIMIT)
        if not items:
            return "暂无笔记记录。发送 /笔记 <链接> 开始收录。"
        lines = [f"最近 {len(items)} / 共 {total} 条记录："]
        for item in items:
            status = str(item.get("status") or "")
            status_label = NoteStatus(status).label if status in {value.value for value in NoteStatus} else status
            platform_label = PLATFORM_LABELS.get(str(item.get("platform")), str(item.get("platform")))
            lines.append(
                f"- [{status_label}] {item.get('title') or item.get('source_url')} "
                f"（{platform_label} · {item.get('created_at', '')} · id={item.get('content_id', '')}）"
            )
        return "\n".join(lines)

    def _help_text(self) -> str:
        """返回命令帮助文本。

        :return: 帮助文本
        """
        return (
            "ClipNotes 使用说明（/note 与 /笔记 等效）：\n"
            "/笔记 <链接>：收录链接并自动抓取整理\n"
            "/笔记 list：查看最近记录\n"
            "/笔记 retry <id>：重新处理指定记录\n"
            f"只发送 /笔记 不跟参数时，可在 {INPUT_SESSION_TIMEOUT} 秒内直接发送链接。"
        )

    def _reply(self, channel: Any, source: Any, user: Any, text: str, title: str = "ClipNotes") -> None:
        """向用户发送回执。

        :param channel: 消息渠道
        :param source: 消息来源
        :param user: 用户标识
        :param text: 回执内容
        :param title: 回执标题
        """
        if channel is None and not user:
            # 无渠道也无用户（如接口或智能体触发）时不广播，避免打扰所有通知渠道
            logger.info(f"ClipNotes 命令无回执对象，已跳过通知：{text.splitlines()[0] if text else ''}")
            return
        try:
            self.post_message(
                channel=channel,
                title=title,
                text=text,
                userid=str(user) if user else None,
                source=source,
            )
        except Exception as err:
            logger.error(f"ClipNotes 发送回执失败：{err}")

    def _notify_result(self, record: NoteRecord, kind: str) -> None:
        """整理完成或失败后的通知回调。

        :param record: 笔记记录
        :param kind: 结果类型
        """
        if not self._notify_ready or not record.submit_user:
            return
        channel = self._channel_of(record.submit_channel)
        if record.submit_channel and channel is None:
            # 渠道无法识别时不要退化成全渠道广播
            logger.warning(f"ClipNotes 无法识别通知渠道，跳过通知：{record.submit_channel}")
            return
        if kind == NOTIFY_READY:
            text = (
                f"标题：{record.title}\n"
                f"分类：{(record.ai or {}).get('category') or 'Inbox'}\n"
                f"平台：{record.platform_label}\n"
                f"id：{record.content_id}"
            )
            title = "ClipNotes 整理完成"
        else:
            text = f"链接：{record.source_url}\n原因：{record.error or '解析失败'}"
            title = "ClipNotes 处理失败"
        self.post_message(
            channel=channel,
            title=title,
            text=text,
            userid=record.submit_user,
            source=record.submit_source,
        )

    @staticmethod
    def _channel_of(value: str) -> Optional[MessageChannel]:
        """把渠道文本转换为消息渠道枚举。

        :param value: 渠道文本
        :return: 消息渠道，无法识别时返回 None
        """
        if not value:
            return None
        try:
            return MessageChannel(value)
        except ValueError:
            return None

    def _storage(self) -> NoteStorage:
        """构建笔记存储实例。

        :return: 笔记存储
        """
        return NoteStorage(self)

    def _asset_store(self) -> AssetStore:
        """构建资源存储实例。

        :return: 资源存储
        """
        return AssetStore(self.get_data_path() / "assets")

    def _options(self) -> ClipNotesOptions:
        """根据当前配置构建运行参数。

        :return: 运行参数
        """
        return ClipNotesOptions.from_config(self._config)

    def _build_worker(self, options: Optional[ClipNotesOptions] = None) -> NoteWorker:
        """构建后台处理服务。

        :param options: 运行参数
        :return: 后台处理服务
        """
        return NoteWorker(
            storage=self._storage(),
            options=options or self._options(),
            asset_store=self._asset_store(),
            notifier=self._notify_result,
        )

    @staticmethod
    def _default_config() -> Dict[str, Any]:
        """返回插件默认配置。

        :return: 默认配置字典
        """
        return {
            "enabled": False,
            "notify": True,
            "notify_ready": True,
            "enable_wechat": True,
            "enable_xiaohongshu": True,
            "enable_douyin": True,
            "download_images": True,
            "default_status": "待读",
            "worker_interval": 30,
            "batch_size": 3,
            "max_attempts": 3,
            "request_timeout": 20,
            "ai_max_chars": 4000,
            "llm_provider": "",
            "llm_model": "",
            "llm_temperature": 0.2,
            "xiaohongshu_cookie": "",
            "third_party_api": "",
            "user_agent": "",
            "wechat_menu_category": DEFAULT_WECHAT_MENU_CATEGORY,
            "categories": "\n".join(DEFAULT_CATEGORIES),
        }

    def _api_prefix(self) -> str:
        """返回当前插件 API 的访问前缀。

        :return: API 前缀
        """
        return f"/api/v1/plugin/{self.__class__.__name__}"

    async def api_list_notes(
        self,
        status: Optional[str] = None,
        platform: Optional[str] = None,
        since: Optional[str] = None,
        limit: Optional[int] = None,
        page: Optional[int] = None,
        count: Optional[int] = None,
        include_deleted: bool = False,
    ) -> Dict[str, Any]:
        """查询笔记列表。

        传入 ``since``（游标）或 ``limit`` 时进入增量同步模式：按
        ``(updated_at, content_id)`` 升序 keyset 分页，返回 ``next_cursor``；
        否则保持偏移分页的浏览模式。

        :param status: 状态过滤，多个状态用逗号分隔
        :param platform: 平台过滤，多个平台用逗号分隔
        :param since: 上次同步返回的游标，需按不透明字符串原样回传
        :param limit: 本页最大条数，默认 20，最大 100
        :param page: 浏览模式的页码
        :param count: 浏览模式的每页数量
        :param include_deleted: 是否附带游标之后的删除记录
        :return: 列表结果
        """
        storage = self._storage()
        if since is not None or limit is not None:
            page_items, next_cursor, has_more, remaining = storage.list_notes_since(
                status=status,
                platform=platform,
                since=since or "",
                limit=limit or 20,
            )
            result: Dict[str, Any] = {
                "success": True,
                "protocol": SYNC_PROTOCOL_VERSION,
                "mode": "cursor",
                "server_time": now_text(),
                "count": len(page_items),
                "remaining": remaining,
                "has_more": has_more,
                "next_cursor": next_cursor,
                "items": [self._list_item(item) for item in page_items],
            }
            if include_deleted:
                result["deleted"] = storage.tombstones(since or "")
            return result
        items, total = storage.list_notes(
            status=status,
            platform=platform,
            page=page or 1,
            count=count or 20,
        )
        return {
            "success": True,
            "protocol": SYNC_PROTOCOL_VERSION,
            "mode": "offset",
            "server_time": now_text(),
            "page": max(1, int(page or 1)),
            "count": len(items),
            "total": total,
            "has_more": (max(1, int(page or 1)) * max(1, min(100, int(count or 20)))) < total,
            "next_cursor": "",
            "items": [self._list_item(item) for item in items],
            "deleted": [],
        }

    @staticmethod
    def _list_item(summary: Dict[str, Any]) -> Dict[str, Any]:
        """规范化列表项字段，保证同步端拿到稳定结构。

        :param summary: 索引中的摘要信息
        :return: 稳定结构的列表项
        """
        platform = str(summary.get("platform") or "")
        return {
            "content_id": str(summary.get("content_id") or ""),
            "source_url": str(summary.get("source_url") or ""),
            "platform": platform,
            "platform_label": PLATFORM_LABELS.get(platform, platform),
            "status": str(summary.get("status") or ""),
            "title": str(summary.get("title") or ""),
            "category": str(summary.get("category") or ""),
            "tags": list(summary.get("tags") or []),
            "created_at": str(summary.get("created_at") or ""),
            "updated_at": str(summary.get("updated_at") or ""),
            "revision": int(summary.get("revision") or 0),
            "content_hash": str(summary.get("content_hash") or ""),
            "markdown_size": int(summary.get("markdown_size") or 0),
            "assets_count": int(summary.get("assets_count") or 0),
            "error": str(summary.get("error") or ""),
        }

    async def api_submit_note(self, payload: SubmitRequest) -> Dict[str, Any]:
        """提交待收录链接。

        :param payload: 提交请求体
        :return: 处理结果
        """
        url = (payload.url or "").strip()
        if not is_valid_http_url(url):
            raise HTTPException(status_code=400, detail="链接无效")
        # 短链解析会阻塞请求，放到线程池执行
        target = (
            await asyncio.to_thread(resolve_short_url, url) if is_short_link(url) else url
        )
        result = self._register_note(target=target)
        if result.get("invalid"):
            raise HTTPException(status_code=400, detail="链接无效")
        return {"success": True, **result}

    async def api_get_note(self, note_id: str) -> Dict[str, Any]:
        """获取单条笔记详情。

        :param note_id: 笔记标识
        :return: 笔记详情
        """
        record = self._storage().get(note_id)
        if not record:
            raise HTTPException(status_code=404, detail="笔记不存在")
        item = record.item or {}
        ai = record.ai or {}
        assets = []
        for asset in record.assets or []:
            if not isinstance(asset, dict):
                continue
            entry = dict(asset)
            if entry.get("hash"):
                entry["api_path"] = f"{self._api_prefix()}/assets/{entry.get('hash')}"
            assets.append(entry)
        return {
            "success": True,
            "protocol": SYNC_PROTOCOL_VERSION,
            "data": {
                "content_id": record.content_id,
                "source_url": record.source_url,
                "platform": record.platform,
                "platform_label": record.platform_label,
                "status": record.status,
                "status_label": record.status_label,
                "title": record.title,
                "filename": build_filename(record),
                "category": ai.get("category") or "",
                "tags": ai.get("tags") or [],
                "summary": ai.get("summary") or "",
                "language": ai.get("language") or "",
                "markdown": record.markdown,
                "markdown_size": len(record.markdown or ""),
                "content_hash": record.content_hash or markdown_hash(record.markdown),
                "revision": record.revision,
                "meta": {
                    "author": item.get("author") or "",
                    "publish_time": item.get("publish_time") or "",
                    "video": item.get("video") or "",
                    "images": item.get("images") or [],
                    "extra": item.get("metadata") or {},
                },
                "assets": assets,
                "assets_count": len(assets),
                "error": record.error,
                "attempts": record.attempts,
                "created_at": record.created_at,
                "updated_at": record.updated_at,
                "synced_at": record.synced_at,
            },
        }

    async def api_ack_note(self, note_id: str, payload: Optional[AckRequest] = None) -> Dict[str, Any]:
        """回写同步状态。

        :param note_id: 笔记标识
        :param payload: 同步确认请求体
        :return: 处理结果
        """
        storage = self._storage()
        record = storage.get(note_id)
        if not record:
            raise HTTPException(status_code=404, detail="笔记不存在")
        target_status = (payload.status if payload and payload.status else NoteStatus.SYNCED.value)
        if target_status not in {item.value for item in NoteStatus}:
            raise HTTPException(status_code=400, detail="状态非法")
        current_hash = record.content_hash or markdown_hash(record.markdown)
        # 客户端确认的是旧版本时不做标记，让其重新拉取，避免漏更新
        if payload and payload.content_hash and payload.content_hash != current_hash:
            return {
                "success": True,
                "stale": True,
                "content_id": record.content_id,
                "status": record.status,
                "revision": record.revision,
                "content_hash": current_hash,
                "message": "客户端同步的是旧版本，请重新拉取该笔记",
            }
        record.status = target_status
        if target_status == NoteStatus.SYNCED.value:
            record.synced_at = now_text()
        storage.save(record)
        return {
            "success": True,
            "stale": False,
            "content_id": record.content_id,
            "status": record.status,
            "synced_at": record.synced_at,
            "revision": record.revision,
            "content_hash": record.content_hash or current_hash,
        }

    def api_get_asset(self, asset_hash: str) -> FileResponse:
        """下载已落盘的图片资源。

        :param asset_hash: 资源哈希
        :return: 文件响应
        """
        path = self._asset_store().path_of(asset_hash)
        if not path or not path.exists():
            raise HTTPException(status_code=404, detail="资源不存在")
        return FileResponse(path, filename=path.name)

    async def api_stats(self) -> Dict[str, Any]:
        """返回插件运行统计。

        :return: 统计数据
        """
        options = self._options()
        stats = self._storage().stats()
        return {
            "success": True,
            "protocol": SYNC_PROTOCOL_VERSION,
            "server_time": now_text(),
            "enabled": self._enabled,
            "stats": stats,
            "options": {
                "worker_interval": options.worker_interval,
                "batch_size": options.batch_size,
                "max_attempts": options.max_attempts,
                "ai_max_chars": options.ai_max_chars,
                "download_images": options.download_images,
                "categories": options.categories,
                "llm_provider": options.llm_provider,
                "llm_model": options.llm_model,
                "third_party_api_configured": bool(options.third_party_api),
            },
        }
