"""解析器注册与选择。"""
from __future__ import annotations

from typing import List, Optional

from ..models import ClipNotesOptions, Platform
from .base import BaseParser, HtmlConverter, ParseError
from .douyin import DouyinParser
from .generic import GenericParser
from .wechat import WechatParser
from .xiaohongshu import XiaohongshuParser

# 各平台解析开关，可单独关闭后改用兜底解析
PLATFORM_SWITCHES = {
    Platform.WECHAT.value: "enable_wechat",
    Platform.XIAOHONGSHU.value: "enable_xiaohongshu",
    Platform.DOUYIN.value: "enable_douyin",
}


def build_parsers(options: Optional[ClipNotesOptions] = None) -> List[BaseParser]:
    """根据配置构建可用的站点解析器列表。

    :param options: 插件运行参数
    :return: 解析器实例列表（不含兜底解析器）
    """
    options = options or ClipNotesOptions()
    parsers: List[BaseParser] = []
    for parser_class in (WechatParser, XiaohongshuParser, DouyinParser):
        switch = PLATFORM_SWITCHES.get(parser_class.platform)
        if switch and not getattr(options, switch, True):
            continue
        parsers.append(parser_class(options))
    return parsers


def select_parser(url: str, parsers: List[BaseParser]) -> Optional[BaseParser]:
    """选择处理指定链接的解析器。

    :param url: 目标链接
    :param parsers: 可用解析器列表
    :return: 匹配的解析器，未匹配时返回 None
    """
    for parser in parsers:
        if parser.can_handle(url):
            return parser
    return None


def build_generic_parser(options: Optional[ClipNotesOptions] = None) -> BaseParser:
    """构建兜底解析器。

    :param options: 插件运行参数
    :return: 兜底解析器实例
    """
    return GenericParser(options)


__all__ = [
    "BaseParser",
    "HtmlConverter",
    "ParseError",
    "GenericParser",
    "WechatParser",
    "XiaohongshuParser",
    "DouyinParser",
    "build_parsers",
    "build_generic_parser",
    "select_parser",
]
