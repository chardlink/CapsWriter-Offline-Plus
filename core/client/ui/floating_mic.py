# coding: utf-8
"""
桌面悬浮语音识别按钮 (FloatingMicButton)

功能特性：
1. 视觉风格与 TrayPanel / RecordingOverlay 高度统一：
   - 待机态：深空毛玻璃底座 + 青紫极光渐变 (Cyan -> Violet) 麦克风核心，悬浮微光。
   - 录音态：玫瑰红脉冲呼吸光环 (Rose Red) + 圆角正方形停止图标 (⏹)，直观提示“再次点击即可结束识别”。
   - 右键菜单态：在悬浮球右侧/左侧展开一体式毛玻璃胶囊菜单 [× 隐藏悬浮按钮]，点击即可隐藏并自动同步取消设置页勾选。
2. 零焦点抢占 (WS_EX_NOACTIVATE + WM_MOUSEACTIVATE -> MA_NOACTIVATE)：
   - 点击悬浮球开始/停止录音时，绝不抢占用户当前活动窗口（浏览器、Word、微信、IDE 等）的输入焦点，识别完成后直接粘贴到原光标处。
3. 自由拖拽 + 智能贴边防出界：
   - 按住左键拖动 (>5px) 可在屏幕任意位置顺滑移动，松开不误触录音；轻点左键立即切换“开始录音 / 停止识别”。
"""

import ctypes
from ctypes import wintypes
import math
import threading
import time
from typing import Callable, Optional, Tuple

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from core.client.shortcut.shortcut_config import Shortcut
from core.client.shortcut.task import ShortcutTask
from core.client import logger


# ── Win32 常量 ───────────────────────────────────────────────────
WS_POPUP = 0x80000000
WS_EX_LAYERED = 0x00080000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000

WM_DESTROY = 0x0002
WM_MOUSEACTIVATE = 0x0021
WM_SETCURSOR = 0x0020
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_RBUTTONDOWN = 0x0204
WM_RBUTTONUP = 0x0205
WM_MOUSELEAVE = 0x02A3
WM_TIMER = 0x0113
WM_APP_SHOW = 0x8001
WM_APP_HIDE = 0x8002
WM_APP_CLOSE = 0x8003
WM_APP_REDRAW = 0x8004

MA_NOACTIVATE = 3
MA_NOACTIVATEANDEAT = 4

SW_HIDE = 0
SW_SHOWNOACTIVATE = 4
HWND_TOPMOST = -1
SWP_NOMOVE = 0x0002
SWP_NOSIZE = 0x0001
SWP_NOACTIVATE = 0x0010
SWP_SHOWWINDOW = 0x0040

ULW_ALPHA = 0x00000002
AC_SRC_OVER = 0x00
AC_SRC_ALPHA = 0x01
DIB_RGB_COLORS = 0
BI_RGB = 0

IDC_HAND = 32649
IDC_SIZEALL = 32646
TME_LEAVE = 0x00000002

TIMER_PULSE = 1
MONITOR_DEFAULTTONEAREST = 2

# ── 主题色板（与 TrayPanel / RecordingOverlay 保持一致）──────────
BG_GLASS = (17, 21, 34)
ACCENT_C1 = (56, 189, 248)     # 极光青 #38BDF8
ACCENT_C2 = (139, 92, 246)     # 霓虹紫 #8B5CF6
ROSE_C1 = (251, 113, 133)      # 珊瑚粉 #FB7185
ROSE_C2 = (225, 29, 72)        # 绯红 #E11D48
TEXT_PRIMARY = (248, 250, 252)
TEXT_SECONDARY = (203, 213, 225)


# ── Win32 结构体 ─────────────────────────────────────────────────
class POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class SIZE(ctypes.Structure):
    _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ("BlendOp", ctypes.c_byte),
        ("BlendFlags", ctypes.c_byte),
        ("SourceConstantAlpha", ctypes.c_byte),
        ("AlphaFormat", ctypes.c_byte),
    ]


class BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", ctypes.c_long),
        ("biHeight", ctypes.c_long),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", ctypes.c_long),
        ("biYPelsPerMeter", ctypes.c_long),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]


class TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("hwndTrack", wintypes.HWND),
        ("dwHoverTime", wintypes.DWORD),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", wintypes.DWORD),
    ]


WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.UINT),
        ("style", wintypes.UINT),
        ("lpfnWndProc", WNDPROC),
        ("cbClsExtra", ctypes.c_int),
        ("cbWndExtra", ctypes.c_int),
        ("hInstance", wintypes.HINSTANCE),
        ("hIcon", wintypes.HANDLE),
        ("hCursor", wintypes.HANDLE),
        ("hbrBackground", wintypes.HANDLE),
        ("lpszMenuName", wintypes.LPCWSTR),
        ("lpszClassName", wintypes.LPCWSTR),
        ("hIconSm", wintypes.HANDLE),
    ]


# 独立加载 WinDLL 实例，避免与其他 UI 模块的 ctypes argtypes 冲突
user32 = ctypes.WinDLL("user32", use_last_error=True)
gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.RegisterClassExW.argtypes = [ctypes.POINTER(WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, wintypes.HDC, ctypes.POINTER(POINT), ctypes.POINTER(SIZE),
    wintypes.HDC, ctypes.POINTER(POINT), wintypes.COLORREF,
    ctypes.POINTER(BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(TRACKMOUSEEVENT)]
user32.TrackMouseEvent.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, wintypes.LPVOID]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCapture.argtypes = [wintypes.HWND]
user32.SetCapture.restype = wintypes.HWND
user32.ReleaseCapture.argtypes = []
user32.ReleaseCapture.restype = wintypes.BOOL
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
user32.LoadCursorW.restype = wintypes.HANDLE
user32.SetCursor.argtypes = [wintypes.HANDLE]
user32.SetCursor.restype = wintypes.HANDLE
user32.MonitorFromPoint.argtypes = [POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = wintypes.HANDLE
user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
gdi32.CreateCompatibleDC.restype = wintypes.HDC
gdi32.CreateDIBSection.argtypes = [
    wintypes.HDC, ctypes.POINTER(BITMAPINFO), wintypes.UINT,
    ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = wintypes.HBITMAP
gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HANDLE]
gdi32.SelectObject.restype = wintypes.HANDLE
gdi32.DeleteObject.argtypes = [wintypes.HANDLE]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


def _load_font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont:
    candidates = (
        ["msyhbd.ttc", "msyh.ttc", "segoeui.ttf"]
        if bold
        else ["msyh.ttc", "segoeui.ttf"]
    )
    for name in candidates:
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def _mix(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = max(0.0, min(1.0, t))
    return (
        int(round(c1[0] + (c2[0] - c1[0]) * t)),
        int(round(c1[1] + (c2[1] - c1[1]) * t)),
        int(round(c1[2] + (c2[2] - c1[2]) * t)),
    )


def _draw_grad_circle(diameter: int, c1: Tuple[int, int, int], c2: Tuple[int, int, int], alpha: int = 255) -> Image.Image:
    """绘制 4x 超采样抗锯齿斜向渐变圆"""
    grad = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    px = grad.load()
    denom = max(1, (diameter - 1) * 2)
    for y in range(diameter):
        for x in range(diameter):
            t = (x + y) / denom
            r, g, b = _mix(c1, c2, t)
            px[x, y] = (r, g, b, alpha)
    mask = Image.new("L", (diameter * 4, diameter * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=255)
    mask = mask.resize((diameter, diameter), Image.LANCZOS)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(grad, (0, 0), mask)
    return out


def _draw_rr(
    size: Tuple[int, int],
    radius: float,
    fill=None,
    outline=None,
    width: int = 1,
) -> Image.Image:
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    scale = 4
    big = Image.new("RGBA", (w * scale, h * scale), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle(
        (0, 0, w * scale - 1, h * scale - 1),
        radius=int(round(radius * scale)),
        fill=fill,
        outline=outline,
        width=max(1, int(round(width * scale))) if outline else 0,
    )
    return big.resize((w, h), Image.LANCZOS)


class FloatingMicButton:
    """
    桌面悬浮语音按钮（点击开始录音 / 再次点击结束识别 / 可自由拖动 / 右键隐藏）
    """

    def __init__(self, app, on_user_hide: Optional[Callable[[], None]] = None):
        self.app = app
        self.on_user_hide = on_user_hide

        # 专属 ShortcutTask：threshold=0.0 确保点击悬浮球立即弹出底部字幕条并开始录音
        self._shortcut_cfg = Shortcut(
            key="floating_mic",
            type="mouse",
            suppress=False,
            hold_mode=False,
            threshold=0.0,
            enabled=True,
        )
        self._task = ShortcutTask(self.app, self._shortcut_cfg)

        self.hwnd: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._ready_evt = threading.Event()
        self._wndproc_ref = None

        # 状态
        self._visible = False
        self._hover_main = False
        self._hover_menu = False
        self._pressed_main = False
        self._menu_open = False
        self._tracking_leave = False
        self._last_recording_state = False

        # 拖拽状态
        self._lbutton_down = False
        self._dragging = False
        self._down_screen_pt = (0, 0)
        self._down_win_pt = (0, 0)

        # DPI 缩放与几何尺寸
        self.s = self._detect_scale()
        self._init_geometry()

    def _detect_scale(self) -> float:
        try:
            hdc = user32.GetDC(0)
            dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
            user32.ReleaseDC(0, hdc)
            if dpi and dpi > 0:
                return max(1.0, min(2.25, dpi / 96.0))
        except Exception:
            pass
        return 1.0

    def _init_geometry(self) -> None:
        s = self.s
        # 画布尺寸：左侧悬浮球区域 + 右侧右键“隐藏按钮”胶囊展开区
        self.glow_pad = int(round(14 * s))
        self.orb_d = int(round(54 * s))
        self.menu_gap = int(round(8 * s))
        self.menu_w = int(round(108 * s))
        self.menu_h = int(round(36 * s))

        self.win_w = self.glow_pad * 2 + self.orb_d + self.menu_gap + self.menu_w
        self.win_h = self.glow_pad * 2 + self.orb_d

        # 悬浮球圆心与矩形
        self.orb_x0 = self.glow_pad
        self.orb_y0 = self.glow_pad
        self.orb_cx = self.orb_x0 + self.orb_d // 2
        self.orb_cy = self.orb_y0 + self.orb_d // 2
        self.orb_r = self.orb_d // 2

        # 右键菜单胶囊矩形
        self.menu_x0 = self.orb_x0 + self.orb_d + self.menu_gap
        self.menu_y0 = self.orb_cy - self.menu_h // 2
        self.menu_rect = (
            self.menu_x0,
            self.menu_y0,
            self.menu_x0 + self.menu_w,
            self.menu_y0 + self.menu_h,
        )

        self.f_menu = _load_font(int(round(12 * s)), bold=True)
        self.f_rec = _load_font(int(round(9 * s)), bold=True)

        # 默认初始位置：主屏幕右下角偏上（避开任务栏与右下角通知区）
        work = self._get_work_area(POINT(200, 200))
        self.win_x = max(work.left + 20, work.right - self.win_w - int(round(36 * s)))
        self.win_y = max(work.top + 40, work.bottom - self.win_h - int(round(140 * s)))

    def _get_work_area(self, pt: POINT) -> RECT:
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        hmon = user32.MonitorFromPoint(pt, MONITOR_DEFAULTTONEAREST)
        if hmon and user32.GetMonitorInfoW(hmon, ctypes.byref(mi)):
            return mi.rcWork
        rc = RECT()
        rc.left, rc.top, rc.right, rc.bottom = 0, 0, 1920, 1040
        return rc

    # ── 公共线程安全接口 ─────────────────────────────────────────
    def start_thread_if_needed(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._ready_evt.clear()
        self._thread = threading.Thread(
            target=self._ui_thread_main,
            name="FloatingMicUI",
            daemon=True,
        )
        self._thread.start()
        self._ready_evt.wait(timeout=2.0)

    def show(self) -> None:
        self.start_thread_if_needed()
        self._visible = True
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_SHOW, 0, 0)

    def hide(self) -> None:
        self._visible = False
        self._menu_open = False
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_HIDE, 0, 0)

    def set_visible(self, enabled: bool) -> None:
        if enabled:
            self.show()
        else:
            self.hide()

    def is_visible(self) -> bool:
        return bool(self._visible)

    def stop(self) -> None:
        self._visible = False
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_CLOSE, 0, 0)

    # ── 录音开关核心逻辑 ─────────────────────────────────────────
    def _is_recording(self) -> bool:
        try:
            return bool(self.app and self.app.state and self.app.state.recording)
        except Exception:
            return False

    def toggle_recording(self) -> None:
        """左键轻点悬浮按钮：未录音 -> 立即开始录音；录音中 -> 立即停止并输出结果"""
        if not self.app or not hasattr(self.app, "shortcut"):
            return

        if self._is_recording():
            # 停止当前所有活跃的录音任务（无论是悬浮球启动还是键盘/鼠标按住启动）
            def _stop_all():
                if self._task.is_recording:
                    self._task.finish()
                for t in list(getattr(self.app.shortcut, "tasks", {}).values()):
                    if t.is_recording:
                        t.finish()

            self.app.shortcut._submit_action(_stop_all)
        else:
            # 启动悬浮球免按住录音
            def _start_floating():
                if not self._is_recording():
                    self._task.launch()

            self.app.shortcut._submit_action(_start_floating)

        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REDRAW, 0, 0)

    # ── 视觉渲染（4x 超采样抗锯齿）──────────────────────────────
    def _draw_mic_or_stop_glyph(self, size: int, is_recording: bool) -> Image.Image:
        scale = 4
        S = size * scale
        img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        cx, cy = S / 2.0, S / 2.0
        white = (255, 255, 255, 255)

        if is_recording:
            # 录音态：醒目的白色圆角正方形停止图标 (⏹)
            half = S * 0.25
            r = S * 0.08
            d.rounded_rectangle(
                (cx - half, cy - half, cx + half, cy + half),
                radius=int(round(r)),
                fill=white,
            )
        else:
            # 待机态：精致矢量麦克风图标
            stroke = max(2, int(round(S * 0.085)))
            cap_w = S * 0.28
            cap_h = S * 0.44
            cap_top = cy - S * 0.34
            d.rounded_rectangle(
                (cx - cap_w / 2, cap_top, cx + cap_w / 2, cap_top + cap_h),
                radius=int(round(cap_w / 2)),
                fill=white,
            )
            arc_r = S * 0.25
            arc_cy = cap_top + cap_h - S * 0.11
            d.arc(
                (cx - arc_r, arc_cy - arc_r, cx + arc_r, arc_cy + arc_r),
                start=12,
                end=168,
                fill=white,
                width=stroke,
            )
            stem_top = arc_cy + arc_r
            stem_bot = cy + S * 0.34
            d.line((cx, stem_top, cx, stem_bot), fill=white, width=stroke)
            foot_w = S * 0.28
            d.line(
                (cx - foot_w / 2, stem_bot, cx + foot_w / 2, stem_bot),
                fill=white,
                width=stroke,
            )

        return img.resize((size, size), Image.LANCZOS)

    def render_preview_frame(
        self,
        recording: Optional[bool] = None,
        menu_open: Optional[bool] = None,
        now: Optional[float] = None,
    ) -> Image.Image:
        """渲染当前悬浮球帧（可供 UI 线程与自动化视觉测试直接调用）"""
        if now is None:
            now = time.perf_counter()
        if recording is None:
            recording = self._is_recording()
        if menu_open is None:
            menu_open = self._menu_open

        s = self.s
        frame = Image.new("RGBA", (self.win_w, self.win_h), (0, 0, 0, 0))
        cx, cy = self.orb_cx, self.orb_cy
        orb_d = self.orb_d

        # 1. 外围呼吸光晕 (Outer Aura Glow)
        pulse = 0.5 + 0.5 * math.sin(now * (5.2 if recording else 2.2))
        glow_layer = Image.new("RGBA", (self.win_w, self.win_h), (0, 0, 0, 0))
        gd = ImageDraw.Draw(glow_layer)
        if recording:
            glow_r = int(round(self.orb_r + (4 + 5 * pulse) * s))
            glow_alpha = int(round(95 + 55 * pulse))
            gd.ellipse(
                (cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r),
                fill=ROSE_C1 + (glow_alpha,),
            )
        else:
            glow_r = int(round(self.orb_r + (3 if self._hover_main else 1) * s))
            glow_alpha = 85 if self._hover_main else 48
            gd.ellipse(
                (cx - glow_r, cy - glow_r, cx + glow_r, cy + glow_r),
                fill=ACCENT_C1 + (glow_alpha,),
            )
        glow_layer = glow_layer.filter(ImageFilter.GaussianBlur(radius=5.5 * s))
        frame.alpha_composite(glow_layer)

        # 2. 录音态扩散脉冲环 (Expanding Ripple Ring)
        if recording:
            ripple_phase = (now * 1.15) % 1.0
            rip_d = int(round(orb_d + ripple_phase * 18 * s))
            rip_alpha = int(round((1.0 - ripple_phase) * 155))
            if rip_alpha > 8:
                rip_big = Image.new("RGBA", (rip_d * 4, rip_d * 4), (0, 0, 0, 0))
                ImageDraw.Draw(rip_big).ellipse(
                    (2, 2, rip_d * 4 - 3, rip_d * 4 - 3),
                    outline=ROSE_C1 + (rip_alpha,),
                    width=max(4, int(round(6 * s))),
                )
                rip_img = rip_big.resize((rip_d, rip_d), Image.LANCZOS)
                frame.alpha_composite(rip_img, (cx - rip_d // 2, cy - rip_d // 2))

        # 3. 深空毛玻璃外环底座 (Frosted Dark Glass Shell)
        shell_big = Image.new("RGBA", (orb_d * 4, orb_d * 4), (0, 0, 0, 0))
        sd = ImageDraw.Draw(shell_big)
        border_col = (
            ROSE_C1 + (235,)
            if recording
            else (
                ACCENT_C1 + (220,)
                if self._hover_main
                else (255, 255, 255, 65)
            )
        )
        sd.ellipse(
            (2, 2, orb_d * 4 - 3, orb_d * 4 - 3),
            fill=BG_GLASS + (242,),
            outline=border_col,
            width=max(4, int(round(5.5 * s))),
        )
        shell_img = shell_big.resize((orb_d, orb_d), Image.LANCZOS)
        frame.alpha_composite(shell_img, (self.orb_x0, self.orb_y0))

        # 4. 内部渐变核心球 (Gradient Inner Orb)
        inner_pad = int(round((6 if not self._pressed_main else 8) * s))
        inner_d = orb_d - inner_pad * 2
        if recording:
            c1 = _mix(ROSE_C1, (255, 160, 175), 0.25 * pulse)
            c2 = ROSE_C2
        else:
            c1 = _mix(ACCENT_C1, (125, 211, 252), 0.25 if self._hover_main else 0.0)
            c2 = _mix(ACCENT_C2, (167, 139, 250), 0.25 if self._hover_main else 0.0)
        core_img = _draw_grad_circle(inner_d, c1, c2, 245)
        frame.alpha_composite(core_img, (cx - inner_d // 2, cy - inner_d // 2))

        # 5. 中心图标：待机麦克风 vs 录音红色方块停止键 (⏹)
        ico_sz = int(round(24 * s))
        glyph = self._draw_mic_or_stop_glyph(ico_sz, is_recording=recording)
        frame.alpha_composite(glyph, (cx - ico_sz // 2, cy - ico_sz // 2))

        # 6. 右键弹出胶囊菜单：[× 隐藏按钮]
        if menu_open:
            mx0, my0, mx1, my1 = self.menu_rect
            mw, mh = mx1 - mx0, my1 - my0
            bg_col = (30, 38, 58, 245) if self._hover_menu else (18, 23, 38, 240)
            bd_col = ROSE_C1 + (210,) if self._hover_menu else (255, 255, 255, 70)
            pill = _draw_rr((mw, mh), mh / 2, fill=bg_col, outline=bd_col, width=1)
            frame.alpha_composite(pill, (mx0, my0))

            d = ImageDraw.Draw(frame)
            # 绘制小型关闭叉号 + “隐藏悬浮球”文字
            ix = mx0 + int(round(16 * s))
            iy = my0 + mh // 2
            r_x = int(round(4 * s))
            x_col = ROSE_C1 + (255,) if self._hover_menu else TEXT_SECONDARY + (235,)
            d.line((ix - r_x, iy - r_x, ix + r_x, iy + r_x), fill=x_col, width=max(1, int(round(1.8 * s))))
            d.line((ix - r_x, iy + r_x, ix + r_x, iy - r_x), fill=x_col, width=max(1, int(round(1.8 * s))))
            txt_col = TEXT_PRIMARY + (255,) if self._hover_menu else TEXT_SECONDARY + (245,)
            d.text(
                (ix + int(round(10 * s)), iy),
                "隐藏悬浮球",
                font=self.f_menu,
                fill=txt_col,
                anchor="lm",
            )

        return frame

    def _present(self) -> None:
        if not self.hwnd or not self._visible:
            return
        frame = self.render_preview_frame()
        w, h = frame.size
        raw_bgra = frame.tobytes("raw", "BGRA")

        hdc_screen = user32.GetDC(0)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)

        bmi = BITMAPINFO()
        bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
        bmi.bmiHeader.biWidth = w
        bmi.bmiHeader.biHeight = -h  # Top-down DIB
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = BI_RGB

        ppv_bits = ctypes.c_void_p()
        hbm = gdi32.CreateDIBSection(
            hdc_screen, ctypes.byref(bmi), DIB_RGB_COLORS, ctypes.byref(ppv_bits), None, 0
        )
        if hbm and ppv_bits.value:
            old_bm = gdi32.SelectObject(hdc_mem, hbm)
            # 预乘 Alpha
            arr = bytearray(raw_bgra)
            for i in range(0, len(arr), 4):
                a = arr[i + 3]
                if a == 0:
                    arr[i] = arr[i + 1] = arr[i + 2] = 0
                elif a < 255:
                    arr[i] = (arr[i] * a) // 255
                    arr[i + 1] = (arr[i + 1] * a) // 255
                    arr[i + 2] = (arr[i + 2] * a) // 255
            ctypes.memmove(ppv_bits.value, bytes(arr), len(arr))

            pt_dst = POINT(int(self.win_x), int(self.win_y))
            sz = SIZE(w, h)
            pt_src = POINT(0, 0)
            blend = BLENDFUNCTION(AC_SRC_OVER, 0, 255, AC_SRC_ALPHA)
            user32.UpdateLayeredWindow(
                self.hwnd,
                hdc_screen,
                ctypes.byref(pt_dst),
                ctypes.byref(sz),
                hdc_mem,
                ctypes.byref(pt_src),
                0,
                ctypes.byref(blend),
                ULW_ALPHA,
            )
            gdi32.SelectObject(hdc_mem, old_bm)
            gdi32.DeleteObject(hbm)

        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(0, hdc_screen)

    # ── 命中检测 ─────────────────────────────────────────────────
    def _hit_main_orb(self, lx: int, ly: int) -> bool:
        return math.hypot(lx - self.orb_cx, ly - self.orb_cy) <= (self.orb_r + 2 * self.s)

    def _hit_menu_pill(self, lx: int, ly: int) -> bool:
        if not self._menu_open:
            return False
        x0, y0, x1, y1 = self.menu_rect
        return x0 <= lx <= x1 and y0 <= ly <= y1

    # ── Win32 窗口过程 ───────────────────────────────────────────
    def _ui_thread_main(self) -> None:
        hinst = kernel32.GetModuleHandleW(None)
        cls_name = f"CapsWriterFloatingMic_{id(self)}"
        self._wndproc_ref = WNDPROC(self._wndproc)

        wc = WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.hCursor = user32.LoadCursorW(None, ctypes.cast(IDC_HAND, wintypes.LPCWSTR))
        wc.lpszClassName = cls_name
        user32.RegisterClassExW(ctypes.byref(wc))

        ex_style = WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
        self.hwnd = user32.CreateWindowExW(
            ex_style,
            cls_name,
            "CapsWriterFloatingMic",
            WS_POPUP,
            int(self.win_x),
            int(self.win_y),
            int(self.win_w),
            int(self.win_h),
            None,
            None,
            hinst,
            None,
        )
        self._ready_evt.set()
        if not self.hwnd:
            logger.warning("创建桌面悬浮语音按钮窗口失败")
            return

        # 30fps 轻量定时器：同步外部按键录音状态并驱动录音态呼吸脉冲动画
        user32.SetTimer(self.hwnd, TIMER_PULSE, 40, None)

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

    def _wndproc(self, hwnd, msg, wparam, lparam):
        if msg == WM_MOUSEACTIVATE:
            # 关键：点击悬浮球绝不激活窗口，保持原活动输入框焦点
            return MA_NOACTIVATE

        if msg == WM_SETCURSOR:
            cur_id = IDC_SIZEALL if self._dragging else IDC_HAND
            user32.SetCursor(user32.LoadCursorW(None, ctypes.cast(cur_id, wintypes.LPCWSTR)))
            return 1

        if msg == WM_APP_SHOW:
            self._visible = True
            self._present()
            user32.ShowWindow(hwnd, SW_SHOWNOACTIVATE)
            user32.SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_SHOWWINDOW,
            )
            return 0

        if msg == WM_APP_HIDE:
            self._visible = False
            self._menu_open = False
            user32.ShowWindow(hwnd, SW_HIDE)
            return 0

        if msg == WM_APP_REDRAW:
            if self._visible:
                self._present()
            return 0

        if msg == WM_APP_CLOSE:
            user32.KillTimer(hwnd, TIMER_PULSE)
            user32.DestroyWindow(hwnd)
            return 0

        if msg == WM_TIMER and wparam == TIMER_PULSE:
            if not self._visible:
                return 0
            rec = self._is_recording()
            if rec or rec != self._last_recording_state:
                self._last_recording_state = rec
                self._present()
            return 0

        if msg == WM_MOUSEMOVE:
            if not self._tracking_leave:
                tme = TRACKMOUSEEVENT(ctypes.sizeof(TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0)
                user32.TrackMouseEvent(ctypes.byref(tme))
                self._tracking_leave = True

            if self._lbutton_down:
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                dx = pt.x - self._down_screen_pt[0]
                dy = pt.y - self._down_screen_pt[1]
                if not self._dragging and math.hypot(dx, dy) > 5.0 * self.s:
                    self._dragging = True
                    self._menu_open = False
                if self._dragging:
                    work = self._get_work_area(pt)
                    nx = self._down_win_pt[0] + dx
                    ny = self._down_win_pt[1] + dy
                    # 保证悬浮球主体不超出当前显示器工作区
                    min_x = work.left - self.orb_x0
                    max_x = work.right - (self.orb_x0 + self.orb_d)
                    min_y = work.top - self.orb_y0
                    max_y = work.bottom - (self.orb_y0 + self.orb_d)
                    self.win_x = max(min_x, min(max_x, nx))
                    self.win_y = max(min_y, min(max_y, ny))
                    self._present()
                return 0

            lx = ctypes.c_short(lparam & 0xFFFF).value
            ly = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            hm = self._hit_main_orb(lx, ly)
            hmenu = self._hit_menu_pill(lx, ly)
            if hm != self._hover_main or hmenu != self._hover_menu:
                self._hover_main = hm
                self._hover_menu = hmenu
                self._present()
            return 0

        if msg == WM_MOUSELEAVE:
            self._tracking_leave = False
            if self._hover_main or self._hover_menu or self._pressed_main:
                self._hover_main = False
                self._hover_menu = False
                self._pressed_main = False
                self._present()
            return 0

        if msg == WM_LBUTTONDOWN:
            lx = ctypes.c_short(lparam & 0xFFFF).value
            ly = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            if self._hit_menu_pill(lx, ly):
                # 点击了右键菜单的“隐藏悬浮球”
                self._menu_open = False
                self.hide()
                if self.on_user_hide:
                    threading.Thread(target=self.on_user_hide, daemon=True).start()
                return 0

            if self._hit_main_orb(lx, ly):
                pt = POINT()
                user32.GetCursorPos(ctypes.byref(pt))
                self._lbutton_down = True
                self._dragging = False
                self._pressed_main = True
                self._down_screen_pt = (pt.x, pt.y)
                self._down_win_pt = (self.win_x, self.win_y)
                user32.SetCapture(hwnd)
                self._present()
            elif self._menu_open:
                self._menu_open = False
                self._present()
            return 0

        if msg == WM_LBUTTONUP:
            was_down = self._lbutton_down
            was_dragging = self._dragging
            self._lbutton_down = False
            self._dragging = False
            self._pressed_main = False
            if was_down:
                user32.ReleaseCapture()
                if not was_dragging:
                    if self._menu_open:
                        self._menu_open = False
                        self._present()
                    else:
                        self.toggle_recording()
                else:
                    self._present()
            return 0

        if msg == WM_RBUTTONUP:
            lx = ctypes.c_short(lparam & 0xFFFF).value
            ly = ctypes.c_short((lparam >> 16) & 0xFFFF).value
            if self._hit_main_orb(lx, ly) or self._hit_menu_pill(lx, ly):
                self._menu_open = not self._menu_open
                self._present()
            return 0

        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
