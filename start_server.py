# coding: utf-8
"""CapsWriter Offline 独立服务端启动入口。"""

from multiprocessing import freeze_support
from core.server.app import CapsWriterServer


if __name__ == '__main__':
    freeze_support()
    CapsWriterServer().start()
