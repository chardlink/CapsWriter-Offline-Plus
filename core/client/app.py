# coding: utf-8
"""
CapsWriter Offline 客户端主程序门面类 (Facade)

采用外观模式统一管理音频流 (AudioStreamManager)、
识别结果处理 (ResultProcessor) 和快捷键管理 (ShortcutManager)。
"""

import os
import sys
import socket
import asyncio
import threading
from pathlib import Path
from multiprocessing import freeze_support

from .state import ClientState
from . import logger
from config_client import ClientConfig as Config, __version__
from core.tools.signal_handler import register_signal
from .state import console
from .connection import WebSocketManager
from typing import TYPE_CHECKING, Optional
from .manager import (
    TrayManager,
    MicRunner, FileRunner
)
from .audio.stream import AudioStreamManager
from .shortcut.shortcut_manager import ShortcutManager
from .shortcut.shortcut_config import Shortcut

from .udp.udp_control import UDPController

from .hotword.manager import HotwordManager
from .llm.llm_handler import LLMHandler
from .output.text_output import TextOutput
from .diary.diary_writer import DiaryWriter
from core.tools.empty_working_set import empty_current_working_set
from platform import system



def _disable_console_quick_edit() -> None:
    """禁用 Windows 控制台快速编辑模式，防止用户误点黑窗口进入选择暂停态导致主进程/日志打印卡死"""
    if os.name != 'nt':
        return
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL('kernel32')
        h_in = kernel32.GetStdHandle(-10)  # STD_INPUT_HANDLE
        if not h_in or h_in == -1:
            return
        mode = wintypes.DWORD()
        if kernel32.GetConsoleMode(h_in, ctypes.byref(mode)):
            # 清除 ENABLE_QUICK_EDIT_MODE(0x40) 和 ENABLE_INSERT_MODE(0x20)，设置 ENABLE_EXTENDED_FLAGS(0x80)
            new_mode = (mode.value & ~0x0060) | 0x0080
            kernel32.SetConsoleMode(h_in, new_mode)
    except Exception:
        pass


class CapsWriterClient:
    """
    CapsWriter 客户端门面类
    
    管理的外部接口简洁：start()。
    """
    def __init__(self):
        _disable_console_quick_edit()
        # 确保正确的工作目录
        self.base_dir = Path(__file__).parents[2]
        os.chdir(self.base_dir)
            
        # 初始化事件循环
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
            
        # 初始化状态容器
        self.state = ClientState(app=self)

        # 初始化热词管理器
        self.hotword = HotwordManager(
            hotword_files=None,
            threshold=Config.hot_thresh,
            similar_threshold=Config.hot_similar
        )

        # 4. 初始化 LLM 润色系统
        self.llm = LLMHandler(app=self)
        
        self.output = TextOutput()
        self.diary = DiaryWriter(base_path=self.base_dir)
        self.overlay = None

        # 初始化各管理器
        self.ws = WebSocketManager(self)
        self.tray = TrayManager(self)

        # 实例化硬件资源管理组件
        self.stream = AudioStreamManager(self)
        self.shortcut = ShortcutManager(self, [Shortcut(**sc) for sc in Config.shortcuts])
        self.udp = UDPController(self.shortcut)
        self.server = None
        self._server_thread: Optional[threading.Thread] = None

        # 内存清理
        empty_current_working_set()

        # 启动时自动清理超过保留天数的录音与日记
        try:
            self.diary.clean_expired()
        except Exception as e:
            logger.debug(f"启动清理过期录音失败: {e}")

    @staticmethod
    def _is_server_port_open() -> bool:
        """检查本地或配置的服务端端口是否已有服务在监听"""
        host = '127.0.0.1' if Config.addr in ('0.0.0.0', 'localhost', '127.0.0.1') else Config.addr
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.25)
            return s.connect_ex((host, int(Config.port))) == 0

    def _start_embedded_server_if_needed(self):
        """如果当前端口没有服务端运行，且连接地址是本机，则自动在后台线程启动服务端"""
        if Config.addr not in ('127.0.0.1', 'localhost', '0.0.0.0'):
            return
        if self._is_server_port_open():
            logger.info(f"检测到端口 {Config.port} 已有服务端运行，直接连接现有服务端")
            return
        try:
            from core.server.app import CapsWriterServer
            logger.info("未检测到运行中的服务端，正在一体化启动内置服务端...")
            self.server = CapsWriterServer(manage_tray=False, register_signals=False)
            self.tray.server = self.server
            self._server_thread = threading.Thread(
                target=self.server.start,
                daemon=True,
                name="EmbeddedServerThread",
            )
            self._server_thread.start()
        except Exception as e:
            logger.error(f"启动内置服务端失败: {e}", exc_info=True)

    def restart_client_components(self):
        """重新加载客户端组件（音频流、快捷键、热词、LLM 角色）"""
        logger.info("正在重新加载客户端组件...")
        try:
            self.stream.reopen()
        except Exception as e:
            logger.warning(f"重开音频流失败: {e}")
        try:
            self.shortcut.stop()
            self.shortcut = ShortcutManager(self, [Shortcut(**sc) for sc in Config.shortcuts])
            self.shortcut.start()
        except Exception as e:
            logger.warning(f"重启快捷键监听失败: {e}")
        try:
            self.hotword.load_all()
        except Exception as e:
            logger.warning(f"重新加载热词失败: {e}")
        try:
            self.llm.reload_roles()
        except Exception as e:
            logger.warning(f"重新加载 LLM 角色失败: {e}")
        logger.info("客户端组件重新加载完成")

    def exit_all(self):
        """一体化退出：同时清理客户端和内置服务端资源"""
        self.stop()

    def stop(self):
        """
        统一释放所有资源（清理顺序：硬件 -> 托盘 -> WebSocket -> State）
        """
        logger.info("正在执行 CapsWriterClient 资源释放...")

        # 1. 停止核心运行组件
        self.udp.stop()
        self.shortcut.stop()
        if self.overlay:
            self.overlay.stop()
        self.stream.stop()

        # 2. 托盘资源
        self.tray.stop()

        # 3. 关闭监控
        self.hotword.stop()
        self.llm.stop()

        # 4. 关闭 WebSocket 连接
        self.ws.close_sync()

        # 5. 重置 State
        try:
            self.state.reset()
        except Exception as e:
            logger.warning(f"重置状态时发生错误: {e}")

        # 5.5 如果内置了服务端，一并停止
        if self.server is not None:
            try:
                self.server.stop()
            except Exception as e:
                logger.warning(f"停止内置服务端时发生错误: {e}")

        # 6. 停止事件循环（最后一步，确保前面的异步操作已调度）
        try:
            self.loop.call_soon_threadsafe(self.loop.stop)
        except Exception:
            pass

        logger.info("资源释放完成")
        console.print('[green4]再见！')


    def start(self):
        """
        启动客户端 (唯一入口)
        
        自动根据命令行参数识别模式。内部管理异步循环。
        """
        freeze_support()

        # 注册退出函数
        register_signal(self.stop)

        # 若未启动服务端，则自动拉起内置服务端
        self._start_embedded_server_if_needed()

        files = [Path(f) for f in sys.argv[1:] if os.path.exists(f)]

        if files:
            # 文件转录模式
            runner = FileRunner(self, files)
        else:
            # 麦克风实时模式
            runner = MicRunner(self)
        
        try:
            self.loop.run_until_complete(runner.run())
        except RuntimeError:
            ...
