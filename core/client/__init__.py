# coding: utf-8
"""
客户端模块

提供 CapsWriter 客户端的所有功能模块。

模块架构：
- state: 客户端状态管理
- connection/: WebSocket 连接管理
- audio/: 音频相关（录制、流、文件管理）
- shortcut/: 快捷键处理（原 input/）
- output/: 结果处理和输出（原 processing/）
- udp/: UDP 控制
- transcribe/: 文件转录
- diary/: 日记写入
- ui/: 用户界面
"""

import sys as _sys
from multiprocessing import freeze_support as _freeze_support

# 在 PyInstaller 打包环境下，当作为一体化进程拉起服务端识别子进程（multiprocessing spawn）时，
# 必须在主模块顶层第一时间调用 freeze_support()，让子进程直接进入 RecognizerWorker，
# 避免子进程重复初始化客户端和托盘。
if any(arg.startswith('--multiprocessing-fork') for arg in _sys.argv[1:]):
    _freeze_support()

from config_client import ClientConfig as Config
from core.logger import get_logger, setup_logger

# 直接在这里配置主日志级别
setup_logger('client', level=Config.log_level)
logger = get_logger('client')

# 门面类
from core.client.app import CapsWriterClient

__all__ = [
    'CapsWriterClient',
]

