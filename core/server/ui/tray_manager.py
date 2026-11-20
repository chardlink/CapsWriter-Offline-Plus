# coding: utf-8
from __future__ import annotations
import os
import sys
import threading
from typing import TYPE_CHECKING
from config_server import ServerConfig as Config
from ..state import console
from .. import logger # Server module logger
if TYPE_CHECKING:
    from ..app import CapsWriterServer


class TrayManager:
    """
    托盘管理器：负责系统托盘图标的初始化、菜单构建及回调处理。
    """
    def __init__(self, app: CapsWriterServer):
        self.app = app

    def start(self):
        """初始化系统托盘图标"""
        if not Config.enable_tray:
            return

        try:
            from . import enable_min_to_tray
        except ImportError as e:
            logger.warning(f"托盘模块导入失败，跳过托盘功能: {e}")
            return

        # 获取图标路径
        icon_path = os.path.join(self.app.base_dir, 'assets', 'icon.ico')

        def _get_status():
            busy = bool(getattr(self.app, '_restarting', False))
            title = '正在重启识别引擎…' if busy else '服务端运行中'
            return (title, f'模型: {Config.model_type} · 端口: {Config.port}', busy)

        def _open_hotword():
            target = os.path.abspath('hot-server.txt')
            if not os.path.exists(target):
                with open(target, 'w', encoding='utf-8') as f:
                    f.write('# 服务端热词文件，每行一个\n')
            if sys.platform == 'win32':
                os.startfile(target)

        def _open_log():
            target = os.path.abspath(os.path.join('logs', 'server_latest.log'))
            if os.path.exists(target) and sys.platform == 'win32':
                os.startfile(target)

        def _restart_engine():
            threading.Thread(target=self.app.restart_worker, daemon=True).start()

        panel_config = {
            'title': 'CapsWriter',
            'badge': '服务端',
            'on_exit': self._request_exit,
            'server': {
                'get_status': _get_status,
                'actions': [
                    ('srv_hotword', 'hotword', '服务端热词', 'hot-server.txt', _open_hotword),
                    ('srv_log', 'log', '查看服务端日志', 'server_latest.log', _open_log),
                    ('srv_restart', 'refresh', '重启识别引擎', '重载模型', _restart_engine),
                ],
            },
        }

        # 启用托盘
        enable_min_to_tray(
            'CapsWriter Server',
            icon_path,
            exit_callback=self._request_exit,
            panel_config=panel_config,
        )
        logger.info("托盘图标已启用")

    def _request_exit(self, icon=None, item=None):
        """托盘图标引用的退出回调"""
        logger.info("托盘退出: 用户点击退出菜单，准备清理资源并退出")
        self.app.stop()

    def stop(self):
        """停止托盘图标"""
        if not Config.enable_tray:
            return
            
        try:
            from core.ui.tray import stop_tray
            stop_tray()
            logger.info("TrayManager: 托盘图标已卸载")
        except Exception as e:
            logger.debug(f"TrayManager: 卸载托盘时发生错误: {e}")
