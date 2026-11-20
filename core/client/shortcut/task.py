# coding: utf-8
"""
快捷键任务模块

管理单个快捷键的录音任务状态
"""

from __future__ import annotations
import asyncio
import time
from threading import Event, Lock, Timer
from typing import TYPE_CHECKING, Optional

from . import logger
from core.tools.my_status import Status
 
if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.audio.recorder import AudioRecorder
    from core.client.app import CapsWriterClient



class ShortcutTask:
    """
    单个快捷键的录音任务

    跟踪每个快捷键独立的录音状态，防止互相干扰。
    """

    def __init__(self, app: CapsWriterClient, shortcut: Shortcut, recorder_class=None):
        """
        初始化快捷键任务

        Args:
            app: 客户端 App 实例
            shortcut: 快捷键配置
            recorder_class: AudioRecorder 类（可选，用于延迟导入）
        """
        self.app = app
        self.shortcut = shortcut
        self._recorder_class = recorder_class

        # 任务状态
        self.task: Optional[asyncio.Future] = None
        self.recording_start_time: float = 0.0
        self.is_recording: bool = False

        # hold_mode 状态跟踪
        self.pressed: bool = False
        self.released: bool = True
        self.event: Event = Event()
        self.threshold: float = 0.8
        self.pending_release: bool = False
        self.overlay_shown: bool = False
        self._overlay_lock: Lock = Lock()
        self._overlay_timer: Optional[Timer] = None

        # 线程池（用于 countdown）
        self.pool = None

        # 录音状态动画
        self._status = Status('开始录音', spinner='point')

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _get_recorder(self) -> AudioRecorder:
        """获取 AudioRecorder 实例"""
        if self._recorder_class is None:
            from core.client.audio.recorder import AudioRecorder
            self._recorder_class = AudioRecorder
        return self._recorder_class(self.app)

    def _cancel_overlay_timer(self) -> None:
        """取消挂起的悬浮窗延迟显示定时器"""
        with self._overlay_lock:
            if self._overlay_timer is not None:
                self._overlay_timer.cancel()
                self._overlay_timer = None

    def ensure_overlay_shown(self, start_time: Optional[float] = None) -> None:
        """在确认进入长按语音识别后显示录音悬浮窗"""
        with self._overlay_lock:
            target_time = self.recording_start_time if start_time is None else start_time
            if (
                not self.is_recording
                or self.pending_release
                or self.recording_start_time != target_time
                or self.overlay_shown
            ):
                return
            if self._overlay_timer is not None:
                self._overlay_timer.cancel()
                self._overlay_timer = None
            self.overlay_shown = True

        self.app.overlay.recording(target_time)

    def launch(self) -> None:
        """启动录音任务"""
        logger.info(f"[{self.shortcut.key}] 触发：开始录音")

        # 记录开始时间并复位浮窗状态
        with self._overlay_lock:
            self.recording_start_time = time.time()
            self.is_recording = True
            self.pending_release = False
            self.overlay_shown = False
            if self._overlay_timer is not None:
                self._overlay_timer.cancel()
                self._overlay_timer = None

        # 将开始标志放入队列
        asyncio.run_coroutine_threadsafe(
            self.state.queue_in.put({'type': 'begin', 'time': self.recording_start_time, 'data': None}),
            self.app.loop
        )

        # 更新录音状态（先在后台缓存音频，按住达到阈值后再弹出悬浮窗）
        self.state.start_recording(self.recording_start_time)
        if self.shortcut.hold_mode and self.threshold > 0:
            start_time = self.recording_start_time
            with self._overlay_lock:
                if self.is_recording and not self.pending_release and not self.overlay_shown:
                    timer = Timer(self.threshold, self.ensure_overlay_shown, args=(start_time,))
                    timer.daemon = True
                    self._overlay_timer = timer
                    timer.start()
        else:
            self.ensure_overlay_shown(self.recording_start_time)

        # 打印动画：正在录音
        self._status.start()

        # 启动识别任务
        recorder = self._get_recorder()
        self.task = asyncio.run_coroutine_threadsafe(
            recorder.record_and_send(),
            self.app.loop,
        )

    def cancel(self) -> None:
        """取消录音任务（时间过短）"""
        logger.debug(f"[{self.shortcut.key}] 取消录音任务（时间过短）")

        with self._overlay_lock:
            self.is_recording = False
            self.pending_release = False
            if self._overlay_timer is not None:
                self._overlay_timer.cancel()
                self._overlay_timer = None
            was_shown = self.overlay_shown
            self.overlay_shown = False

        self.state.stop_recording()
        self._status.stop()
        if was_shown:
            self.app.overlay.cancel(self.recording_start_time)

        if self.task is not None:
            self.task.cancel()
            self.task = None

    def finish(self) -> None:
        """完成录音任务"""
        logger.info(f"[{self.shortcut.key}] 释放：完成录音")

        with self._overlay_lock:
            self.is_recording = False
            self.pending_release = False
            if self._overlay_timer is not None:
                self._overlay_timer.cancel()
                self._overlay_timer = None
            need_show = not self.overlay_shown
            if need_show:
                self.overlay_shown = True

        self.state.stop_recording()
        self._status.stop()
        if need_show:
            self.app.overlay.recording(self.recording_start_time)
        self.app.overlay.processing(self.recording_start_time)

        asyncio.run_coroutine_threadsafe(
            self.state.queue_in.put({
                'type': 'finish',
                'time': time.time(),
                'data': None
            }),
            self.app.loop
        )

        # 执行 restore（可恢复按键 + 非阻塞模式）
        # 阻塞模式下按键不会发送到系统，状态不会改变，不需要恢复
        if self.shortcut.is_toggle_key() and not self.shortcut.suppress:
            self._restore_key()

    def _restore_key(self) -> None:
        """恢复按键状态（防自捕获逻辑由 ShortcutManager 处理）"""
        # 通知管理器执行 restore
        # 防自捕获：管理器会设置 flag 再发送按键
        manager = self._manager_ref()
        if manager:
            logger.debug(f"[{self.shortcut.key}] 自动恢复按键状态 (suppress={self.shortcut.suppress})")
            manager.schedule_restore(self.shortcut.key)
        else:
            logger.warning(f"[{self.shortcut.key}] manager 引用丢失，无法 restore")
