"""解析器基类、异常定义与通用 HTML 转换工具。"""
from __future__ import annotations

import abc
import re
from typing import Any, Dict, List, Optional
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup, NavigableString, Tag

from ..models import ClipNotesOptions, ContentItem, Platform
from ..urlutils import normalize_asset_url, resolve_proxy

# 默认浏览器 UA
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 空内容判定阈值
MIN_CONTENT_LENGTH = 40


class ParseError(Exception):
    """内容解析失败异常，用于把站点差异收敛为统一错误。"""

    def __init__(self, platform: str, message: str, url: str = "", status_code: int = 0):
        """初始化解析异常。

        :param platform: 平台标识
        :param message: 失败原因
        :param url: 出错的链接
        :param status_code: HTTP 状态码
        """
        super().__init__(message)
        self.platform = platform
        self.message = message
        self.url = url
        self.status_code = status_code

    def __str__(self) -> str:
        """返回可读的异常描述。"""
        parts = [f"[{self.platform}] {self.message}"]
        if self.status_code:
            parts.append(f"HTTP {self.status_code}")
        if self.url:
            parts.append(self.url)
        return " | ".join(parts)


class HtmlConverter:
    """将正文 HTML 转换为结构化 Markdown。"""

    # 需要整体跳过的标签
    DROP_TAGS = {"script", "style", "noscript", "iframe", "svg", "button", "form"}

    def convert(self, node: Any) -> str:
        """将 HTML 节点转换为 Markdown 文本。

        :param node: BeautifulSoup 节点
        :return: Markdown 文本
        """
        if node is None:
            return ""
        text = self._block(node)
        text = re.sub(r"[ \t]+\n", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    def _children(self, node: Tag) -> List[Any]:
        """过滤出有效子节点。"""
        return [child for child in node.children if self._keep(child)]

    @staticmethod
    def _keep(child: Any) -> bool:
        """判断子节点是否需要保留。"""
        if isinstance(child, NavigableString):
            return True
        if isinstance(child, Tag):
            return child.name.lower() not in HtmlConverter.DROP_TAGS
        return False

    def _inline(self, node: Any) -> str:
        """转换行内内容。"""
        if isinstance(node, NavigableString):
            return re.sub(r"\s+", " ", str(node))
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        if name in self.DROP_TAGS:
            return ""
        if name in ("strong", "b"):
            inner = self._inline_children(node).strip()
            return f"**{inner}**" if inner else ""
        if name in ("em", "i"):
            inner = self._inline_children(node).strip()
            return f"*{inner}*" if inner else ""
        if name == "br":
            return "\n"
        if name == "code":
            inner = node.get_text().strip()
            return f"`{inner}`" if inner else ""
        if name == "a":
            text = self._inline_children(node).strip()
            href = str(node.get("href") or "").strip()
            if href and text:
                return f"[{text}]({href})"
            return text
        if name == "img":
            return self._image_markdown(node)
        if name in ("span", "font", "sub", "sup", "label", "small", "u", "s", "del"):
            return self._inline_children(node)
        return self._inline_children(node)

    def _inline_children(self, node: Tag) -> str:
        """转换全部子节点的行内内容。"""
        return "".join(self._inline(child) for child in self._children(node))

    @staticmethod
    def image_url(node: Tag) -> str:
        """提取图片标签中的真实地址。"""
        for attr in ("data-src", "data-original", "data-actualsrc", "data-echo", "src"):
            value = node.get(attr)
            if value:
                normalized = normalize_asset_url(str(value))
                if normalized:
                    return normalized
        return ""

    def _image_markdown(self, node: Tag) -> str:
        """生成图片 Markdown。"""
        url = self.image_url(node)
        if not url:
            return ""
        alt = str(node.get("alt") or "").strip()
        return f"![{alt}]({url})"

    def _list(self, node: Tag, ordered: bool) -> str:
        """转换有序或无序列表。"""
        lines = []
        index = 1
        for child in self._children(node):
            if not isinstance(child, Tag) or child.name.lower() != "li":
                continue
            inner = self._block(child).strip()
            inner = re.sub(r"\n{2,}", "\n", inner)
            if not inner:
                # 跳过微信代码块用于占位的空列表项
                continue
            prefix = f"{index}. " if ordered else "- "
            inner = inner.replace("\n", "\n  ")
            lines.append(f"{prefix}{inner}")
            index += 1
        return "\n".join(lines) + "\n\n" if lines else ""

    def _quote(self, node: Tag) -> str:
        """转换引用块。"""
        inner = self._container(node).strip()
        if not inner:
            return ""
        quoted = "\n".join(f"> {line}" if line else ">" for line in inner.splitlines())
        return f"{quoted}\n\n"

    def _code_block(self, node: Tag) -> str:
        """转换代码块，兼容微信公众号按行包裹子元素的结构。"""
        codes = [
            child
            for child in node.find_all("code")
            if isinstance(child, Tag) and not child.find("code")
        ]
        lines: Optional[List[str]] = None
        if len(codes) > 1:
            # 每个 code 元素代表一行
            lines = [code.get_text() for code in codes]
        if lines is None:
            single = codes[0] if codes else node
            spans = [
                child
                for child in single.children
                if isinstance(child, Tag) and child.name in ("span", "div", "section")
            ]
            if len(spans) > 1:
                # 单个 code 内用多个行内元素承载每一行
                lines = [span.get_text() for span in spans]
        if lines is None:
            for br in node.find_all("br"):
                br.replace_with(NavigableString("\n"))
            lines = node.get_text().splitlines()
        text = "\n".join(line.rstrip() for line in lines)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        return f"```\n{text}\n```\n\n" if text else ""

    def _container(self, node: Tag) -> str:
        """按块级规则递归转换容器节点。"""
        parts: List[str] = []
        for child in self._children(node):
            if isinstance(child, Tag) and child.name.lower() in {
                "p",
                "div",
                "section",
                "article",
                "h1",
                "h2",
                "h3",
                "h4",
                "h5",
                "h6",
                "ul",
                "ol",
                "blockquote",
                "pre",
                "table",
                "hr",
                "figure",
                "img",
            }:
                parts.append(self._block(child))
            else:
                inline = self._inline(child)
                if inline.strip():
                    parts.append(f"{inline.strip()}\n\n")
        return "".join(parts)

    def _table(self, node: Tag) -> str:
        """尽力转换表格。"""
        rows: List[str] = []
        for tr in node.find_all("tr"):
            cells = [
                re.sub(r"\s+", " ", cell.get_text(" ", strip=True)).replace("|", "\\|")
                for cell in tr.find_all(["td", "th"])
            ]
            if cells:
                rows.append("| " + " | ".join(cells) + " |")
        return "\n".join(rows) + "\n\n" if rows else ""

    def _block(self, node: Any) -> str:
        """转换块级内容。"""
        if isinstance(node, NavigableString):
            text = re.sub(r"\s+", " ", str(node))
            return text if text.strip() else ""
        if not isinstance(node, Tag):
            return ""
        name = node.name.lower()
        if name in self.DROP_TAGS:
            return ""
        if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
            level = int(name[1])
            inner = self._inline_children(node).strip()
            return f"{'#' * level} {inner}\n\n" if inner else ""
        if name == "p":
            inner = self._inline_children(node).strip()
            return f"{inner}\n\n" if inner else ""
        if name == "blockquote":
            return self._quote(node)
        if name == "ul":
            return self._list(node, ordered=False)
        if name == "ol":
            return self._list(node, ordered=True)
        if name == "pre":
            return self._code_block(node)
        if name == "table":
            return self._table(node)
        if name == "hr":
            return "---\n\n"
        if name in ("figure", "figcaption"):
            inner = self._inline_children(node).strip()
            return f"{inner}\n\n" if inner else ""
        if name == "img":
            markdown = self._image_markdown(node)
            return f"{markdown}\n\n" if markdown else ""
        # 通用容器：递归块级转换
        return self._container(node)

    def extract_images(self, node: Any, base_url: str = "") -> List[str]:
        """提取正文中的全部图片地址。

        :param node: 正文节点
        :param base_url: 用于补全相对地址的基础链接
        :return: 去重后的图片地址列表
        """
        if node is None:
            return []
        images: List[str] = []
        for img in node.find_all("img"):
            url = self.image_url(img)
            if not url:
                continue
            if base_url:
                url = urljoin(base_url, url)
            if url not in images:
                images.append(url)
        return images


class BaseParser(abc.ABC):
    """解析器抽象基类。"""

    # 平台标识
    platform: str = Platform.GENERIC.value
    # 可处理的域名
    domains: tuple = ()
    # 站点名称
    site_name: str = ""

    def __init__(self, options: Optional[ClipNotesOptions] = None):
        """初始化解析器。

        :param options: 插件运行参数
        """
        self.options = options or ClipNotesOptions()
        self.converter = HtmlConverter()

    @classmethod
    def can_handle(cls, url: str) -> bool:
        """判断是否由当前解析器处理该链接。

        :param url: 目标链接
        :return: 是否可处理
        """
        from ..urlutils import normalize_url

        try:
            from urllib.parse import urlparse

            netloc = urlparse(normalize_url(url)).netloc.lower()
        except ValueError:
            return False
        if not netloc:
            return False
        return any(netloc == domain or netloc.endswith(f".{domain}") for domain in cls.domains)

    @abc.abstractmethod
    async def parse(self, url: str) -> ContentItem:
        """抓取并解析目标链接。

        :param url: 目标链接
        :return: 标准化内容
        :raises ParseError: 解析失败
        """
        raise NotImplementedError

    @property
    def user_agent(self) -> str:
        """返回本次抓取使用的 User-Agent。"""
        return self.options.user_agent or DEFAULT_USER_AGENT

    @property
    def proxy(self) -> Optional[str]:
        """返回系统代理地址，未配置时返回 None。"""
        return resolve_proxy()

    def default_headers(self, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
        """构造请求头。

        :param extra: 需要附加的请求头
        :return: 请求头字典
        """
        headers = {
            "User-Agent": self.user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        if extra:
            headers.update({k: v for k, v in extra.items() if v})
        return headers

    async def fetch_text(
        self,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        cookie: str = "",
    ) -> str:
        """抓取页面 HTML 文本。

        :param url: 目标链接
        :param headers: 附加请求头
        :param cookie: Cookie 字符串
        :return: 页面 HTML
        :raises ParseError: 请求失败
        """
        merged = self.default_headers(headers)
        if cookie:
            merged["Cookie"] = cookie
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.options.request_timeout,
                proxy=self.proxy,
            ) as client:
                response = await client.get(url, headers=merged)
        except Exception as err:
            raise ParseError(self.platform, f"请求失败：{err}", url) from err
        if response.status_code >= 400:
            raise ParseError(
                self.platform,
                "目标站点返回错误状态",
                url,
                status_code=response.status_code,
            )
        return response.text

    async def fetch_bytes(
        self,
        url: str,
        referer: str = "",
    ) -> tuple:
        """下载二进制资源。

        :param url: 资源地址
        :param referer: 防盗链所需的来源地址
        :return: (内容字节, 响应类型)
        :raises ParseError: 下载失败
        """
        headers = self.default_headers(
            {
                "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                "Referer": referer,
            }
        )
        try:
            async with httpx.AsyncClient(
                follow_redirects=True,
                timeout=self.options.request_timeout,
                proxy=self.proxy,
            ) as client:
                response = await client.get(url, headers=headers)
        except Exception as err:
            raise ParseError(self.platform, f"资源下载失败：{err}", url) from err
        if response.status_code >= 400:
            raise ParseError(
                self.platform,
                "资源下载返回错误状态",
                url,
                status_code=response.status_code,
            )
        return response.content, response.headers.get("content-type", "")

    @staticmethod
    def soup(html: str) -> BeautifulSoup:
        """构造 BeautifulSoup 对象。

        :param html: 页面 HTML
        :return: BeautifulSoup 实例
        """
        return BeautifulSoup(html or "", "html.parser")

    @staticmethod
    def meta_content(soup: BeautifulSoup, *names: str) -> str:
        """读取页面 meta 信息。

        :param soup: BeautifulSoup 实例
        :param names: meta 的 property 或 name 值
        :return: meta 内容
        """
        for name in names:
            for attr in ("property", "name", "itemprop"):
                node = soup.find("meta", attrs={attr: name})
                if node and node.get("content"):
                    return str(node.get("content")).strip()
        return ""

    @staticmethod
    def text_of(node: Any) -> str:
        """提取节点纯文本。

        :param node: BeautifulSoup 节点
        :return: 纯文本
        """
        if node is None:
            return ""
        return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip()

    def ensure_content(self, content: str, url: str) -> str:
        """校验正文是否有效。

        :param content: 已转换的正文
        :param url: 目标链接
        :return: 校验通过的正文
        :raises ParseError: 正文为空或过短
        """
        if len(re.sub(r"\s+", "", content or "")) < MIN_CONTENT_LENGTH:
            raise ParseError(self.platform, "未能提取到有效正文，可能需要登录或站点结构已变更", url)
        return content
