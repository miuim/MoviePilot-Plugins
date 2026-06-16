import random
import re
import time
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytz
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from app.core.config import settings
from app.core.event import eventmanager, Event
from app.plugins import _PluginBase
from typing import Any, List, Dict, Tuple, Optional
from app.log import logger
from app.schemas.types import EventType
from app.utils.http import RequestUtils
from app.schemas import Notification, NotificationType, MessageChannel


class CnlangSigninMiu(_PluginBase):
    # 插件名称
    plugin_name = "国语视界签到 Miu"
    # 插件描述
    plugin_desc = "国语视界论坛自动签到，支持随机延迟。移除远程获取随机心情。"
    # 插件图标
    plugin_icon = "https://raw.githubusercontent.com/miuim/MoviePilot-Plugins/main/icons/cnlang.png"
    # 插件版本
    plugin_version = "1.2.1"
    # 插件作者
    plugin_author = "imaliang,Miu"
    # 作者主页
    author_url = "https://github.com/miuim"
    # 插件配置项ID前缀
    plugin_config_prefix = "cnalngsigninmiu_"
    # 加载顺序
    plugin_order = 3
    # 可使用的用户级别
    auth_level = 1

    # 私有属性
    _enabled = False
    # 任务执行间隔
    _cron = None
    _cookie = None
    _onlyonce = False
    _notify = False
    _history_days = None
    _random_delay = None
    _clear = False
    # 定时器
    _scheduler: Optional[BackgroundScheduler] = None

    def init_plugin(self, config: dict = None):
        # 停止现有任务
        self.stop_service()

        if config:
            self._enabled = config.get("enabled")
            self._cron = config.get("cron")
            self._cookie = config.get("cookie")
            self._notify = config.get("notify")
            self._onlyonce = config.get("onlyonce")
            self._history_days = config.get("history_days") or 30
            self._random_delay = config.get("random_delay")
            self._clear = config.get("clear")

        # 清除历史
        if self._clear:
            self.del_data('history')
            self._clear = False
            self.__update_config()

        if self._onlyonce:
            # 定时服务
            self._scheduler = BackgroundScheduler(timezone=settings.TZ)
            logger.info(f"国语视界签到服务启动，立即运行一次")
            self._scheduler.add_job(func=self.signin, trigger='date',
                                    run_date=datetime.now(tz=pytz.timezone(
                                        settings.TZ)) + timedelta(seconds=5),
                                    name="国语视界签到")
            # 关闭一次性开关
            self._onlyonce = False
            self.__update_config()

            # 启动任务
            if self._scheduler.get_jobs():
                self._scheduler.print_jobs()
                self._scheduler.start()

    def __update_config(self):
        self.update_config({
            "onlyonce": False,
            "cron": self._cron,
            "enabled": self._enabled,
            "cookie": self._cookie,
            "notify": self._notify,
            "history_days": self._history_days,
            "random_delay": self._random_delay,
            "clear": self._clear
        })

    def __send_fail_msg(self, text):
        logger.info(text)
        if self._notify:
            sign_time = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
            self.post_message(
                mtype=NotificationType.Plugin,
                title="🏷︎ 国语视界签到 ✴️",
                text=f"执行时间：{sign_time}\n"
                f"{text}")

    def __send_success_msg(self, text):
        logger.info(text)
        if self._notify:
            self.post_message(
                mtype=NotificationType.Plugin,
                title="🏷︎ 国语视界签到 ✅",
                text=text)

    @eventmanager.register(EventType.PluginAction)
    def signin(self, event: Event = None):
        """
        国语视界签到
        """
        if event:
            event_data = event.event_data
            if not event_data or event_data.get("action") != "cnlang_signin":
                return
            logger.info("收到命令，开始执行...")

        _url = "cnlang.org"
        headers = {'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
                   'Accept - Encoding': 'gzip, deflate, br',
                   'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
                   'cache-control': 'max-age=0',
                   'Upgrade-Insecure-Requests': '1',
                   'Host': _url,
                   'Cookie': self._cookie,
                   'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/97.0.4692.71 Safari/537.36 Edg/97.0.1072.62'}

        res = RequestUtils(headers=headers).get_res(
            url='https://' + _url + '/dsu_paulsign-sign.html?mobile=no')
        if not res or res.status_code != 200:
            self.__send_fail_msg("获取基本信息失败-status_code=" + res.status_code)
            return

        user_info = res.text
        user_name = re.search(r'title="访问我的空间">(.*?)</a>', user_info)
        if user_name:
            user_name = user_name.group(1)
            logger.info("登录用户名为：" + user_name)
        else:
            self.__send_fail_msg("未获取到用户名-cookie或许已失效")
            return

        is_sign = re.search(r'(您今天已经签到过了或者签到时间还未开始)', user_info)
        if is_sign:
            self.__send_success_msg("您今天已经签到过了或者签到时间还未开始")
            return

        # 使用正则表达式查找 formhash 的值
        formhash_value = re.search(
            r'<input[^>]*name="formhash"[^>]*value="([^"]*)"', user_info)

        if formhash_value:
            formhash_value = formhash_value.group(1)
            logger.info("formhash：" + formhash_value)
        else:
            self.__send_fail_msg("未获取到 formhash 值")
            return

        totalContinuousCheckIn = re.search(
            r'<p>您本月已累计签到:<b>(.*?)</b>', user_info)
        if totalContinuousCheckIn:
            totalContinuousCheckIn = int(totalContinuousCheckIn.group(1)) + 1
            logger.info(f"您本月已累计签到：{totalContinuousCheckIn}")
        else:
            totalContinuousCheckIn = 1

        # 随机获取心情 - 使用英文避免编码问题
        mood_list = [
            "morning~",
            "morning~~",
            "Morning~",
            "Morning~~",
            "Hello morning",
            "hello morning",
            "hello morning~",
            "Hello morning~",
            "hello morning~~",
            "Hello morning~~",
            "good morning~",
            "good morning~~",
            "good morning",
            "morning lalalala"
        ]
        xq = random.choice(mood_list)
        logger.info("随机选择的心情：" + xq)

        # 获取签到链接,并签到
        qd_url = 'plugin.php?id=dsu_paulsign:sign&operation=qiandao&infloat=1'

        qd_data = {
            "formhash": formhash_value,
            "qdxq": "kx",
            "qdmode": "1",
            "todaysay": xq,
            "fastreply": "0",
        }

        # 开始签到
        res = RequestUtils(headers=headers).post_res(
            url=f"https://{_url}/{qd_url}", data=qd_data)
        if not res or res.status_code != 200:
            self.__send_fail_msg("请求签到接口失败-status_code=" + res.status_code)
            return

        sign_html = res.text

        # 使用正则表达式查找 class 为 'c' 的 div 标签中的内容
        content = re.search(r'<div class="c">(.*?)</div>',
                            sign_html, re.DOTALL)
        if content:
            content = content.group(1).strip()
            logger.info(content)
        else:
            self.__send_fail_msg("获取签到后的响应内容失败")
            return

        # 获取积分
        user_info = RequestUtils(headers=headers).get_res(
            url=f'https://{_url}/home.php?mod=spacecp&ac=credit&showcredit=1&inajax=1&ajaxtarget=extcreditmenu_menu').text

        money = re.search(
            r'<span id="hcredit_2">(\d+)</span>', user_info).group(1)

        logger.info(f"当前大洋余额：{money}")

        sign_time = datetime.today().strftime('%Y-%m-%d %H:%M:%S')
        text = (f"签到账号：{user_name}\n"
                f"累计签到：{totalContinuousCheckIn} 天\n"
                f"当前大洋：{money}\n"
                f"签到时间：{sign_time}\n"
                f"{content}")
        # 发送通知
        self.__send_success_msg(text)

        # 读取历史记录
        history = self.get_data('history') or []

        history.append({
            "date": sign_time,
            "username": user_name,
            "totalContinuousCheckIn": totalContinuousCheckIn,
            "money": money,
            "content": content,
        })

        thirty_days_ago = time.time() - int(self._history_days) * 24 * 60 * 60
        history = [record for record in history if
                   datetime.strptime(record["date"],
                                     '%Y-%m-%d %H:%M:%S').timestamp() >= thirty_days_ago]
        # 保存签到历史
        self.save_data(key="history", value=history)

    def __add_task(self):
        """
        增加任务
        """
        random_seconds = 5
        if self._random_delay:
            # 拆分字符串获取范围
            start, end = map(int, self._random_delay.split('-'))
            # 生成随机秒数
            random_seconds = random.randint(start, end)

        self._scheduler = BackgroundScheduler(timezone=settings.TZ)
        logger.info(f"增加国语视界签到任务，{random_seconds}s后执行...")
        self._scheduler.add_job(func=self.signin, trigger='date',
                                run_date=datetime.now(tz=pytz.timezone(
                                    settings.TZ)) + timedelta(seconds=random_seconds),
                                name="国语视界签到")
        # 启动任务
        if self._scheduler.get_jobs():
            self._scheduler.print_jobs()
            self._scheduler.start()

    def get_state(self) -> bool:
        return self._enabled

    @staticmethod
    def get_command() -> List[Dict[str, Any]]:
        """
        定义远程控制命令
        :return: 命令关键字、事件、描述、附带数据
        """
        return [{
            "cmd": "/cnlang_signin",
            "event": EventType.PluginAction,
            "desc": "国语视界签到",
            "category": "站点",
            "data": {
                "action": "cnlang_signin"
            }
        }]

    def get_api(self) -> List[Dict[str, Any]]:
        pass

    def get_service(self) -> List[Dict[str, Any]]:
        """
        注册插件公共服务
        [{
            "id": "服务ID",
            "name": "服务名称",
            "trigger": "触发器：cron/interval/date/CronTrigger.from_crontab()",
            "func": self.xxx,
            "kwargs": {} # 定时器参数
        }]
        """
        if self._enabled and self._cron:
            return [{
                "id": "CnlangSigninMiu",
                "name": "国语视界签到服务",
                "trigger": CronTrigger.from_crontab(self._cron),
                "func": self.__add_task,
                "kwargs": {}
            }]
        return []

    def get_form(self) -> Tuple[List[dict], Dict[str, Any]]:
        """
        拼装插件配置页面，需要返回两块数据：1、页面配置；2、数据结构
        """
        return [
            {
                'component': 'VForm',
                'content': [
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'enabled',
                                            'label': '启用插件',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'notify',
                                            'label': '开启通知',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'onlyonce',
                                            'label': '立即运行一次',
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VSwitch',
                                        'props': {
                                            'model': 'clear',
                                            'label': '清除历史记录',
                                        }
                                    }
                                ]
                            },
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'cron',
                                            'label': '签到周期'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 6
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'random_delay',
                                            'label': '随机延迟(秒)',
                                            'placeholder': '100-200 随机延迟100-200秒'
                                        }
                                    }
                                ]
                            },
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                    'md': 3
                                },
                                'content': [
                                    {
                                        'component': 'VTextField',
                                        'props': {
                                            'model': 'history_days',
                                            'label': '保留历史天数'
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12
                                },
                                'content': [
                                    {
                                        'component': 'VTextarea',
                                        'props': {
                                            'model': 'cookie',
                                            'label': '国语视界 Cookie',
                                            'rows': 5,
                                            'placeholder': ''
                                        }
                                    }
                                ]
                            }
                        ]
                    },
                    {
                        'component': 'VRow',
                        'content': [
                            {
                                'component': 'VCol',
                                'props': {
                                    'cols': 12,
                                },
                                'content': [
                                    {
                                        'component': 'VAlert',
                                        'props': {
                                            'type': 'info',
                                            'variant': 'tonal',
                                            'text': '建议将随机延迟调大，以防被风控。'
                                        }
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ], {
            "enabled": False,
            "onlyonce": False,
            "notify": False,
            "clear": False,
            "cookie": "",
            "random_delay": "",
            "history_days": 30,
            "cron": "0 7 * * *"
        }

    def get_page(self) -> List[dict]:
        # 查询同步详情
        historys = self.get_data('history')
        if not historys:
            return [
                {
                    'component': 'div',
                    'text': '暂无数据',
                    'props': {
                        'class': 'text-center',
                    }
                }
            ]

        if not isinstance(historys, list):
            historys = [historys]

        # 按照签到时间倒序
        historys = sorted(historys, key=lambda x: x.get(
            "date") or 0, reverse=True)

        # 签到消息
        sign_msgs = [
            {
                'component': 'tr',
                'props': {
                    'class': 'text-sm'
                },
                'content': [
                    {
                        'component': 'td',
                        'props': {
                            'class': 'whitespace-nowrap break-keep text-high-emphasis'
                        },
                        'text': history.get("date")
                    },
                    {
                        'component': 'td',
                        'text': history.get("username")
                    },
                    {
                        'component': 'td',
                        'text': history.get("totalContinuousCheckIn")
                    },
                    {
                        'component': 'td',
                        'text': history.get("money")
                    },
                    {
                        'component': 'td',
                        'text': history.get("content")
                    }
                ]
            } for history in historys
        ]

        # 拼装页面
        return [
            {
                'component': 'VRow',
                'content': [
                    {
                        'component': 'VCol',
                        'props': {
                            'cols': 12,
                        },
                        'content': [
                            {
                                'component': 'VTable',
                                'props': {
                                    'hover': True
                                },
                                'content': [
                                    {
                                        'component': 'thead',
                                        'content': [
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '时间'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '账号'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '连续签到次数'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '当前大洋'
                                            },
                                            {
                                                'component': 'th',
                                                'props': {
                                                    'class': 'text-start ps-4'
                                                },
                                                'text': '响应'
                                            },
                                        ]
                                    },
                                    {
                                        'component': 'tbody',
                                        'content': sign_msgs
                                    }
                                ]
                            }
                        ]
                    }
                ]
            }
        ]

        pass

    def stop_service(self):
        """
        退出插件
        """
        try:
            if self._scheduler:
                self._scheduler.remove_all_jobs()
                if self._scheduler.running:
                    self._scheduler.shutdown()
                self._scheduler = None
        except Exception as e:
            logger.error("退出插件失败：%s" % str(e))

    def post_message(self, channel: MessageChannel = None, mtype: NotificationType = None, title: str = None,
                     text: str = None, image: str = None, link: str = None, userid: str = None):
        """
        发送消息
        """
        self.chain.post_message(Notification(
            channel=channel, mtype=mtype, title=title, text=text,
            image=image, link=link, userid=userid
        ))
