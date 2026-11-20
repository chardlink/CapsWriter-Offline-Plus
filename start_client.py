# coding: utf-8
"""CapsWriter Offline 一体化 / 客户端启动入口。"""

from multiprocessing import freeze_support
from core.client import CapsWriterClient


if __name__ == '__main__':
    freeze_support()
    CapsWriterClient().start()
