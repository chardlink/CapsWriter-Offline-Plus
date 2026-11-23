# coding: utf-8
"""
托盘图标模块

提供最小化到系统托盘的功能。
仅在 Windows 平台有效。

功能：
- 禁用控制台窗口的关闭按钮（防止误关）
- 最小化时自动隐藏到托盘
- 双击托盘图标显示/隐藏窗口
- 托盘菜单退出程序

注意：pystray 在 Linux 无 GUI 环境下无法导入，因此采用延迟导入。
"""

import os
import sys
import time
import threading
import platform
import subprocess
from typing import Optional
from . import logger, set_ui_logger

# 退出回调函数（由主程序传入）
_exit_callback = None

# 悬浮通知回调函数（由主程序传入，优先使用底部美化胶囊悬浮窗提示）
_notify_callback = None

# 是否可用（在 enable_min_to_tray 时检测）
_tray_available = None

def _set_exit_callback(callback):
    """设置退出回调函数"""
    global _exit_callback
    _exit_callback = callback

def _get_exit_callback():
    return _exit_callback

def set_notify_callback(callback):
    """设置系统通知回调函数: callback(message: str, theme: str = 'done', duration: float = 2.4)"""
    global _notify_callback
    _notify_callback = callback

def notify_user(message: str, theme: str = 'done', duration: float = 2.4):
    """优先使用底部胶囊悬浮窗显示通知，若未就绪则回退到美化后的 Toast"""
    if _notify_callback is not None:
        try:
            _notify_callback(message, theme, duration)
            return
        except Exception:
            pass
    try:
        from .toast import toast
        bg = "#162032" if theme != 'error' else "#5b1924"
        toast(message, duration=int(duration * 1000), bg=bg)
    except Exception:
        pass


def _check_tray_available() -> bool:
    """
    检查托盘功能是否可用
    
    Returns:
        bool: 是否可用
    """
    global _tray_available
    
    if _tray_available is not None:
        return _tray_available
    
    # 非 Windows 系统不支持
    if platform.system() != 'Windows':
        _tray_available = False
        return False
    
    # 尝试导入 pystray
    try:
        import pystray
        from PIL import Image
        _tray_available = True
    except ImportError as e:
        logger.warning(f"托盘功能不可用: {e}")
        _tray_available = False
    except Exception as e:
        logger.warning(f"托盘功能检测失败: {e}")
        _tray_available = False
    
    return _tray_available


# Windows API（延迟初始化）
_win_api_initialized = False
user32 = None
kernel32 = None

SW_HIDE = 0
SW_RESTORE = 9
SW_SHOW = 5
SC_CLOSE = 0xF060
MF_BYCOMMAND = 0x00000000
GA_ROOT = 3


def _init_win_api():
    """初始化 Windows API"""
    global _win_api_initialized, user32, kernel32
    
    if _win_api_initialized:
        return
    
    if platform.system() != 'Windows':
        return
    
    try:
        import ctypes
        user32 = ctypes.windll.user32
        kernel32 = ctypes.windll.kernel32
        _win_api_initialized = True
    except Exception as e:
        logger.warning(f"Windows API 初始化失败: {e}")


# 全局变量
_tray_instance: Optional['_TraySystem'] = None
_lock = threading.Lock()


def _get_console_hwnd():
    _init_win_api()
    hwnd = kernel32.GetConsoleWindow()
    if hwnd:
        # 在 Windows Terminal 中，GetConsoleWindow 返回的是内部窗口句柄。
        # 为了能让“退出按钮不可用”以及“从任务栏消失”生效，我们需要操作最外层的顶层窗口。
        # 对于普通 CMD，GetAncestor(hwnd, GA_ROOT) 依然返回 hwnd 本身。
        root_hwnd = user32.GetAncestor(hwnd, GA_ROOT)
        if root_hwnd:
            return root_hwnd
    return hwnd


def _disable_close_button(hwnd: int) -> None:
    """禁用窗口的关闭按钮"""
    if user32 is None:
        return
    h_menu = user32.GetSystemMenu(hwnd, False)
    if h_menu:
        user32.DeleteMenu(h_menu, SC_CLOSE, MF_BYCOMMAND)


def _enable_close_button(hwnd: int) -> None:
    """恢复窗口的关闭按钮"""
    if user32 is None:
        return
    user32.GetSystemMenu(hwnd, True)


def _is_window_minimized(hwnd: int) -> bool:
    """检查窗口是否最小化"""
    if user32 is None:
        return False
    return user32.IsIconic(hwnd) != 0


def _is_window_visible(hwnd: int) -> bool:
    """检查窗口是否可见"""
    if user32 is None:
        return False
    return user32.IsWindowVisible(hwnd) != 0


def _create_icon(icon_path: Optional[str] = None):
    """
    创建托盘图标
    
    优先从指定路径加载图标文件，如果不存在则动态生成。
    
    Args:
        icon_path: 图标文件路径
        
    Returns:
        PIL Image 对象
    """
    from PIL import Image, ImageDraw
    
    # 如果图标文件存在，直接加载
    if icon_path and os.path.exists(icon_path):
        try:
            image = Image.open(icon_path)
            if image.mode != 'RGBA':
                image = image.convert('RGBA')
            return image.resize((64, 64), Image.Resampling.LANCZOS)
        except Exception:
            pass  # 加载失败则使用动态生成

    # 动态生成图标
    size = 64
    scale = 4
    real_size = size * scale

    image = Image.new('RGBA', (real_size, real_size), (0, 0, 0, 0))
    dc = ImageDraw.Draw(image)

    blue = (55, 118, 171)
    yellow = (255, 211, 67)
    white = (255, 255, 255)

    # 蓝色圆角背景
    m = 2 * scale
    dc.rounded_rectangle(
        [m, m, real_size - m, real_size - m],
        radius=real_size // 4,
        fill=blue
    )

    # 黄色圆圈
    center = real_size // 2
    r = real_size // 3.5
    dc.ellipse([center - r, center - r, center + r, center + r], fill=yellow)

    # 白色圆点
    r2 = r // 2
    dc.ellipse([center - r2, center - r2, center + r2, center + r2], fill=white)

    return image.resize((size, size), Image.Resampling.LANCZOS)


# ============================================================
# 开机自启动管理 (Windows 注册表 HKCU\...\Run)
# ============================================================

_AUTOSTART_REG_PATH = r"Software\Microsoft\Windows\CurrentVersion\Run"
_AUTOSTART_KEY_NAME = "CapsWriter-Offline"


def _get_autostart_command() -> str:
    """获取开机自启应执行的命令字符串"""
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for exe_name in ("CapsWriter.exe", "start_client.exe"):
        candidate = os.path.join(base_dir, exe_name)
        if os.path.exists(candidate):
            return f'"{candidate}"'
    if getattr(sys, 'frozen', False):
        return f'"{sys.executable}"'
    return f'"{sys.executable}" "{os.path.abspath(sys.argv[0])}"'


def is_autostart_enabled() -> bool:
    """检查当前是否已启用开机自启动"""
    if platform.system() != 'Windows':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_READ) as key:
            val, _ = winreg.QueryValueEx(key, _AUTOSTART_KEY_NAME)
            return bool(val)
    except FileNotFoundError:
        return False
    except Exception as e:
        logger.debug(f"读取开机自启注册表项失败: {e}")
        return False


def set_autostart(enabled: bool) -> bool:
    """开启或关闭开机自启动"""
    if platform.system() != 'Windows':
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _AUTOSTART_REG_PATH, 0, winreg.KEY_SET_VALUE) as key:
            if enabled:
                cmd = _get_autostart_command()
                winreg.SetValueEx(key, _AUTOSTART_KEY_NAME, 0, winreg.REG_SZ, cmd)
                logger.info(f"已开启开机自启动: {cmd}")
            else:
                try:
                    winreg.DeleteValue(key, _AUTOSTART_KEY_NAME)
                    logger.info("已关闭开机自启动")
                except FileNotFoundError:
                    pass
        return True
    except Exception as e:
        logger.error(f"设置开机自启动失败: {e}")
        return False


def _build_menu_items(options: list):
    """递归构建 pystray 菜单项列表（支持二级子菜单与复选框）"""
    import pystray
    from pystray import Menu, MenuItem as item

    built = []
    for opt in options:
        if opt == '---' or opt is Menu.SEPARATOR:
            built.append(Menu.SEPARATOR)
            continue
        opt_name, opt_val = opt[0], opt[1]
        kwargs = opt[2] if len(opt) > 2 and isinstance(opt[2], dict) else {}
        if isinstance(opt_val, (list, tuple)):
            sub_items = _build_menu_items(opt_val)
            built.append(item(opt_name, Menu(*sub_items)))
        else:
            built.append(item(opt_name, opt_val, **kwargs))
    return built


class _TraySystem:
    """托盘系统内部类"""

    def __init__(
        self,
        name: Optional[str] = None,
        icon_path: Optional[str] = None,
        more_options: list = None,
        panel_config: Optional[dict] = None,
    ):
        # 延迟导入 pystray
        import pystray
        from pystray import MenuItem as item

        self.hwnd = _get_console_hwnd()
        self.should_exit = False
        self.title = name if name else (os.path.basename(sys.argv[0]) or "Console App")
        self.panel = None

        # 优先使用现代毛玻璃悬浮控制面板
        try:
            from .tray_panel import TrayPanel
            full_cfg = self._build_panel_config(panel_config)
            self.panel = TrayPanel(full_cfg)
        except Exception as e:
            logger.warning(f"初始化毛玻璃托盘面板失败，回退到原生菜单: {e}")
            self.panel = None

        if self.panel is not None:
            panel = self.panel

            class _PanelTrayIcon(pystray.Icon):
                def _on_notify(icon_self, wparam, lparam):
                    try:
                        from pystray._util import win32
                        if lparam == win32.WM_RBUTTONUP:
                            import ctypes
                            from ctypes import wintypes
                            pt = wintypes.POINT()
                            u32 = ctypes.windll.user32
                            ok = hasattr(u32, 'GetPhysicalCursorPos') and u32.GetPhysicalCursorPos(ctypes.byref(pt))
                            if not ok:
                                u32.GetCursorPos(ctypes.byref(pt))
                            u32.AllowSetForegroundWindow(0xFFFFFFFF)
                            panel.toggle_at(pt.x, pt.y)
                            return
                    except Exception as ex:
                        logger.debug(f"托盘右键触发悬浮面板失败: {ex}")
                    return super()._on_notify(wparam, lparam)

            self.icon = _PanelTrayIcon(
                "console_tray",
                _create_icon(icon_path),
                title=f"{self.title}",
                menu=(item('显示/隐藏', self.toggle_window, default=True),),
            )
            return

        # 回退方案：传统原生右键菜单
        menu_items = [
            item(f"{self.title}", lambda: None, enabled=False),
            item('👁️ 显示/隐藏', self.toggle_window, default=True),
        ]
        if more_options:
            menu_items.extend(_build_menu_items(more_options))
        menu_items.append(
            item(
                '🚀 开机自启动',
                self.on_toggle_autostart,
                checked=lambda _: is_autostart_enabled(),
            )
        )
        menu_items.append(item('🔄 全部重启', self.on_restart))
        menu_items.append(item('❌ 退出', self.on_exit))

        self.icon = pystray.Icon(
            "console_tray",
            _create_icon(icon_path),
            title=f"{self.title}",
            menu=tuple(menu_items)
        )

    def _build_panel_config(self, panel_config: Optional[dict]) -> dict:
        """构建完整控制面板配置；若调用方未提供，则回退为默认服务端面板配置。"""
        base = {
            'title': 'CapsWriter',
            'badge': None,
            'is_console_visible': lambda: bool(self.hwnd and _is_window_visible(self.hwnd)),
            'on_toggle_console': self.toggle_window,
            'is_autostart': is_autostart_enabled,
            'on_toggle_autostart': self.on_toggle_autostart,
            'on_restart_all': lambda: self.on_restart(self.icon, None),
            'on_exit': lambda: self.on_exit(self.icon, None),
        }
        if panel_config:
            base.update(panel_config)
            return base

        # 独立服务端默认配置
        def _srv_status():
            try:
                from config_server import ServerConfig
                return ('服务端运行中', f'模型: {ServerConfig.model_type} · 端口: {ServerConfig.port}', False)
            except Exception:
                return ('服务端运行中', '本地离线语音引擎', False)

        def _open_hot():
            target = os.path.abspath('hot-server.txt')
            if not os.path.exists(target):
                with open(target, 'w', encoding='utf-8') as f:
                    f.write('# 服务端热词文件，每行一个\n')
            if platform.system() == 'Windows':
                os.startfile(target)

        def _open_log():
            target = os.path.abspath(os.path.join('logs', 'server_latest.log'))
            if os.path.exists(target) and platform.system() == 'Windows':
                os.startfile(target)

        base['badge'] = '服务端'
        base['server'] = {
            'get_status': _srv_status,
            'actions': [
                ('srv_hotword', 'hotword', '服务端热词', 'hot-server.txt', _open_hot),
                ('srv_log', 'log', '查看服务端日志', 'server_latest.log', _open_log),
            ],
        }
        return base

    def on_toggle_autostart(self, icon=None, item=None) -> None:
        """切换开机自启动状态"""
        new_state = not is_autostart_enabled()
        ok = set_autostart(new_state)
        if ok:
            msg = "已开启开机自启动" if new_state else "已关闭开机自启动"
            notify_user(msg, theme='done', duration=2.4)
        else:
            notify_user("设置开机自启动失败", theme='error', duration=3.0)
        try:
            self.icon.update_menu()
        except Exception:
            pass

    def toggle_window(self) -> None:
        """切换窗口显示状态"""
        if not self.hwnd or user32 is None:
            return

        if _is_window_visible(self.hwnd):
            user32.ShowWindow(self.hwnd, SW_HIDE)
        else:
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            user32.SetForegroundWindow(self.hwnd)

    def monitor_loop(self) -> None:
        """监控线程：检测最小化操作"""
        while not self.should_exit:
            if self.hwnd and user32:
                # 窗口可见且最小化 -> 隐藏到托盘
                if _is_window_visible(self.hwnd) and _is_window_minimized(self.hwnd):
                    user32.ShowWindow(self.hwnd, SW_HIDE)
            time.sleep(0.2)

    def on_restart(self, icon, item) -> None:
        """托盘重启处理：先释放当前进程端口与子进程资源，再拉起独立新进程并立即退出"""
        logger.info("托盘重启: 用户点击重启菜单，准备重启程序")
        self.should_exit = True

        # 1. 先执行退出回调，确保端口 6016、音频流与识别子进程已释放
        exit_callback = _get_exit_callback()
        if exit_callback:
            try:
                exit_callback()
            except Exception as e:
                logger.error(f"重启时调用退出回调发生错误: {e}")

        # 2. 启动全新进程（使用独立控制台，避免继承已被暂停或正在销毁的旧控制台句柄）
        try:
            if getattr(sys, 'frozen', False):
                cmd = sys.argv
            else:
                cmd = [sys.executable] + sys.argv
            flags = getattr(subprocess, 'CREATE_NEW_CONSOLE', 0) if platform.system() == 'Windows' else 0
            subprocess.Popen(cmd, creationflags=flags, close_fds=True)
        except Exception as e:
            logger.error(f"重启拉起新进程失败: {e}")
            return

        # 3. 强制终止当前旧进程，避免后台残留线程占用资源
        os._exit(0)

    def on_exit(self, icon, item) -> None:
        """托盘退出处理"""
        exit_callback = _get_exit_callback()

        logger.info("托盘退出: 用户点击退出菜单，准备清理资源并退出")

        # 1. 设置退出标志，停止监控循环
        self.should_exit = True
        logger.debug("已设置托盘退出标志")

        # 2. 恢复窗口关闭按钮并显示窗口
        if self.hwnd and user32:
            _enable_close_button(self.hwnd)
            user32.ShowWindow(self.hwnd, SW_RESTORE)
            logger.debug("已恢复窗口显示")

        # 3. 调用退出回调函数，请求主程序退出
        if exit_callback:
            try:
                logger.debug("正在调用退出回调函数...")
                exit_callback()
                logger.info("退出回调函数已调用")
            except Exception as e:
                logger.error(f"调用退出回调函数时发生错误: {e}")



        # 5. 停止托盘控制面板与托盘图标
        if self.panel is not None:
            try:
                self.panel.stop()
            except Exception as e:
                logger.debug(f"停止托盘控制面板时发生错误: {e}")
        try:
            logger.debug("正在停止托盘图标线程...")
            self.icon.stop()
            logger.debug("托盘图标线程已停止")
        except Exception as e:
            logger.warning(f"停止托盘图标时发生错误: {e}")

    def start(self) -> None:
        """启动托盘系统"""
        # 托盘图标线程
        t_tray = threading.Thread(target=self.icon.run, daemon=False)
        t_tray.start()

        # 状态监控线程
        t_monitor = threading.Thread(target=self.monitor_loop, daemon=True)
        t_monitor.start()

        # 启动时隐藏窗口
        self.toggle_window()


def enable_min_to_tray(
    name: Optional[str] = None,
    icon_path: Optional[str] = None,
    exit_callback=None,
    more_options: list = None,
    panel_config: Optional[dict] = None,
) -> None:
    """
    启用最小化到托盘功能

    如果检测不到控制台窗口（如 .pyw 运行），则不执行任何操作。
    如果在 Linux 等无 GUI 环境下运行，也会跳过。

    Args:
        name: 托盘图标显示的名称，默认使用程序名称
        icon_path: 图标文件路径，默认动态生成
        exit_callback: 退出回调函数，当用户点击托盘退出菜单时调用
        more_options: 额外菜单项列表（作为回退原生菜单使用）
        panel_config: 现代毛玻璃悬浮控制面板配置字典
    """
    global _tray_instance

    # 设置退出回调函数
    if exit_callback is not None:
        _set_exit_callback(exit_callback)

    # 检查托盘功能是否可用
    if not _check_tray_available():
        logger.info("托盘功能不可用，跳过启用")
        return

    # DPI 感知设置
    try:
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    with _lock:
        if _tray_instance is not None:
            return  # 已启动

        if not _get_console_hwnd():
            return  # 没有控制台窗口

        _tray_instance = _TraySystem(name, icon_path, more_options, panel_config=panel_config)
        _tray_instance.start()



def stop_tray() -> None:
    """停止托盘图标"""
    global _tray_instance
    if _tray_instance:
        if getattr(_tray_instance, 'panel', None):
            try:
                _tray_instance.panel.stop()
            except Exception:
                pass
        if getattr(_tray_instance, 'icon', None):
            try:
                _tray_instance.icon.stop()
            except Exception:
                pass
    _tray_instance = None


if __name__ == "__main__":
    enable_min_to_tray()
    print("程序运行中... 你可以双击托盘图标隐藏我。")
    while True:
        time.sleep(1)
