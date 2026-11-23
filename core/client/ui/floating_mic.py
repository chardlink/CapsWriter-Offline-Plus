# coding: utf-8
"""
桌面悬浮语音识别按钮 (FloatingMicButton) —— 科幻流体毛玻璃质感版

视觉与交互特性：
1. 与底部流式字幕悬浮条 (RecordingOverlay) 同源的科幻流体毛玻璃美学：
   - 预渲染高扩散双色霓虹外发光 (GaussianBlur 16*s) + 3D 顶部镜面高光玻璃底座 + 渐变流光边框。
   - 充裕的 32*s 四周全透明光晕画布，彻底杜绝呼吸光环或扩散波纹在上下左右被窗口边界截断。
   - 纯色彩光晕扩散与声波驱动呼吸（无任何灰边伪影），直接联动麦克风实时音量 (overlay._level_in) 驱动光晕、波纹与球心微呼吸。
   - 待机态：深空流体玻璃底座 + 极光青-霓虹紫 3D 球心 + 发光矢量麦克风。
   - 录音态：绯红-玫瑰霓虹脉冲光晕 + 双层水波纹 + 3D 绯红发光球心 + 带柔和辉光的圆角正方形停止键 (⏹)。
2. 零焦点抢占 (WS_EX_NOACTIVATE + WM_MOUSEACTIVATE -> MA_NOACTIVATE)：
   - 点击悬浮球开始/停止录音时，绝不抢占当前打字窗口焦点，识别完成后直接粘贴到原光标处。
3. 自由拖拽 + 屏幕中轴线智能左右展开右键菜单 + 点击任意空白处自动收起右键菜单。
"""

import ctypes
from ctypes import wintypes
import math
import threading
import time
from typing import Callable, Optional, Tuple

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

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

VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
VK_MBUTTON = 0x04
VK_ESCAPE = 0x1B

# ── 与 RecordingOverlay 完全同源的科幻色板 ────────────────────────
BODY_COLOR = (14, 18, 30, 178)     # 深空半透明毛玻璃底色
ACCENT_C1 = (56, 189, 248)         # 极光青 #38BDF8
ACCENT_C2 = (139, 92, 246)         # 霓虹紫 #8B5CF6
REC_C1 = (251, 113, 133)           # 珊瑚霓虹红 #FB7185
REC_C2 = (236, 72, 153)            # 电光品红 #EC4899
REC_CORE_C2 = (225, 29, 72)        # 绯红核心 #E11D48
TEXT_PRIMARY = (246, 248, 255)
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
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = wintypes.SHORT

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


def _clamp(v: float, lo: float = 0.0, hi: float = 1.0) -> float:
    return lo if v < lo else hi if v > hi else v


def _ease_out(t: float) -> float:
    t = _clamp(t)
    return 1.0 - (1.0 - t) ** 3


def _mix(c1: Tuple[int, int, int], c2: Tuple[int, int, int], t: float) -> Tuple[int, int, int]:
    t = _clamp(t)
    return (
        int(round(c1[0] + (c2[0] - c1[0]) * t)),
        int(round(c1[1] + (c2[1] - c1[1]) * t)),
        int(round(c1[2] + (c2[2] - c1[2]) * t)),
    )


def _scale_alpha(img: Image.Image, k: float) -> Image.Image:
    if k >= 0.999:
        return img
    out = img.copy()
    a = (np.asarray(out.getchannel("A"), dtype=np.float32) * _clamp(k)).astype(np.uint8)
    out.putalpha(Image.fromarray(a, "L"))
    return out


def _diag_grad(width: int, height: int, c1: Tuple[int, int, int], c2: Tuple[int, int, int]) -> Image.Image:
    """生成对角线平滑双色渐变图（纯彩 RGB + 满 Alpha，彻底杜绝黑色边缘混色发灰）"""
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    denom = max(1.0, float(width + height - 2))
    t = np.clip((xx + yy) / denom, 0.0, 1.0)[..., None]
    rgb = np.asarray(c1, np.float32) * (1.0 - t) + np.asarray(c2, np.float32) * t
    arr = np.empty((height, width, 4), np.uint8)
    arr[..., :3] = rgb.astype(np.uint8)
    arr[..., 3] = 255
    return Image.fromarray(arr, "RGBA")


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


class FloatingMicButton:
    """
    桌面悬浮语音按钮（科幻流体毛玻璃质感 · 点击开始/再次点击结束 · 自由拖动 · 右键隐藏）
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

        # 状态与平滑过渡动画
        self._visible = False
        self._hover_main = False
        self._hover_menu = False
        self._pressed_main = False
        self._menu_open = False
        self._menu_opened_at = 0.0
        self._tracking_leave = False
        self._last_recording_state = False
        self._theme_at = 0.0
        self._lvl = 0.0  # 平滑后的实时声波能量 (0.0 ~ 1.0)

        # 拖拽状态
        self._lbutton_down = False
        self._dragging = False
        self._down_screen_pt = (0, 0)
        self._down_win_pt = (0, 0)

        # DPI 缩放与预渲染高品质资源
        self.s = self._detect_scale()
        self._init_geometry()
        self._build_assets()

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
        # 关键升级：将四周光晕留白扩大到 32*s（原先仅 14*s），
        # 确保 GaussianBlur(14*s) 呼吸光晕与扩散波纹在上下左右拥有充足空间，绝不截断！
        self.glow_pad = int(round(32 * s))
        self.orb_d = int(round(58 * s))          # 外层流体毛玻璃舱直径
        self.orb_r = self.orb_d // 2
        self.core_r = int(round(20 * s))         # 内部 3D 渐变发光球体半径
        self.orb_box = self.orb_d + self.glow_pad * 2  # 局部球体+光晕完整正方形画布 (122*s)

        self.menu_gap = int(round(10 * s))
        self.menu_w = int(round(114 * s))
        self.menu_h = int(round(38 * s))

        # 窗口总尺寸：中间悬浮球 + 左右对称的右键菜单展开区
        self.win_w = self.orb_box + (self.menu_gap + self.menu_w) * 2
        self.win_h = self.orb_box

        # 悬浮球在窗口内的几何坐标
        self.box_x0 = self.menu_w + self.menu_gap
        self.box_y0 = 0
        self.orb_x0 = self.box_x0 + self.glow_pad
        self.orb_y0 = self.glow_pad
        self.orb_cx = self.orb_x0 + self.orb_r
        self.orb_cy = self.orb_y0 + self.orb_r

        # 右侧与左侧菜单胶囊矩形
        self.menu_y0 = self.orb_cy - self.menu_h // 2
        self.menu_x0_right = self.orb_x0 + self.orb_d + self.menu_gap
        self.menu_x0_left = self.orb_x0 - self.menu_gap - self.menu_w
        self.menu_rect_right = (
            self.menu_x0_right,
            self.menu_y0,
            self.menu_x0_right + self.menu_w,
            self.menu_y0 + self.menu_h,
        )
        self.menu_rect_left = (
            self.menu_x0_left,
            self.menu_y0,
            self.menu_x0_left + self.menu_w,
            self.menu_y0 + self.menu_h,
        )
        self._menu_side = "left"
        self.menu_rect = self.menu_rect_left

        self.f_menu = _load_font(int(round(12.5 * s)), bold=True)

        # 默认初始位置：主屏幕右下角偏上（避开右侧/底部任务栏与通知区）
        work = self._get_work_area(POINT(200, 200))
        target_orb_right = work.right - int(round(36 * s))
        self.win_x = max(
            work.left - self.orb_x0 + 20,
            target_orb_right - (self.orb_x0 + self.orb_d),
        )
        self.win_y = max(work.top + 40, work.bottom - self.win_h - int(round(130 * s)))
        self._update_menu_side()

    def _build_assets(self) -> None:
        """采用与 RecordingOverlay 完全一致的光学着色算法预渲染玻璃舱、柔和霓虹光晕与 3D 球心"""
        s, ss = self.s, 4
        B = self.orb_box
        cx = cy = B / 2.0
        R = self.orb_r

        # 1. 外层毛玻璃圆舱蒙版 (orb_box x orb_box)
        big_mask = Image.new("L", (B * ss, B * ss), 0)
        ImageDraw.Draw(big_mask).ellipse(
            ((cx - R) * ss, (cy - R) * ss, (cx + R) * ss - 1, (cy + R) * ss - 1),
            fill=255,
        )
        self._shell_mask = big_mask.resize((B, B), Image.LANCZOS)

        # 2. 外围柔和双色霓虹光晕（纯彩 RGB + Alpha 扩散，绝不产生黑灰暗边）
        self._glow_idle = self._make_box_glow(ACCENT_C1, ACCENT_C2, blur_px=13.5 * s, boost=1.35)
        self._glow_rec = self._make_box_glow(REC_C1, REC_C2, blur_px=14.5 * s, boost=1.55)

        # 3. 3D 流体毛玻璃底座（深空半透明底 + 顶部曲面玻璃反光高光 + 渐变流光细描边）
        self._glass_body_idle = self._make_glass_shell(ACCENT_C1, ACCENT_C2, border_alpha=115)
        self._glass_body_hover = self._make_glass_shell(ACCENT_C1, ACCENT_C2, border_alpha=215)
        self._glass_body_rec = self._make_glass_shell(REC_C1, REC_C2, border_alpha=225)

        # 4. 内部 3D 渐变核心球（带顶部弧形高光 + 发光矢量图标）
        self._core_idle = self._make_core_orb(ACCENT_C1, ACCENT_C2, is_recording=False)
        self._core_rec = self._make_core_orb(REC_C1, REC_CORE_C2, is_recording=True)

        # 5. 右键弹出胶囊菜单（预渲染常态与悬停态科幻毛玻璃胶囊）
        self._menu_pill_idle = self._make_menu_pill(hovered=False)
        self._menu_pill_hover = self._make_menu_pill(hovered=True)

    def _make_box_glow(
        self,
        c1: Tuple[int, int, int],
        c2: Tuple[int, int, int],
        blur_px: float,
        boost: float,
    ) -> Image.Image:
        """与 RecordingOverlay._make_glow 同源：纯彩渐变图套高斯模糊 Alpha，彻底消除灰边"""
        B = self.orb_box
        grad = _diag_grad(B, B, c1, c2)
        spread = self._shell_mask.filter(ImageFilter.GaussianBlur(blur_px))
        spread = spread.point(lambda v: min(255, int(v * boost)))
        # 舱体内部减淡光晕，突出深邃毛玻璃质感
        inner = self._shell_mask.point(lambda v: 255 - int(v * 0.72))
        grad.putalpha(ImageChops.multiply(spread, inner))
        return grad

    def _make_glass_shell(
        self,
        c1: Tuple[int, int, int],
        c2: Tuple[int, int, int],
        border_alpha: int,
    ) -> Image.Image:
        """与 RecordingOverlay._make_body 同源：深空毛玻璃 + 顶部曲面反光 + 双色流光细描边"""
        s, ss = self.s, 4
        B = self.orb_box
        cx = cy = B / 2.0
        R = self.orb_r

        body = Image.new("RGBA", (B, B), BODY_COLOR[:3] + (0,))
        body.putalpha(
            ImageChops.multiply(self._shell_mask, Image.new("L", (B, B), BODY_COLOR[3]))
        )

        # 顶部曲面玻璃高光 (Specular Sheen)
        g = np.zeros((B, 1), np.float32)
        top = cy - R
        mid = top + (2 * R) * 0.56
        ys = np.arange(B)
        g[:, 0] = np.clip(1.0 - (ys - top) / max(1.0, (mid - top)), 0.0, 1.0) ** 1.6 * 34.0
        sheen = Image.new("RGBA", (B, B), (255, 255, 255, 0))
        sheen.putalpha(
            ImageChops.multiply(
                Image.fromarray(np.broadcast_to(g, (B, B)).astype(np.uint8), "L"),
                self._shell_mask,
            )
        )
        body = Image.alpha_composite(body, sheen)

        # 双色流光抗锯齿细描边
        ring_mask_big = Image.new("L", (B * ss, B * ss), 0)
        ImageDraw.Draw(ring_mask_big).ellipse(
            ((cx - R) * ss, (cy - R) * ss, (cx + R) * ss - 1, (cy + R) * ss - 1),
            outline=border_alpha,
            width=max(ss, int(round(1.35 * s * ss))),
        )
        ring_mask = ring_mask_big.resize((B, B), Image.LANCZOS)
        ring_grad = _diag_grad(B, B, _mix(c1, (255, 255, 255), 0.25), c2)
        ring_grad.putalpha(ring_mask)
        return Image.alpha_composite(body, ring_grad)

    def _make_core_orb(
        self,
        c1: Tuple[int, int, int],
        c2: Tuple[int, int, int],
        is_recording: bool,
    ) -> Image.Image:
        """与 RecordingOverlay._make_orb 完全同源的 3D 球心 + 顶部弧形高光 + 发光矢量图标"""
        s, ss = self.s, 4
        B = self.orb_box
        box = B * ss
        r = self.core_r * ss
        c = box / 2.0

        # 1. 对角线渐变球体
        yy, xx = np.mgrid[0:box, 0:box].astype(np.float32)
        t = np.clip(((xx - c + r) + (yy - c + r)) / max(1.0, (4.0 * r)), 0.0, 1.0)[..., None]
        rgb = np.asarray(c1, np.float32) * (1.0 - t) + np.asarray(c2, np.float32) * t
        arr = np.zeros((box, box, 4), np.uint8)
        arr[..., :3] = rgb.astype(np.uint8)
        mask = Image.new("L", (box, box), 0)
        ImageDraw.Draw(mask).ellipse((c - r, c - r, c + r, c + r), fill=255)
        arr[..., 3] = np.asarray(mask)
        img = Image.fromarray(arr, "RGBA")

        # 2. 顶部曲面高光 (3D 玻璃球质感核心)
        hl = Image.new("RGBA", (box, box), (255, 255, 255, 0))
        hm = Image.new("L", (box, box), 0)
        ImageDraw.Draw(hm).ellipse(
            (c - r * 0.72, c - r * 0.92, c + r * 0.72, c - r * 0.05),
            fill=54,
        )
        hl.putalpha(ImageChops.multiply(hm.filter(ImageFilter.GaussianBlur(r * 0.12)), mask))
        img = Image.alpha_composite(img, hl)

        # 3. 中心矢量图标（带柔和白光辉光）
        glyph_layer = Image.new("RGBA", (box, box), (0, 0, 0, 0))
        d = ImageDraw.Draw(glyph_layer)
        white = (255, 255, 255, 255)
        lw = max(2, int(round(r * 0.105)))

        if is_recording:
            # 录音态：精致圆角正方形停止键 (⏹)
            half = r * 0.36
            rad = r * 0.115
            d.rounded_rectangle(
                (c - half, c - half, c + half, c + half),
                radius=int(round(rad)),
                fill=white,
            )
        else:
            # 待机态：与 RecordingOverlay 完全一致的矢量麦克风
            d.rounded_rectangle(
                (c - r * 0.19, c - r * 0.56, c + r * 0.19, c + r * 0.12),
                radius=r * 0.19,
                fill=white,
            )
            d.arc(
                (c - r * 0.37, c - r * 0.3, c + r * 0.37, c + r * 0.36),
                start=0,
                end=180,
                fill=white,
                width=lw,
            )
            d.rounded_rectangle(
                (c - lw / 2, c + r * 0.34, c + lw / 2, c + r * 0.54),
                radius=lw / 2,
                fill=white,
            )
            d.rounded_rectangle(
                (c - r * 0.22, c + r * 0.5, c + r * 0.22, c + r * 0.5 + lw),
                radius=lw / 2,
                fill=white,
            )

        glyph_small = glyph_layer.resize((B, B), Image.LANCZOS)
        orb_small = img.resize((B, B), Image.LANCZOS)

        # 为中心图标叠加一层微妙的科幻内发光
        glow_a = glyph_small.getchannel("A").filter(ImageFilter.GaussianBlur(2.2 * s))
        glow_a = glow_a.point(lambda v: int(v * 0.42))
        glyph_glow = Image.new("RGBA", (B, B), (255, 255, 255, 0))
        glyph_glow.putalpha(glow_a)
        orb_small = Image.alpha_composite(orb_small, glyph_glow)
        orb_small = Image.alpha_composite(orb_small, glyph_small)
        return orb_small

    def _make_menu_pill(self, hovered: bool) -> Image.Image:
        """预渲染右键『× 隐藏悬浮球』科幻毛玻璃胶囊（带顶部高光与霓虹描边）"""
        s, ss = self.s, 4
        mw, mh = self.menu_w, self.menu_h
        mask_big = Image.new("L", (mw * ss, mh * ss), 0)
        ImageDraw.Draw(mask_big).rounded_rectangle(
            (0, 0, mw * ss - 1, mh * ss - 1),
            radius=(mh * ss) // 2,
            fill=255,
        )
        mask = mask_big.resize((mw, mh), Image.LANCZOS)

        bg_rgb = (22, 28, 46) if hovered else (14, 18, 30)
        bg_alpha = 235 if hovered else 215
        pill = Image.new("RGBA", (mw, mh), bg_rgb + (0,))
        pill.putalpha(ImageChops.multiply(mask, Image.new("L", (mw, mh), bg_alpha)))

        # 顶部玻璃高光
        g = np.zeros((mh, 1), np.float32)
        ys = np.arange(mh)
        g[:, 0] = np.clip(1.0 - ys / max(1.0, mh * 0.55), 0.0, 1.0) ** 1.5 * 28.0
        sheen = Image.new("RGBA", (mw, mh), (255, 255, 255, 0))
        sheen.putalpha(
            ImageChops.multiply(
                Image.fromarray(np.broadcast_to(g, (mh, mw)).astype(np.uint8), "L"),
                mask,
            )
        )
        pill = Image.alpha_composite(pill, sheen)

        # 渐变描边
        bd_big = Image.new("L", (mw * ss, mh * ss), 0)
        ImageDraw.Draw(bd_big).rounded_rectangle(
            (0, 0, mw * ss - 1, mh * ss - 1),
            radius=(mh * ss) // 2,
            outline=220 if hovered else 95,
            width=max(ss, int(round(1.2 * s * ss))),
        )
        bd_mask = bd_big.resize((mw, mh), Image.LANCZOS)
        bd_grad = _diag_grad(mw, mh, REC_C1 if hovered else ACCENT_C1, REC_C2 if hovered else ACCENT_C2)
        bd_grad.putalpha(bd_mask)
        pill = Image.alpha_composite(pill, bd_grad)

        # 绘制关闭图标与发光文字
        d = ImageDraw.Draw(pill)
        ix = int(round(17 * s))
        iy = mh // 2
        r_x = int(round(4 * s))
        x_col = REC_C1 + (255,) if hovered else TEXT_SECONDARY + (235,)
        stroke = max(1, int(round(1.8 * s)))
        d.line((ix - r_x, iy - r_x, ix + r_x, iy + r_x), fill=x_col, width=stroke)
        d.line((ix - r_x, iy + r_x, ix + r_x, iy - r_x), fill=x_col, width=stroke)
        txt_col = TEXT_PRIMARY + (255,) if hovered else TEXT_SECONDARY + (245,)
        d.text(
            (ix + int(round(10 * s)), iy),
            "隐藏悬浮球",
            font=self.f_menu,
            fill=txt_col,
            anchor="lm",
        )
        return pill

    def _update_menu_side(self) -> str:
        """以当前显示器工作区中轴线为准：中轴线左侧向右展开，中轴线右侧向左展开"""
        orb_screen_cx = int(self.win_x + self.orb_cx)
        orb_screen_cy = int(self.win_y + self.orb_cy)
        work = self._get_work_area(POINT(orb_screen_cx, orb_screen_cy))
        mid_x = (work.left + work.right) // 2
        if orb_screen_cx <= mid_x:
            self._menu_side = "right"
            self.menu_rect = self.menu_rect_right
        else:
            self._menu_side = "left"
            self.menu_rect = self.menu_rect_left
        return self._menu_side

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

    def _read_audio_level(self) -> float:
        """读取实时麦克风声波能量（与 RecordingOverlay 同源联动）"""
        try:
            ov = getattr(self.app, "overlay", None)
            if ov is not None:
                raw = float(getattr(ov, "_level_in", 0.0))
                target = _clamp((raw - 0.008) / 0.11) ** 0.65
                k = 0.42 if target > self._lvl else 0.16
                self._lvl += (target - self._lvl) * k
                return self._lvl
        except Exception:
            pass
        return 0.0

    def toggle_recording(self) -> None:
        """左键轻点悬浮按钮：未录音 -> 立即开始录音；录音中 -> 立即停止并输出结果"""
        if not self.app or not hasattr(self.app, "shortcut"):
            return

        if self._is_recording():
            def _stop_all():
                if self._task.is_recording:
                    self._task.finish()
                for t in list(getattr(self.app.shortcut, "tasks", {}).values()):
                    if t.is_recording:
                        t.finish()

            self.app.shortcut._submit_action(_stop_all)
        else:
            def _start_floating():
                if not self._is_recording():
                    self._task.launch()

            self.app.shortcut._submit_action(_start_floating)

        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_APP_REDRAW, 0, 0)

    # ── 视觉渲染（与 RecordingOverlay 同源科幻流体毛玻璃质感）──────
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

        s, ss = self.s, 3
        B = self.orb_box
        c = (B * ss) / 2.0
        r_core = self.core_r * ss

        # 实时声波与呼吸强度
        lvl = self._read_audio_level() if recording else 0.0
        breath = 0.5 + 0.5 * math.sin(now * (4.2 if recording else 2.2))
        energy = max(lvl, 0.25 + 0.35 * breath) if recording else breath

        box_frame = Image.new("RGBA", (B, B), (0, 0, 0, 0))

        # 1. 外围柔和霓虹光晕（32*s 充裕留白，绝不触碰边界，纯彩 Alpha 无灰边）
        if recording:
            glow_k = 0.68 + 0.32 * energy
            box_frame.alpha_composite(_scale_alpha(self._glow_rec, glow_k))
        else:
            glow_k = (0.88 if self._hover_main else 0.52) + 0.12 * breath
            box_frame.alpha_composite(_scale_alpha(self._glow_idle, glow_k))

        # 2. 3D 流体毛玻璃圆舱底座（深空玻璃 + 顶部曲面反光 + 渐变流光细描边）
        if recording:
            box_frame.alpha_composite(self._glass_body_rec)
        elif self._hover_main:
            box_frame.alpha_composite(self._glass_body_hover)
        else:
            box_frame.alpha_composite(self._glass_body_idle)

        # 3. 舱内声波光晕环与双层流体扩散波纹（与 RecordingOverlay._render_orb 完全同源）
        halo_big = Image.new("RGBA", (B * ss, B * ss), (0, 0, 0, 0))
        hd = ImageDraw.Draw(halo_big)
        if recording:
            # 随音量起伏的内层贴身光环
            rr = r_core + (2.5 + 6.5 * energy) * s * ss
            hd.ellipse(
                (c - rr, c - rr, c + rr, c + rr),
                fill=REC_C1 + (int(42 + 95 * energy),),
            )
            # 周期性扩散波纹（限制在 R + 12*s 以内，远小于 B/2 = 61*s，绝对不会上下截断）
            for offset in (0.0, 0.5):
                ph = ((now / 1.6) + offset) % 1.0
                rip_r = r_core + ph * 13.5 * s * ss
                rip_col = _mix(REC_C1, REC_C2, ph)
                rip_a = int(175 * ((1.0 - ph) ** 1.35))
                if rip_a > 6:
                    hd.ellipse(
                        (c - rip_r, c - rip_r, c + rip_r, c + rip_r),
                        outline=rip_col + (rip_a,),
                        width=max(2, int(round(1.6 * s * ss))),
                    )
        else:
            # 待机态：柔和的呼吸内光环 + 悬停旋转流光弧
            rr = r_core + (3.2 + (1.8 if self._hover_main else 0.8) * breath) * s * ss
            hd.ellipse(
                (c - rr, c - rr, c + rr, c + rr),
                fill=ACCENT_C1 + (int((68 if self._hover_main else 42) + 20 * breath),),
            )
            if self._hover_main:
                arc_r = (self.orb_r - 3.5 * s) * ss
                start = (now * 140) % 360
                hd.arc(
                    (c - arc_r, c - arc_r, c + arc_r, c + arc_r),
                    start,
                    start + 95,
                    fill=ACCENT_C1 + (185,),
                    width=max(2, int(round(1.5 * s * ss))),
                )
                hd.arc(
                    (c - arc_r, c - arc_r, c + arc_r, c + arc_r),
                    start + 180,
                    start + 275,
                    fill=ACCENT_C2 + (165,),
                    width=max(2, int(round(1.5 * s * ss))),
                )

        box_frame.alpha_composite(halo_big.resize((B, B), Image.LANCZOS))

        # 4. 内部 3D 渐变核心球（录音时随声波轻微呼吸缩放，按压时微缩反馈）
        core = self._core_rec if recording else self._core_idle
        scale_k = 0.93 if self._pressed_main else (1.0 + 0.055 * energy if recording else 1.0)
        if abs(scale_k - 1.0) > 0.005:
            sz = int(round(B * scale_k)) | 1
            big_core = core.resize((sz, sz), Image.BICUBIC)
            off = (B - sz) // 2
            scaled = Image.new("RGBA", (B, B), (0, 0, 0, 0))
            if off < 0:
                scaled.alpha_composite(big_core.crop((-off, -off, -off + B, -off + B)), (0, 0))
            else:
                scaled.alpha_composite(big_core, (off, off))
            core = scaled
        box_frame.alpha_composite(core)

        # 5. 合成到总窗口画布，并在需要时绘制左右自适应右键胶囊菜单
        frame = Image.new("RGBA", (self.win_w, self.win_h), (0, 0, 0, 0))
        frame.alpha_composite(box_frame, (self.box_x0, self.box_y0))

        if menu_open:
            self._update_menu_side()
            mx0, my0, _, _ = self.menu_rect
            pill = self._menu_pill_hover if self._hover_menu else self._menu_pill_idle
            frame.alpha_composite(pill, (mx0, my0))

        return frame

    def _present(self) -> None:
        if not self.hwnd or not self._visible:
            return
        frame = self.render_preview_frame()
        w, h = frame.size

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
            # 使用与 RecordingOverlay._LayeredSurface.present 完全一致的 NumPy 精确四舍五入预乘 BGRA
            arr = np.asarray(frame, dtype=np.uint8)
            a = arr[..., 3:4].astype(np.uint16)
            buf = np.empty((h, w, 4), np.uint8)
            buf[..., :3] = (arr[..., 2::-1].astype(np.uint16) * a + 127) // 255
            buf[..., 3] = arr[..., 3]
            ctypes.memmove(ppv_bits.value, buf.ctypes.data, buf.nbytes)

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

        # 30fps 定时器：同步外部按键录音状态并驱动丝滑声波呼吸与波纹动画
        user32.SetTimer(self.hwnd, TIMER_PULSE, 33, None)

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
            # 当右键“隐藏悬浮球”胶囊展开时，检测用户在屏幕其他任意空白处的鼠标点击或 Esc 键，自动收起胶囊
            if self._menu_open and not self._lbutton_down and (time.perf_counter() - self._menu_opened_at) > 0.15:
                l_down = (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000) != 0
                r_down = (user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000) != 0
                m_down = (user32.GetAsyncKeyState(VK_MBUTTON) & 0x8000) != 0
                esc_down = (user32.GetAsyncKeyState(VK_ESCAPE) & 0x8000) != 0
                if esc_down:
                    self._menu_open = False
                    self._hover_menu = False
                    self._present()
                    return 0
                if l_down or r_down or m_down:
                    pt = POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    lx = int(pt.x - self.win_x)
                    ly = int(pt.y - self.win_y)
                    if not (l_down and self._hit_menu_pill(lx, ly)) and not self._hit_main_orb(lx, ly):
                        self._menu_open = False
                        self._hover_menu = False
                        self._present()
                        return 0
            rec = self._is_recording()
            if rec or self._hover_main or rec != self._last_recording_state:
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
            if self._hit_main_orb(lx, ly):
                self._update_menu_side()
                self._menu_open = not self._menu_open
                if self._menu_open:
                    self._menu_opened_at = time.perf_counter()
                self._present()
            elif self._menu_open:
                self._menu_open = False
                self._hover_menu = False
                self._present()
            return 0

        if msg == WM_DESTROY:
            user32.PostQuitMessage(0)
            return 0

        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)
