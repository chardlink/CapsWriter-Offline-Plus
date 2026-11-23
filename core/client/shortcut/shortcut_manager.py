# coding: utf-8
"""
快捷键管理器（重构版）

统一管理多个快捷键，处理键盘和鼠标事件，支持：
1. 多快捷键并发处理
2. 防止不同按键互相干扰
3. restore 功能的防自捕获逻辑
4. hold_mode 和 click_mode 支持
"""
from __future__ import annotations
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Dict, List, Optional

from pynput import keyboard, mouse

from . import logger
from core.client.shortcut.key_mapper import *
from core.client.shortcut.key_mapper import KeyMapper
from core.client.shortcut.emulator import ShortcutEmulator
from core.client.shortcut.event_handler import ShortcutEventHandler
from core.client.shortcut.task import ShortcutTask

if TYPE_CHECKING:
    from core.client.shortcut.shortcut_config import Shortcut
    from core.client.state import ClientState
    from core.client.app import CapsWriterClient



class ShortcutManager:
    """
    快捷键管理器

    统一管理多个快捷键，使用 pynput 监听键盘和鼠标事件。
    底层钩子中仅执行轻量判定与事件阻塞，将耗时任务派发至独立串行执行器。
    """

    # 鼠标侧键防微动抖动窗口（秒）与长按结束后的回弹冷却期（秒）
    MOUSE_BOUNCE_GRACE_S = 0.18
    MOUSE_POST_FINISH_COOLDOWN_S = 0.35

    def __init__(self, app: CapsWriterClient, shortcuts: List[Shortcut]):
        """
        初始化快捷键管理器

        Args:
            app: 客户端 App 实例
            shortcuts: 快捷键配置列表
        """
        self.app = app
        self.shortcuts = shortcuts

        # 监听器
        self.keyboard_listener: Optional[keyboard.Listener] = None
        self.mouse_listener: Optional[mouse.Listener] = None

        # 快捷键任务映射（key -> ShortcutTask）
        self.tasks: Dict[str, ShortcutTask] = {}

        # 线程池（用于补发按键、单机倒计时、恢复状态等）
        self._pool = ThreadPoolExecutor(max_workers=4)

        # 串行动作执行器（使按下/松开逻辑按顺序在后台运行，不阻塞系统钩子）
        self._action_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='ShortcutAction')

        # 鼠标侧键防抖与防回弹状态
        self._mouse_lock = threading.Lock()
        self._mouse_release_timers: Dict[str, threading.Timer] = {}
        self._mouse_cooldown_until: Dict[str, float] = {}

        # 按键模拟器
        self._emulator = ShortcutEmulator()

        # 按键恢复状态追踪
        self._restoring_keys = set()

        # 事件处理器
        self._event_handler = ShortcutEventHandler(self.tasks, self._pool, self._emulator)

        # 初始化快捷键任务
        self._init_tasks()

    @property
    def state(self) -> ClientState:
        """快捷访问状态单例"""
        return self.app.state

    def _init_tasks(self) -> None:
        """初始化所有快捷键任务"""
        from config_client import ClientConfig as Config

        for shortcut in self.shortcuts:
            if not shortcut.enabled:
                continue

            task = ShortcutTask(self.app, shortcut)
            task._manager_ref = lambda: self  # 弱引用，用于回调
            task.pool = self._pool
            task.threshold = shortcut.get_threshold(Config.threshold)
            self.tasks[shortcut.key] = task

    # ========== 监听器创建 ==========

    def _submit_action(self, fn, *args) -> None:
        """在串行动作执行器中提交任务，避免钩子与关闭流程发生竞态异常"""
        try:
            self._action_executor.submit(fn, *args)
        except RuntimeError:
            pass

    def create_keyboard_filter(self):
        """创建键盘事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 KEYDOWN 和 KEYUP 消息
            if msg not in KEYBOARD_MESSAGES:
                return True

            key_name = KeyMapper.vk_to_name(data.vkCode)

            # 防自捕获检查
            if self._check_emulating(key_name, msg):
                return True
            if self._check_restoring(key_name, msg):
                return True

            # 查找匹配的快捷键
            if key_name not in self.tasks:
                return True

            task = self.tasks[key_name]

            # 仅投递到串行执行器，避免阻塞 WH_KEYBOARD_LL 钩子
            if msg in KEY_DOWN_MESSAGES:
                self._submit_action(self._event_handler.handle_keydown, key_name, task)
            elif msg in KEY_UP_MESSAGES:
                self._submit_action(self._event_handler.handle_keyup, key_name, task)

            # 阻塞事件（suppress_event 会抛出 SuppressException，须置于投递之后）
            if task.shortcut.suppress and self.keyboard_listener:
                self.keyboard_listener.suppress_event()

            return True

        return win32_event_filter

    def create_mouse_filter(self):
        """创建鼠标事件过滤器"""
        def win32_event_filter(msg, data):
            # 只处理 XBUTTON 消息
            if msg not in MOUSE_MESSAGES:
                return True

            # 获取按键标识
            xbutton = (data.mouseData >> 16) & 0xFFFF
            button_name = 'x1' if xbutton == XBUTTON1 else 'x2'

            # 防自捕获检查
            if self._check_emulating(button_name, msg, is_mouse=True):
                return True

            # 查找匹配的快捷键
            if button_name not in self.tasks:
                return True

            task = self.tasks[button_name]

            # 仅在钩子中执行轻量防抖判断与派发，避免阻塞 WH_MOUSE_LL
            if msg == WM_XBUTTONDOWN:
                self._on_mouse_keydown(button_name, task)
            elif msg == WM_XBUTTONUP:
                self._on_mouse_keyup(button_name, task)

            # 阻塞事件（suppress_event 会抛出 SuppressException，须置于派发之后）
            if task.shortcut.suppress and self.mouse_listener:
                self.mouse_listener.suppress_event()

            return True

        return win32_event_filter

    def _cancel_mouse_release_timer_locked(self, button_name: str) -> bool:
        """在持锁状态下取消挂起的鼠标松开防抖定时器"""
        timer = self._mouse_release_timers.pop(button_name, None)
        if timer is not None:
            timer.cancel()
            return True
        return False

    def _on_mouse_keydown(self, button_name: str, task) -> None:
        """在钩子层快速处理鼠标按下：拦截松开后回弹、取消瞬断释放并异步启动"""
        now = time.time()
        with self._mouse_lock:
            cancelled_pending = self._cancel_mouse_release_timer_locked(button_name)
            in_cooldown = now < self._mouse_cooldown_until.get(button_name, 0.0)

        if in_cooldown:
            logger.debug(f"[{button_name}] 忽略长按结束后的微动回弹按下")
            return

        if cancelled_pending:
            logger.debug(f"[{button_name}] 检测到微动瞬断回弹，取消松开定时并保持录音")
            task.pending_release = False
            if (
                task.is_recording
                and not getattr(task, 'overlay_shown', False)
                and (now - task.recording_start_time) >= task.threshold
                and hasattr(task, 'ensure_overlay_shown')
            ):
                self._submit_action(task.ensure_overlay_shown, task.recording_start_time)

        task._in_bounce_recovery = bool(cancelled_pending)
        self._submit_action(self._event_handler.handle_keydown, button_name, task)

    def _on_mouse_keyup(self, button_name: str, task) -> None:
        """在钩子层快速处理鼠标松开：长按模式启用防抖延迟结算"""
        if not task.shortcut.hold_mode:
            self._submit_action(self._handle_mouse_click_mode_keyup, button_name, task)
            return

        task.pending_release = True
        up_time = time.time()
        with self._mouse_lock:
            self._cancel_mouse_release_timer_locked(button_name)
            timer_ref: List[Optional[threading.Timer]] = [None]
            timer = threading.Timer(
                self.MOUSE_BOUNCE_GRACE_S,
                self._schedule_mouse_keyup,
                args=(button_name, task, up_time, timer_ref),
            )
            timer.daemon = True
            timer_ref[0] = timer
            self._mouse_release_timers[button_name] = timer
            timer.start()

    def _schedule_mouse_keyup(
        self,
        button_name: str,
        task,
        up_time: float,
        timer_ref: List[Optional[threading.Timer]],
    ) -> None:
        """防抖窗口期满后，将鼠标松开结算投递到串行执行器"""
        current_timer = timer_ref[0]
        with self._mouse_lock:
            if self._mouse_release_timers.get(button_name) is not current_timer:
                return
            self._mouse_release_timers.pop(button_name, None)

        self._submit_action(self._finalize_mouse_keyup, button_name, task, up_time)

    def _handle_mouse_click_mode_keyup(self, button_name: str, task) -> None:
        """在串行执行器中处理单击模式的鼠标松开"""
        if task.pressed:
            task.pressed = False
            task.released = True
            task.event.set()

    def _finalize_mouse_keyup(self, button_name: str, task, up_time: float) -> None:
        """在防抖窗口结束后结算长按模式鼠标松开"""
        if not task.is_recording:
            return

        duration = max(0.0, up_time - task.recording_start_time)
        logger.debug(f"[{button_name}] 松开按键，持续时间: {duration:.3f}s")

        if duration < task.threshold:
            now = time.time()
            with self._mouse_lock:
                in_cooldown = now < self._mouse_cooldown_until.get(button_name, 0.0)
            task.cancel()
            if task.shortcut.suppress and not in_cooldown:
                logger.debug(f"[{button_name}] 安排异步补发鼠标按键")
                self._pool.submit(self._emulator.emulate_mouse_click, button_name)
        else:
            with self._mouse_lock:
                self._mouse_cooldown_until[button_name] = time.time() + self.MOUSE_POST_FINISH_COOLDOWN_S
            task.finish()

    # ========== 按键恢复管理 ==========

    def schedule_restore(self, key: str) -> None:
        """
        安排按键恢复（延迟执行，避免在事件处理中阻塞）

        Args:
            key: 要恢复的按键

        注意：标志清除只在按键释放事件中处理（_check_restoring），
        避免在线程中提前清除导致主线程收到重复消息。
        """
        from pynput import keyboard

        self._restoring_keys.add(key)

        def do_restore():
            import time
            time.sleep(0.05)  # 延迟 50ms
            if key == 'caps_lock':
                controller = keyboard.Controller()
                controller.press(keyboard.Key.caps_lock)
                controller.release(keyboard.Key.caps_lock)

        self._pool.submit(do_restore)

    def is_restoring(self, key: str) -> bool:
        """检查是否正在恢复指定按键"""
        return key in self._restoring_keys

    def clear_restoring_flag(self, key: str) -> None:
        """清除恢复标志"""
        self._restoring_keys.discard(key)

    # ========== 防自捕获检查 ==========

    def _check_emulating(self, key_name: str, msg: int, is_mouse: bool = False) -> bool:
        """检查是否正在模拟按键"""
        if not self._emulator.is_emulating(key_name):
            return False

        # 松开时清除标志
        if is_mouse:
            if msg == WM_XBUTTONUP:
                self._emulator.clear_emulating_flag(key_name)
        else:
            if msg in (WM_KEYUP, WM_SYSKEYUP):
                self._emulator.clear_emulating_flag(key_name)

        return True  # 放行

    def _check_restoring(self, key_name: str, msg: int) -> bool:
        """检查是否正在恢复按键"""
        if not self.is_restoring(key_name):
            return False

        if msg in (WM_KEYUP, WM_SYSKEYUP):
            self.clear_restoring_flag(key_name)

        return True  # 放行

    # ========== 公共接口 ==========

    def start(self) -> None:
        """启动所有监听器与后台自愈守护线程"""
        self._ensure_listeners(log_startup=True)

        # 打印所有启用的快捷键
        for shortcut in self.shortcuts:
            if shortcut.enabled:
                mode = "长按" if shortcut.hold_mode else "单击"
                toggle = "可恢复" if shortcut.is_toggle_key() else "普通键"
                logger.info(f"  [{shortcut.key}] {mode}模式, 阻塞:{shortcut.suppress}, {toggle}")

        # 启动长时运行自愈守护线程（处理休眠唤醒、钩子掉线与超时僵尸录音）
        if not hasattr(self, '_watchdog_stop') or self._watchdog_stop is None:
            self._watchdog_stop = threading.Event()
        else:
            self._watchdog_stop.clear()
        if not getattr(self, '_watchdog_thread', None) or not self._watchdog_thread.is_alive():
            self._watchdog_thread = threading.Thread(
                target=self._watchdog_loop,
                daemon=True,
                name="ShortcutWatchdog",
            )
            self._watchdog_thread.start()

    def _ensure_listeners(self, log_startup: bool = False, force_recreate: bool = False) -> None:
        """确保键盘与鼠标底层钩子监听器处于活跃状态"""
        has_keyboard = any(s.type == 'keyboard' for s in self.shortcuts if s.enabled)
        has_mouse = any(s.type == 'mouse' for s in self.shortcuts if s.enabled)

        if has_keyboard:
            if force_recreate and self.keyboard_listener:
                try:
                    self.keyboard_listener.stop()
                except Exception:
                    pass
                self.keyboard_listener = None

            if self.keyboard_listener and self.keyboard_listener.is_alive():
                if log_startup:
                    logger.debug("键盘监听器已在运行，跳过启动")
            else:
                self.keyboard_listener = keyboard.Listener(
                    win32_event_filter=self.create_keyboard_filter()
                )
                self.keyboard_listener.start()
                if log_startup:
                    logger.info("键盘监听器已启动")
                else:
                    logger.info("键盘监听器已自动重挂恢复")

        if has_mouse:
            if force_recreate and self.mouse_listener:
                try:
                    self.mouse_listener.stop()
                except Exception:
                    pass
                self.mouse_listener = None

            if self.mouse_listener and self.mouse_listener.is_alive():
                if log_startup:
                    logger.debug("鼠标监听器已在运行，跳过启动")
            else:
                self.mouse_listener = mouse.Listener(
                    win32_event_filter=self.create_mouse_filter()
                )
                self.mouse_listener.start()
                if log_startup:
                    logger.info("鼠标监听器已启动")
                else:
                    logger.info("鼠标监听器已自动重挂恢复")

    def _watchdog_loop(self) -> None:
        """后台守护循环：检测系统休眠唤醒、钩子线程意外失效与超长卡死录音"""
        last_mono = time.monotonic()
        last_refresh = last_mono
        while not self._watchdog_stop.wait(timeout=2.0):
            try:
                mono_now = time.monotonic()
                dt = mono_now - last_mono
                last_mono = mono_now
                now = time.time()

                # 1. 兜底清理超过 300 秒因松开事件丢失而悬空的录音任务
                for key_name, task in list(self.tasks.items()):
                    if task.is_recording and (now - task.recording_start_time) > 300.0:
                        logger.warning(f"[{key_name}] 录音持续超过 300s，疑似松开事件丢失，自动取消复位")
                        self._submit_action(task.cancel)

                any_recording = any(t.is_recording for t in self.tasks.values())
                has_kbd = any(s.type == 'keyboard' and s.enabled for s in self.shortcuts)
                has_mouse = any(s.type == 'mouse' and s.enabled for s in self.shortcuts)
                kbd_dead = has_kbd and (self.keyboard_listener is None or not self.keyboard_listener.is_alive())
                mouse_dead = has_mouse and (self.mouse_listener is None or not self.mouse_listener.is_alive())

                # 2. 任一监听线程退出时立即重挂
                if kbd_dead or mouse_dead:
                    logger.warning("检测到快捷键监听线程意外退出，正在自动恢复...")
                    self._ensure_listeners(log_startup=False, force_recreate=False)
                    last_refresh = mono_now
                    continue

                # 3. 系统从睡眠/现代待机唤醒（时间跳变 > 12s）或空闲满 30 分钟时，在非录音状态下静默刷新底层钩子
                woke_from_sleep = dt > 12.0
                periodic_idle = (mono_now - last_refresh) > 1800.0
                if not any_recording and (woke_from_sleep or periodic_idle):
                    if woke_from_sleep:
                        logger.info(f"检测到系统休眠/待机唤醒 (间隔 {dt:.1f}s)，正在刷新快捷键钩子与音频流...")
                    else:
                        logger.debug("执行定期空闲快捷键底层钩子刷新")
                    self._ensure_listeners(log_startup=False, force_recreate=True)
                    last_refresh = mono_now
                    if woke_from_sleep:
                        stream_mgr = getattr(self.app, 'stream', None)
                        if stream_mgr and hasattr(stream_mgr, 'reopen'):
                            try:
                                stream_mgr.reopen()
                            except Exception as e:
                                logger.debug(f"唤醒后重开音频流失败: {e}")
            except Exception as e:
                logger.debug(f"快捷键守护线程检查异常: {e}")

    def stop(self) -> None:
        """停止所有监听器和清理资源"""
        if hasattr(self, '_watchdog_stop') and self._watchdog_stop is not None:
            self._watchdog_stop.set()

        if self.keyboard_listener:
            try:
                self.keyboard_listener.stop()
                logger.debug("键盘监听器已停止")
            except Exception:
                pass
            finally:
                self.keyboard_listener = None
                
        if self.mouse_listener:
            try:
                self.mouse_listener.stop()
                logger.debug("鼠标监听器已停止")
            except Exception:
                pass
            finally:
                self.mouse_listener = None

        # 取消所有挂起的鼠标防抖定时器
        with self._mouse_lock:
            for timer in self._mouse_release_timers.values():
                timer.cancel()
            self._mouse_release_timers.clear()

        # 取消所有任务
        for task in self.tasks.values():
            if task.is_recording:
                task.cancel()

        # 关闭线程池与串行动作执行器
        self._action_executor.shutdown(wait=False)
        self._pool.shutdown(wait=False)
        logger.debug("快捷键管理器线程池已关闭")
