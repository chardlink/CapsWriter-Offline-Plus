# coding: utf-8
import os
import re
import sys
import time
import subprocess
from . import logger
from config_client import ClientConfig as Config


class TrayManager:
    """
    托盘管理器：负责系统托盘图标的初始化、菜单构建及回调处理。
    """
    def __init__(self, app):
        self.app = app
        self.server = None

    @property
    def state(self):
        return self.app.state

    def start(self):
        """初始化系统托盘图标"""
        if not Config.enable_tray:
            return

        try:
            from ..ui import enable_min_to_tray
        except ImportError as e:
            logger.warning(f"托盘模块导入失败，跳过托盘功能: {e}")
            return

        # 获取图标路径
        icon_path = os.path.join(self.app.base_dir, 'assets', 'icon.ico')
        server = getattr(self.app, 'server', None) or self.server

        # 注册悬浮窗通知回调，使托盘操作都走底部美化胶囊悬浮窗提示
        try:
            from core.ui.tray import set_notify_callback
            set_notify_callback(self._notify)
        except Exception:
            pass

        retention_menu = [
            (
                '1 天',
                lambda icon=None, item=None: self._set_retention_days(1),
                {'checked': lambda _: getattr(Config, 'auto_clean_days', 3) == 1},
            ),
            (
                '3 天 (推荐)',
                lambda icon=None, item=None: self._set_retention_days(3),
                {'checked': lambda _: getattr(Config, 'auto_clean_days', 3) == 3},
            ),
            (
                '7 天',
                lambda icon=None, item=None: self._set_retention_days(7),
                {'checked': lambda _: getattr(Config, 'auto_clean_days', 3) == 7},
            ),
            (
                '15 天',
                lambda icon=None, item=None: self._set_retention_days(15),
                {'checked': lambda _: getattr(Config, 'auto_clean_days', 3) == 15},
            ),
            (
                '30 天',
                lambda icon=None, item=None: self._set_retention_days(30),
                {'checked': lambda _: getattr(Config, 'auto_clean_days', 3) == 30},
            ),
            (
                '永久保留 (不自动清理)',
                lambda icon=None, item=None: self._set_retention_days(0),
                {'checked': lambda _: getattr(Config, 'auto_clean_days', 3) == 0},
            ),
        ]

        threshold_menu = [
            (
                f'{sec:g} 秒' + (' (默认)' if sec == 0.3 else ''),
                lambda icon=None, item=None, s=sec: self._set_threshold(s),
                {'checked': lambda _, s=sec: abs(getattr(Config, 'threshold', 0.3) - s) < 0.005},
            )
            for sec in (0.3, 0.5, 0.8)
        ]

        diary_submenu = [
            ('📂 打开当月日记与录音目录', self._open_diary),
            ('⏳ 自动清理保留天数', retention_menu),
            ('🗑️ 立即清理过期数据', self._clean_expired_now),
        ]

        client_menu = [
            ('📋 复制结果', self._copy_last_result),
            ('📁 日记与录音', diary_submenu),
            ('⏱️ 长按触发延时', threshold_menu),
            ('🎙️ 桌面悬浮语音按钮', self._toggle_floating_mic_menu, lambda item: bool(getattr(Config, 'floating_mic', False))),
            ('📝 上下文', self._add_context),
            ('✨ 热词', self._add_hotword),
            ('🧹 清除记忆 (AI对话上下文)', self._clear_memory),
            ('♻️ 重开音频', self._restart_audio),
            ('🔄 重启客户端', self._restart_client),
        ]

        if server is not None:
            server_menu = [
                ('✨ 服务端热词', self._open_server_hotword),
                ('📄 服务端日志', self._open_server_log),
                ('🔄 重启服务端', self._restart_server),
            ]
            tray_title = 'CapsWriter Offline (一体化)'
            more_options = [
                ('🖥️ 服务端 (Server)', server_menu),
                ('🎙️ 客户端 (Client)', client_menu),
            ]
            exit_cb = getattr(self.app, 'exit_all', None) or self.app.stop
        else:
            tray_title = 'CapsWriter Client'
            more_options = client_menu
            exit_cb = self.app.stop

        # 现代悬浮控制面板配置
        panel_config = {
            'title': 'CapsWriter',
            'badge': '一体化' if server is not None else '客户端',
            'on_exit': exit_cb,
            'client': {
                'actions': [
                    ('copy', 'copy', '复制结果', self._copy_last_result),
                    ('context', 'context', '编辑上下文', self._add_context),
                    ('hotword', 'hotword', '客户端热词', self._add_hotword),
                    ('clear_mem', 'eraser', '清除 AI 记忆', self._clear_memory),
                    ('audio', 'audio', '重开音频', self._restart_audio),
                    ('restart_client', 'refresh', '重启客户端', self._restart_client),
                ],
                'diary': {
                    'on_open': self._open_diary,
                    'on_clean_now': self._clean_expired_now,
                    'get_retention_days': lambda: getattr(Config, 'auto_clean_days', 3),
                    'set_retention_days': self._set_retention_days,
                },
                'get_threshold': lambda: getattr(Config, 'threshold', 0.3),
                'set_threshold': self._set_threshold,
                'get_floating_mic': lambda: bool(getattr(Config, 'floating_mic', False)),
                'on_toggle_floating_mic': self._set_floating_mic,
            },
        }
        if server is not None:
            def _get_server_status():
                try:
                    from config_server import ServerConfig
                    model_name = getattr(ServerConfig, 'model_type', 'unknown')
                    port = getattr(ServerConfig, 'port', '6016')
                except Exception:
                    model_name, port = 'ASR', '6016'
                busy = bool(getattr(server, '_restarting', False))
                title = '正在重启识别引擎…' if busy else '服务端运行中'
                return (title, f'模型: {model_name} · 端口: {port}', busy)

            panel_config['server'] = {
                'get_status': _get_server_status,
                'actions': [
                    ('srv_hotword', 'hotword', '服务端热词', 'hot-server.txt', self._open_server_hotword),
                    ('srv_log', 'log', '查看服务端日志', 'server_latest.log', self._open_server_log),
                    ('srv_restart', 'refresh', '重启识别引擎', '重载模型', self._restart_server),
                ],
            }

        # 启用托盘
        enable_min_to_tray(
            tray_title,
            icon_path,
            exit_callback=exit_cb,
            more_options=more_options,
            panel_config=panel_config,
        )
        logger.info("托盘图标已启用")

    def stop(self):
        """停止托盘图标"""
        if not Config.enable_tray:
            return
            
        try:
            from ..ui import stop_tray
            stop_tray()
            logger.info("TrayManager: 托盘图标已卸载")
        except Exception as e:
            logger.debug(f"TrayManager: 卸载托盘时发生错误: {e}")

    def _notify(self, message: str, theme: str = 'done', duration: float = 2.4):
        """使用底部胶囊悬浮窗展示优雅的系统通知（如悬浮窗未就绪则回退到 Toast）"""
        overlay = getattr(self.app, 'overlay', None)
        if overlay is not None and hasattr(overlay, 'notify'):
            overlay.notify(message, theme=theme, duration=duration)
            return
        from ..ui import toast
        bg = "#162032" if theme != 'error' else "#5b1924"
        toast(message, duration=int(duration * 1000), bg=bg)

    def _persist_client_config(self, key: str, value_str: str) -> None:
        """将数值型或布尔型配置项持久化写回 config_client.py"""
        try:
            cfg_path = os.path.join(self.app.base_dir, 'config_client.py')
            if not os.path.exists(cfg_path):
                return
            with open(cfg_path, 'r', encoding='utf-8') as f:
                content = f.read()
            new_content, count = re.subn(
                rf'(^\s*{re.escape(key)}\s*=\s*)(?:True|False|[0-9.]+)',
                rf'\g<1>{value_str}',
                content,
                count=1,
                flags=re.MULTILINE,
            )
            if count > 0 and new_content != content:
                with open(cfg_path, 'w', encoding='utf-8') as f:
                    f.write(new_content)
        except Exception as e:
            logger.warning(f"保存 {key} 到配置文件失败: {e}")

    def _toggle_floating_mic_menu(self) -> None:
        self._set_floating_mic(not bool(getattr(Config, 'floating_mic', False)))

    def _set_floating_mic(self, enabled: bool = None) -> None:
        """开启或隐藏桌面悬浮语音识别按钮，并双向同步 TrayPanel 开关与 config_client.py"""
        if enabled is None:
            new_val = not bool(getattr(Config, 'floating_mic', False))
        else:
            new_val = bool(enabled)

        Config.floating_mic = new_val
        self._persist_client_config('floating_mic', 'True' if new_val else 'False')

        if getattr(self.app, 'floating_mic', None) is not None:
            self.app.floating_mic.set_visible(new_val)

        # 若 TrayPanel 当前打开，立即刷新其滑动开关状态
        try:
            panel = getattr(self._tray_instance, '_panel', None)
            if panel is not None:
                panel._sync_live_state()
                panel._need_redraw = True
                if getattr(panel, 'hwnd', None):
                    panel._present(time.perf_counter())
        except Exception:
            pass

        if new_val:
            logger.info("已开启桌面悬浮语音按钮（点击开始/停止识别，支持自由拖动与右键隐藏）")
            self._notify("已开启桌面悬浮语音按钮（点击即录，可拖动）")
        else:
            logger.info("已隐藏桌面悬浮语音按钮")
            self._notify("已隐藏桌面悬浮语音按钮")

    def _set_threshold(self, seconds: float):
        """设置长按触发语音识别与弹框的延时（秒），立即生效并持久化写入 config_client.py"""
        val = round(float(seconds), 2)
        Config.threshold = val
        shortcut_mgr = getattr(self.app, 'shortcut', None)
        if shortcut_mgr and hasattr(shortcut_mgr, 'tasks'):
            for task in shortcut_mgr.tasks.values():
                task.threshold = task.shortcut.get_threshold(val)
        self._persist_client_config('threshold', f'{val:g}')
        self._notify(f"已设为按住 {val:g} 秒后弹出语音框并识别", theme='done')

    def _set_retention_days(self, days: int):
        """设置录音与日记保留天数，并持久化写入 config_client.py"""
        Config.auto_clean_days = days
        self._persist_client_config('auto_clean_days', str(days))

        if days > 0:
            deleted = self.app.diary.clean_expired(days)
            if deleted > 0:
                self._notify(f"已设为保留 {days} 天（清理了 {deleted} 个过期文件）", theme='done')
            else:
                self._notify(f"已设为自动清理 {days} 天前的录音与日记", theme='done')
        else:
            self._notify("已设为永久保留录音与日记", theme='done')

    def _clean_expired_now(self):
        """立即执行一次过期录音与日记清理"""
        days = getattr(Config, 'auto_clean_days', 3)
        if days <= 0:
            self._notify("当前为永久保留模式，未清理任何文件", theme='processing')
            return
        deleted = self.app.diary.clean_expired(days)
        if deleted > 0:
            self._notify(f"清理完成：彻底删除了 {deleted} 个 {days} 天前的文件", theme='done')
        else:
            self._notify(f"暂无超过 {days} 天的过期文件", theme='done')

    def _restart_audio(self):
        """重启音频流回调"""
        if hasattr(self.app, 'stream') and self.app.stream:
            self.app.stream.reopen()
            logger.info("用户请求重启音频")
            self._notify("麦克风音频流已重新开启", theme='done')

    def _clear_memory(self):
        """清除 LLM 对话历史回调"""
        if self.app.llm:
            self.app.llm.clear_history()
            self._notify("已清除 AI 角色的对话历史记忆", theme='done')

    def _open_diary(self):
        """用系统默认方式打开当月日记文件夹回调"""
        base = self.app.diary.base_path
        folder = base / time.strftime('%Y') / time.strftime('%m')
        folder.mkdir(parents=True, exist_ok=True)
        if sys.platform == 'win32':
            os.startfile(folder)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', folder])
        else:
            subprocess.Popen(['xdg-open', folder])

    def _add_hotword(self):
        """用系统默认方式打开热词文件回调"""
        
        target = os.path.abspath('hot.txt')
        if sys.platform == 'win32':
            os.startfile(target)
        elif sys.platform == 'darwin':
            subprocess.Popen(['open', target])
        else:
            subprocess.Popen(['xdg-open', target])

    def _add_context(self):
        """打开编辑上下文界面回调"""
        try:
            from ..ui import on_edit_context
            on_edit_context()
        except ImportError as e:
            logger.warning(f"无法导入上下文菜单处理器: {e}")

    def _copy_last_result(self):
        """复制最后一次识别结果到剪贴板回调"""
        text = self.state.last_output_text
        if text:
            from ..llm.llm_clipboard import copy_to_clipboard
            copy_to_clipboard(text)
            self._notify("已复制上一次识别结果到剪贴板", theme='done')
        else:
            self._notify("暂无可复制的识别结果", theme='processing')

    def _restart_client(self):
        """仅重启客户端组件（音频流、快捷键、热词与 LLM 角色）"""
        def _do():
            try:
                logger.info("用户请求重启客户端组件...")
                if hasattr(self.app, 'restart_client_components'):
                    self.app.restart_client_components()
                else:
                    self._restart_audio()
                self._notify("客户端组件已重新加载", theme='done')
            except Exception as e:
                logger.error(f"重启客户端组件失败: {e}", exc_info=True)
                self._notify(f"重启客户端失败: {e}", theme='error', duration=3.0)
        import threading
        threading.Thread(target=_do, daemon=True).start()

    def _restart_server(self):
        """仅重启服务端识别引擎"""
        server = getattr(self.app, 'server', None) or self.server
        if not server:
            return
        def _do():
            self._notify("正在重启服务端识别引擎…", theme='processing', duration=3.5)
            server.restart_worker()
            self._notify("服务端识别引擎已重启完成", theme='done')
        import threading
        threading.Thread(target=_do, daemon=True).start()

    def _open_server_hotword(self):
        """打开服务端热词文件 hot-server.txt"""
        target = os.path.abspath('hot-server.txt')
        if not os.path.exists(target):
            with open(target, 'w', encoding='utf-8') as f:
                f.write('# 服务端热词文件，每行一个\n')
        if sys.platform == 'win32':
            os.startfile(target)

    def _open_server_log(self):
        """打开服务端日志文件"""
        target = os.path.abspath(os.path.join('logs', 'server_latest.log'))
        if os.path.exists(target) and sys.platform == 'win32':
            os.startfile(target)

    def _request_exit(self, icon=None, item=None):
        """托盘图标引用的退出回调"""
        logger.info("托盘退出: 用户点击退出菜单，准备清理资源并退出")
        self.app.stop()
