"""URL 规范化、平台识别与去重标识工具。"""
from __future__ import annotations

import hashlib
import re
from typing import Dict, List, Optional, Set
from urllib.parse import parse_qsl, unquote, urlencode, urljoin, urlparse, urlunparse

from .models import Platform

# 短链域名，需要先解析跳转再判断平台与去重
SHORT_LINK_HOSTS = ("xhslink.com", "xhslink.cn", "v.douyin.com")

# 解析短链时使用的 User-Agent
SHORT_LINK_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)

# 去重时需要忽略的易变参数（每次分享都会变化，但不影响同一篇内容）
VOLATILE_PARAMS: Set[str] = {"xsec_token", "xsec_source"}

# 需要剔除的通用跟踪参数
TRACKING_PARAMS: Set[str] = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "utm_id",
    "spm",
    "scm",
    "share_token",
    "share_medium",
    "share_plat",
    "share_source",
    "share_tag",
    "from_source",
    "from",
    "isappinstalled",
    "appuid",
    "apptime",
    "app_id",
    "timestamp",
    "xhsshare",
    "s_channel",
    "s_trans",
    "buvid",
    "vd_source",
    "wtime",
    "wxshare",
}

# 各平台需要保留的查询参数（None 表示全部保留）
PLATFORM_KEEP_PARAMS: Dict[str, Optional[Set[str]]] = {
    "mp.weixin.qq.com": {"__biz", "mid", "idx", "sn", "chksm", "scene", "subscene"},
    # 小红书笔记 ID 在路径中，xsec_token 是访问凭证但会过期，只需保留用于抓取
    "www.xiaohongshu.com": {"xsec_token", "xsec_source"},
    "xiaohongshu.com": {"xsec_token", "xsec_source"},
}

# 平台域名匹配规则，未命中的链接统一走兜底解析器
PLATFORM_DOMAINS: List[tuple] = [
    (Platform.WECHAT.value, ("mp.weixin.qq.com", "weixin.qq.com")),
    (Platform.XIAOHONGSHU.value, ("xiaohongshu.com", "xhslink.com", "xhslink.cn")),
    (Platform.DOUYIN.value, ("douyin.com", "iesdouyin.com")),
]


def normalize_url(url: str) -> str:
    """规范化 URL，去掉无意义的跟踪参数与片段。

    :param url: 原始 URL
    :return: 规范化后的 URL
    """
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("//"):
        raw = f"https:{raw}"
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", raw):
        raw = f"https://{raw}"
    try:
        parsed = urlparse(raw)
    except ValueError:
        return raw
    scheme = (parsed.scheme or "https").lower()
    netloc = (parsed.netloc or "").lower()
    if netloc.endswith(":80") and scheme == "http":
        netloc = netloc[:-3]
    if netloc.endswith(":443") and scheme == "https":
        netloc = netloc[:-4]
    path = re.sub(r"/+", "/", unquote(parsed.path or "/"))
    if len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
    keep_params = PLATFORM_KEEP_PARAMS.get(netloc, set())
    query_pairs = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=False):
        if key.lower() in TRACKING_PARAMS:
            continue
        if keep_params is not None and key not in keep_params:
            continue
        query_pairs.append((key, value))
    query_pairs.sort()
    query = urlencode(query_pairs)
    return urlunparse((scheme, netloc, path, "", query, ""))


def detect_platform(url: str) -> str:
    """根据域名识别内容平台。

    :param url: 原始 URL
    :return: 平台标识
    """
    try:
        netloc = urlparse(normalize_url(url)).netloc.lower()
    except ValueError:
        netloc = ""
    if not netloc:
        return Platform.GENERIC.value
    for platform, domains in PLATFORM_DOMAINS:
        for domain in domains:
            if netloc == domain or netloc.endswith(f".{domain}"):
                return platform
    return Platform.GENERIC.value


def url_hash(url: str, length: int = 16) -> str:
    """计算 URL 的稳定去重标识。

    使用剔除易变参数后的形式计算，保证同一篇内容的不同分享链接命同一个标识。

    :param url: 原始 URL
    :param length: 返回的哈希长度
    :return: 十六进制哈希字符串
    """
    digest = hashlib.sha256(dedup_url(url).encode("utf-8")).hexdigest()
    return digest[: max(8, length)]


def dedup_url(url: str) -> str:
    """返回用于去重的 URL 形式，剔除每次分享都会变化的参数。

    :param url: 原始 URL
    :return: 去重用的 URL
    """
    normalized = normalize_url(url)
    if not normalized:
        return ""
    try:
        parsed = urlparse(normalized)
    except ValueError:
        return normalized
    pairs = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=False)
        if key not in VOLATILE_PARAMS
    ]
    pairs.sort()
    return urlunparse(
        (parsed.scheme, parsed.netloc, parsed.path, "", urlencode(pairs), "")
    )


def is_short_link(url: str) -> bool:
    """判断是否为需要解析跳转的短链。

    :param url: 原始 URL
    :return: 是否为短链
    """
    try:
        netloc = urlparse(normalize_url(url)).netloc.lower()
    except ValueError:
        return False
    return any(netloc == host or netloc.endswith(f".{host}") for host in SHORT_LINK_HOSTS)


def resolve_short_url(url: str, timeout: int = 12) -> str:
    """同步解析短链跳转后的真实地址，失败时返回原链接。

    :param url: 短链地址
    :param timeout: 超时秒数
    :return: 解析后的地址
    """
    if not is_short_link(url):
        return url
    try:
        import httpx

        with httpx.Client(
            follow_redirects=True,
            timeout=timeout,
            proxy=resolve_proxy(),
        ) as client:
            response = client.get(
                url,
                headers={
                    "User-Agent": SHORT_LINK_USER_AGENT,
                    "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
                },
            )
        final_url = str(response.url) if response.url else ""
        return final_url or url
    except Exception:
        return url


def resolve_proxy() -> Optional[str]:
    """读取 MoviePilot 系统代理配置并转换为 httpx 可用的代理地址。

    :return: 代理地址，未配置时返回 None
    """
    try:
        from app.core.config import settings

        proxy = getattr(settings, "PROXY", None)
    except Exception:
        return None
    if not proxy:
        return None
    if isinstance(proxy, str):
        return proxy.strip() or None
    if isinstance(proxy, dict):
        for key in ("https", "http", "all"):
            value = proxy.get(key)
            if value:
                return str(value).strip() or None
        return None
    if isinstance(proxy, (list, tuple)):
        for item in proxy:
            if item:
                return str(item).strip() or None
        return None
    return None


def normalize_asset_url(url: str, base_url: str = "") -> str:
    """规范化资源地址，补全协议相对地址与相对路径。

    :param url: 资源地址
    :param base_url: 用于补全相对地址的基础链接
    :return: 可直接请求的绝对地址
    """
    value = (url or "").strip()
    if not value:
        return ""
    if value.startswith("data:"):
        return ""
    if value.startswith("//"):
        return f"https:{value}"
    if base_url and not value.startswith(("http://", "https://")):
        return urljoin(base_url, value)
    return value


def content_hash(value: str, length: int = 16) -> str:
    """计算任意文本的稳定哈希，用于资源文件命名。

    :param value: 需要计算哈希的文本
    :param length: 返回的哈希长度
    :return: 十六进制哈希字符串
    """
    digest = hashlib.sha256((value or "").encode("utf-8")).hexdigest()
    return digest[: max(8, length)]


def asset_key_url(url: str) -> str:
    """生成资源去重用的 URL 形式。

    小红书等 CDN 会在图片地址中插入时间戳与本次请求签名，同一张图每次抓取地址
    都不同，只有路径末段的内容标识是稳定的，因此对该类 CDN 只保留末段。

    :param url: 图片地址
    :return: 用于计算资源标识的地址
    """
    value = (url or "").strip()
    if not value:
        return ""
    try:
        parsed = urlparse(value)
    except ValueError:
        return value
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host.endswith("xhscdn.com"):
        token = path.rsplit("/", 1)[-1]
        return f"{parsed.scheme}://{host}/{token}" if token else value
    return urlunparse((parsed.scheme, host, path, "", parsed.query, ""))


def guess_extension(url: str, content_type: str = "") -> str:
    """根据 URL 或响应类型推断资源文件扩展名。

    :param url: 资源地址
    :param content_type: HTTP 响应类型
    :return: 扩展名（含点）
    """
    mapping = {
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
        "image/png": ".png",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/bmp": ".bmp",
        "image/svg+xml": ".svg",
        "image/avif": ".avif",
    }
    normalized_type = (content_type or "").split(";")[0].strip().lower()
    if normalized_type in mapping:
        return mapping[normalized_type]
    lower_url = (url or "").lower()
    for ext in (".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif", ".svg"):
        if lower_url.endswith(ext) or f"{ext}?" in lower_url:
            return ".jpg" if ext == ".jpeg" else ext
    fmt_match = re.search(r"(?:wx_fmt|format)=([a-zA-Z0-9]+)", lower_url)
    if fmt_match:
        fmt = fmt_match.group(1).lower()
        if f".{fmt}" in (".jpg", ".jpeg", ".png", ".gif", ".webp"):
            return ".jpg" if fmt == "jpeg" else f".{fmt}"
    return ".jpg"


def is_valid_http_url(url: str) -> bool:
    """判断是否为可访问的 HTTP(S) 链接。

    :param url: 待判断的链接
    :return: 是否有效
    """
    try:
        parsed = urlparse(normalize_url(url))
    except ValueError:
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.netloc)


def extract_first_url(text: str) -> Optional[str]:
    """从文本中提取第一个 URL。

    :param text: 待解析文本
    :return: 提取到的 URL，未找到时返回 None
    """
    if not text:
        return None
    match = re.search(r"https?://[^\s<>\"'）)】\]]+", text)
    if match:
        return match.group(0)
    match = re.search(
        r"(?:mp\.weixin\.qq\.com|xiaohongshu\.com|xhslink\.(?:com|cn)|v\.douyin\.com|douyin\.com)[^\s]+",
        text,
    )
    if match:
        return f"https://{match.group(0)}"
    return None
