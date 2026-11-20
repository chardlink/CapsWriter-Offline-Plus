# coding: utf-8
"""现代深色毛玻璃托盘控制面板。

基于纯 Win32 分层窗口（WS_EX_LAYERED + UpdateLayeredWindow）与 Pillow RGBA 渲染，
彻底替代容易向左折叠重叠的 Windows 原生多级右键菜单。
"""

import ctypes
import math
import os
import threading
import time
from ctypes import wintypes
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

try:
    from . import logger
except Exception:
    import logging
    logger = logging.getLogger(__name__)


# ============================================================
# Win32 常量与结构体定义（64位安全）
# ============================================================

WM_DESTROY = 0x0002
WM_ACTIVATE = 0x0006
WM_SETCURSOR = 0x0020
WM_KEYDOWN = 0x0100
WM_TIMER = 0x0113
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN = 0x0201
WM_LBUTTONUP = 0x0202
WM_MOUSELEAVE = 0x02A3
WM_USER_TOGGLE = 0x0401
WM_USER_HIDE = 0x0402
WM_USER_QUIT = 0x0403

WS_POPUP = 0x80000000
WS_EX_TOPMOST = 0x00000008
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_LAYERED = 0x00080000

SW_HIDE = 0
SW_SHOW = 5
ULW_ALPHA = 2
AC_SRC_ALPHA = 1
TME_LEAVE = 0x00000002
IDC_ARROW = 32512
IDC_HAND = 32649
VK_ESCAPE = 0x1B
VK_LBUTTON = 0x01
VK_RBUTTON = 0x02
MONITOR_DEFAULTTONEAREST = 2
ERROR_CLASS_ALREADY_EXISTS = 1410

TIMER_ANIM = 1
TIMER_GUARD = 2

WNDPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM
)


class _WNDCLASSEXW(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.UINT),
        ('style', wintypes.UINT),
        ('lpfnWndProc', WNDPROC),
        ('cbClsExtra', ctypes.c_int),
        ('cbWndExtra', ctypes.c_int),
        ('hInstance', wintypes.HINSTANCE),
        ('hIcon', wintypes.HICON),
        ('hCursor', wintypes.HICON),
        ('hbrBackground', wintypes.HBRUSH),
        ('lpszMenuName', wintypes.LPCWSTR),
        ('lpszClassName', wintypes.LPCWSTR),
        ('hIconSm', wintypes.HICON),
    ]


class _TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('dwFlags', wintypes.DWORD),
        ('hwndTrack', wintypes.HWND),
        ('dwHoverTime', wintypes.DWORD),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ('biSize', wintypes.DWORD),
        ('biWidth', wintypes.LONG),
        ('biHeight', wintypes.LONG),
        ('biPlanes', wintypes.WORD),
        ('biBitCount', wintypes.WORD),
        ('biCompression', wintypes.DWORD),
        ('biSizeImage', wintypes.DWORD),
        ('biXPelsPerMeter', wintypes.LONG),
        ('biYPelsPerMeter', wintypes.LONG),
        ('biClrUsed', wintypes.DWORD),
        ('biClrImportant', wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [
        ('BlendOp', ctypes.c_ubyte),
        ('BlendFlags', ctypes.c_ubyte),
        ('SourceConstantAlpha', ctypes.c_ubyte),
        ('AlphaFormat', ctypes.c_ubyte),
    ]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ('cbSize', wintypes.DWORD),
        ('rcMonitor', wintypes.RECT),
        ('rcWork', wintypes.RECT),
        ('dwFlags', wintypes.DWORD),
    ]


# 每个模块使用独立的 WinDLL 实例，避免修改全局 ctypes.windll 单例的 argtypes 导致跨模块结构体类型冲突
user32 = ctypes.WinDLL('user32')
gdi32 = ctypes.WinDLL('gdi32')
kernel32 = ctypes.WinDLL('kernel32')
try:
    shcore = ctypes.WinDLL('shcore')
except Exception:
    shcore = None

kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
kernel32.GetModuleHandleW.restype = wintypes.HMODULE
kernel32.GetLastError.argtypes = []
kernel32.GetLastError.restype = wintypes.DWORD

user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
user32.RegisterClassExW.restype = wintypes.ATOM
user32.CreateWindowExW.argtypes = [
    wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p,
]
user32.CreateWindowExW.restype = wintypes.HWND
user32.DestroyWindow.argtypes = [wintypes.HWND]
user32.DestroyWindow.restype = wintypes.BOOL
user32.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.DefWindowProcW.restype = ctypes.c_ssize_t
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = wintypes.BOOL
user32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.TranslateMessage.restype = wintypes.BOOL
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
user32.DispatchMessageW.restype = ctypes.c_ssize_t
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.PostMessageW.restype = wintypes.BOOL
user32.PostQuitMessage.argtypes = [ctypes.c_int]
user32.PostQuitMessage.restype = None
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.ShowWindow.restype = wintypes.BOOL
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
user32.AllowSetForegroundWindow.restype = wintypes.BOOL
user32.SetTimer.argtypes = [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p]
user32.SetTimer.restype = ctypes.c_size_t
user32.KillTimer.argtypes = [wintypes.HWND, ctypes.c_size_t]
user32.KillTimer.restype = wintypes.BOOL
user32.LoadCursorW.argtypes = [wintypes.HINSTANCE, ctypes.c_void_p]
user32.LoadCursorW.restype = wintypes.HICON
user32.SetCursor.argtypes = [wintypes.HICON]
user32.SetCursor.restype = wintypes.HICON
user32.TrackMouseEvent.argtypes = [ctypes.POINTER(_TRACKMOUSEEVENT)]
user32.TrackMouseEvent.restype = wintypes.BOOL
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
user32.MonitorFromPoint.argtypes = [wintypes.POINT, wintypes.DWORD]
user32.MonitorFromPoint.restype = ctypes.c_void_p
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
user32.GetMonitorInfoW.restype = wintypes.BOOL
user32.GetDC.argtypes = [wintypes.HWND]
user32.GetDC.restype = ctypes.c_void_p
user32.ReleaseDC.argtypes = [wintypes.HWND, ctypes.c_void_p]
user32.ReleaseDC.restype = ctypes.c_int
user32.UpdateLayeredWindow.argtypes = [
    wintypes.HWND, ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD,
]
user32.UpdateLayeredWindow.restype = wintypes.BOOL

if hasattr(user32, 'GetDpiForWindow'):
    user32.GetDpiForWindow.argtypes = [wintypes.HWND]
    user32.GetDpiForWindow.restype = wintypes.UINT
if hasattr(user32, 'SetThreadDpiAwarenessContext'):
    user32.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user32.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
if shcore is not None and hasattr(shcore, 'GetDpiForMonitor'):
    shcore.GetDpiForMonitor.argtypes = [
        ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(wintypes.UINT), ctypes.POINTER(wintypes.UINT)
    ]
    shcore.GetDpiForMonitor.restype = ctypes.c_long

gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [
    ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint,
    ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p, wintypes.DWORD,
]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteObject.restype = wintypes.BOOL
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.restype = wintypes.BOOL


# ============================================================
# 视觉样式与辅助工具
# ============================================================

ACCENT_C1 = (56, 189, 248)     # 天青
ACCENT_C2 = (139, 92, 246)     # 极光紫
EMERALD = (52, 211, 153)       # 在线绿
INDIGO = (129, 140, 248)       # 忙碌靛蓝
ROSE = (251, 113, 133)         # 危险红
ROSE_TEXT = (253, 164, 175)
BODY_COLOR = (14, 18, 30, 224)
TEXT_PRIMARY = (246, 248, 255)
TEXT_SECONDARY = (203, 213, 225)
TEXT_MUTED = (148, 163, 184)

RETENTION_CHOICES = [
    (1, '1天'),
    (3, '3天'),
    (7, '7天'),
    (15, '15天'),
    (30, '30天'),
    (0, '永久'),
]


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _ease_out(t):
    t = _clamp(t)
    return 1.0 - (1.0 - t) ** 3


def _ease_in_out(t):
    t = _clamp(t)
    return t * t * (3.0 - 2.0 * t)


def _mix(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _hgrad(width, height, c1, c2, alpha=255):
    w, h = max(1, int(width)), max(1, int(height))
    t = np.linspace(0.0, 1.0, w, dtype=np.float32)[None, :, None]
    rgb = np.asarray(c1, np.float32) * (1.0 - t) + np.asarray(c2, np.float32) * t
    arr = np.empty((h, w, 4), np.uint8)
    arr[..., :3] = np.broadcast_to(rgb, (h, w, 3)).astype(np.uint8)
    arr[..., 3] = int(alpha)
    return Image.fromarray(arr, 'RGBA')


def _load_font(px, bold=False):
    fonts_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    names = ('msyhbd.ttc', 'msyh.ttc', 'simhei.ttf', 'segoeui.ttf') if bold else (
        'msyh.ttc', 'msyh.ttf', 'simhei.ttf', 'segoeui.ttf'
    )
    for name in names:
        try:
            return ImageFont.truetype(os.path.join(fonts_dir, name), max(8, int(round(px))))
        except OSError:
            continue
    return ImageFont.load_default()


def _draw_rr(size, radius, fill=None, outline=None, width=1, ss=4):
    """4倍超采样抗锯齿圆角矩形。"""
    w, h = max(1, int(size[0])), max(1, int(size[1]))
    r = max(1, int(round(radius * ss)))
    big = Image.new('RGBA', (w * ss, h * ss), (0, 0, 0, 0))
    d = ImageDraw.Draw(big)
    d.rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1),
        radius=r,
        fill=fill,
        outline=outline,
        width=max(1, int(round(width * ss))) if outline else 0,
    )
    return big.resize((w, h), Image.LANCZOS)


def _draw_grad_pill(width, height, radius, c1, c2, alpha=255, ss=4):
    """带水平渐变和微妙顶部高光的圆角胶囊。"""
    w, h = max(1, int(width)), max(1, int(height))
    mask = Image.new('L', (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=max(1, int(round(radius * ss))), fill=alpha
    )
    mask = mask.resize((w, h), Image.LANCZOS)
    grad = _hgrad(w, h, c1, c2, 255)
    grad.putalpha(mask)
    # 细微内高光描边
    rim = _draw_rr((w, h), radius, outline=(255, 255, 255, 58), width=1, ss=ss)
    return Image.alpha_composite(grad, rim)


def _make_vector_icon(name: str, size_px: int, color: Tuple[int, int, int]) -> Image.Image:
    """绘制 4x 超采样的抗锯齿矢量小图标。"""
    sz = max(10, int(round(size_px)))
    ss = 4
    S = sz * ss
    img = Image.new('RGBA', (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    col = color + (255,)
    lw = max(2, int(round(S * 0.088)))
    u = S / 24.0  # 24x24 网格坐标映射

    if name == 'mic':
        d.rounded_rectangle((9 * u, 3 * u, 15 * u, 13.5 * u), radius=3 * u, fill=col)
        d.arc((6.5 * u, 7.5 * u, 17.5 * u, 17 * u), 0, 180, fill=col, width=lw)
        d.line([(12 * u, 17 * u), (12 * u, 20.5 * u)], fill=col, width=lw)
        d.line([(8.5 * u, 20.5 * u), (15.5 * u, 20.5 * u)], fill=col, width=lw)
    elif name == 'server':
        d.rounded_rectangle((3.5 * u, 4 * u, 20.5 * u, 10.5 * u), radius=2 * u, outline=col, width=lw)
        d.rounded_rectangle((3.5 * u, 13.5 * u, 20.5 * u, 20 * u), radius=2 * u, outline=col, width=lw)
        d.ellipse((6.2 * u, 6.2 * u, 8.2 * u, 8.2 * u), fill=col)
        d.ellipse((6.2 * u, 15.7 * u, 8.2 * u, 17.7 * u), fill=col)
        d.line([(12 * u, 7.2 * u), (17.5 * u, 7.2 * u)], fill=col, width=lw)
        d.line([(12 * u, 16.7 * u), (17.5 * u, 16.7 * u)], fill=col, width=lw)
    elif name == 'eye':
        d.arc((2.5 * u, 5.5 * u, 21.5 * u, 18.5 * u), 195, 345, fill=col, width=lw)
        d.arc((2.5 * u, 5.5 * u, 21.5 * u, 18.5 * u), 15, 165, fill=col, width=lw)
        d.ellipse((9.3 * u, 9.3 * u, 14.7 * u, 14.7 * u), fill=col)
    elif name == 'copy':
        d.rounded_rectangle((8.5 * u, 8 * u, 19.5 * u, 20 * u), radius=2 * u, outline=col, width=lw)
        d.arc((4.5 * u, 4 * u, 8.5 * u, 8 * u), 180, 270, fill=col, width=lw)
        d.line([(6.5 * u, 4 * u), (15 * u, 4 * u)], fill=col, width=lw)
        d.line([(4.5 * u, 6 * u), (4.5 * u, 15.5 * u)], fill=col, width=lw)
    elif name == 'context':
        d.rounded_rectangle((4 * u, 4 * u, 20 * u, 20 * u), radius=2.5 * u, outline=col, width=lw)
        d.line([(7.5 * u, 9 * u), (16.5 * u, 9 * u)], fill=col, width=lw)
        d.line([(7.5 * u, 13 * u), (16.5 * u, 13 * u)], fill=col, width=lw)
        d.line([(7.5 * u, 17 * u), (13 * u, 17 * u)], fill=col, width=lw)
    elif name == 'hotword':
        pts = [
            (12 * u, 2.5 * u), (14.4 * u, 9.6 * u), (21.5 * u, 12 * u), (14.4 * u, 14.4 * u),
            (12 * u, 21.5 * u), (9.6 * u, 14.4 * u), (2.5 * u, 12 * u), (9.6 * u, 9.6 * u),
        ]
        d.polygon(pts, fill=col)
    elif name == 'eraser':
        d.line([
            (11.5 * u, 4.5 * u), (19.5 * u, 12.5 * u),
            (13 * u, 19 * u), (5 * u, 19 * u), (3.5 * u, 12.5 * u), (11.5 * u, 4.5 * u),
        ], fill=col, width=lw, joint='curve')
        d.line([(7.5 * u, 9.5 * u), (15 * u, 17 * u)], fill=col, width=lw)
        d.line([(12 * u, 19 * u), (20.5 * u, 19 * u)], fill=col, width=lw)
    elif name == 'audio':
        for idx, (x, h_half) in enumerate(((4.5, 3.5), (8.2, 6.5), (12, 8.5), (15.8, 5.5), (19.5, 3.0))):
            d.line([(x * u, (12 - h_half) * u), (x * u, (12 + h_half) * u)], fill=col, width=lw)
    elif name == 'refresh':
        d.arc((4.5 * u, 4.5 * u, 19.5 * u, 19.5 * u), 35, 315, fill=col, width=lw)
        d.polygon([(19.8 * u, 5 * u), (19.8 * u, 10.2 * u), (14.6 * u, 10.2 * u)], fill=col)
    elif name == 'folder':
        d.line([
            (3 * u, 6 * u), (9.5 * u, 6 * u), (11.5 * u, 8 * u),
            (21 * u, 8 * u), (21 * u, 19 * u), (3 * u, 19 * u), (3 * u, 6 * u),
        ], fill=col, width=lw, joint='curve')
    elif name == 'trash':
        d.line([(4.5 * u, 7 * u), (19.5 * u, 7 * u)], fill=col, width=lw)
        d.rounded_rectangle((9.5 * u, 4 * u, 14.5 * u, 7 * u), radius=1 * u, outline=col, width=lw)
        d.rounded_rectangle((6.5 * u, 7 * u, 17.5 * u, 20 * u), radius=1.8 * u, outline=col, width=lw)
        d.line([(10 * u, 10.5 * u), (10 * u, 16.8 * u)], fill=col, width=lw)
        d.line([(14 * u, 10.5 * u), (14 * u, 16.8 * u)], fill=col, width=lw)
    elif name == 'log':
        d.rounded_rectangle((4.5 * u, 3.5 * u, 19.5 * u, 20.5 * u), radius=2 * u, outline=col, width=lw)
        d.line([(8 * u, 9 * u), (16 * u, 9 * u)], fill=col, width=lw)
        d.line([(8 * u, 13 * u), (16 * u, 13 * u)], fill=col, width=lw)
        d.line([(8 * u, 17 * u), (12.5 * u, 17 * u)], fill=col, width=lw)
    elif name == 'bolt':
        d.polygon([
            (13.5 * u, 2.5 * u), (5.5 * u, 13.2 * u), (11.5 * u, 13.2 * u),
            (10.2 * u, 21.5 * u), (18.5 * u, 10.8 * u), (12.5 * u, 10.8 * u),
        ], fill=col)
    elif name == 'power':
        d.arc((5 * u, 5.5 * u, 19 * u, 19.5 * u), -55, 235, fill=col, width=lw)
        d.line([(12 * u, 3.2 * u), (12 * u, 11.5 * u)], fill=col, width=lw)
    return img.resize((sz, sz), Image.LANCZOS)


# ============================================================
# Win32 分层窗口 Surface
# ============================================================

class _LayeredSurface:
    """把 PIL RGBA 图像通过 UpdateLayeredWindow 提交为逐像素透明窗口。"""

    def __init__(self, hwnd, width: int, height: int):
        self.hwnd = hwnd
        self.width = int(width)
        self.height = int(height)
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = _BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        hdr.biWidth = self.width
        hdr.biHeight = -self.height  # 负高度 = 自上而下
        hdr.biPlanes = 1
        hdr.biBitCount = 32
        hdr.biCompression = 0
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(
            self.mem_dc, ctypes.byref(bmi), 0, ctypes.byref(self.bits), None, 0
        )
        self.old_bitmap = gdi32.SelectObject(self.mem_dc, self.bitmap)
        self.buffer = np.empty((self.height, self.width, 4), np.uint8)

    def present(self, img: Image.Image, x: int, y: int, alpha: int = 255):
        arr = np.asarray(img, dtype=np.uint8)
        a = arr[..., 3:4].astype(np.uint16)
        self.buffer[..., :3] = (arr[..., 2::-1].astype(np.uint16) * a + 127) // 255
        self.buffer[..., 3] = arr[..., 3]
        ctypes.memmove(self.bits, self.buffer.ctypes.data, self.buffer.nbytes)
        blend = _BLENDFUNCTION(0, 0, int(_clamp(alpha, 0, 255)), AC_SRC_ALPHA)
        user32.UpdateLayeredWindow(
            self.hwnd,
            self.screen_dc,
            ctypes.byref(wintypes.POINT(int(x), int(y))),
            ctypes.byref(wintypes.SIZE(self.width, self.height)),
            self.mem_dc,
            ctypes.byref(wintypes.POINT(0, 0)),
            0,
            ctypes.byref(blend),
            ULW_ALPHA,
        )

    def close(self):
        if self.mem_dc:
            if self.old_bitmap:
                gdi32.SelectObject(self.mem_dc, self.old_bitmap)
                self.old_bitmap = None
            if self.bitmap:
                gdi32.DeleteObject(self.bitmap)
                self.bitmap = None
            gdi32.DeleteDC(self.mem_dc)
            self.mem_dc = None
        if self.screen_dc:
            user32.ReleaseDC(None, self.screen_dc)
            self.screen_dc = None


# ============================================================
# 托盘控制面板
# ============================================================

class TrayPanel:
    """现代深色毛玻璃托盘悬浮控制面板。"""

    _CLASS_NAME = "CapsWriterTrayGlassPanel"

    def __init__(self, config: dict):
        self.config = config
        self._has_client = bool(config.get('client'))
        self._has_server = bool(config.get('server'))
        self._has_tabs = self._has_client and self._has_server

        self.hwnd: Optional[int] = None
        self._surface: Optional[_LayeredSurface] = None
        self._wndproc_ref = None
        self._lock = threading.Lock()
        self._pending_pos: Tuple[int, int] = (0, 0)
        self._ready = threading.Event()
        self._stopped = False

        # 交互与动画状态
        self.s = 1.0
        self.visible = False
        self._show_at = 0.0
        self._hide_from: Optional[float] = None
        self._hidden_at = 0.0
        self._was_foreground = False
        self._tracking_leave = False
        self._win_x = 0
        self._win_y = 0

        self._hover_id: Optional[str] = None
        self._press_id: Optional[str] = None
        self._hits: List[Tuple[str, Tuple[int, int, int, int], Optional[Callable], bool]] = []

        self._active_tab = 0 if self._has_client else 1
        self._tab_pos = float(self._active_tab)
        self._tab_from = self._tab_pos
        self._tab_at = 0.0

        self._autostart_val = self._read_autostart()
        self._switch_pos = 1.0 if self._autostart_val else 0.0
        self._switch_from = self._switch_pos
        self._switch_at = 0.0

        self._selected_days = self._read_retention_days()
        self._console_visible = self._read_console_visible()

        self._icon_cache: Dict[Tuple, Image.Image] = {}
        self._cached_frame: Optional[Image.Image] = None
        self._need_redraw = True
        self._cursor_hand = user32.LoadCursorW(None, ctypes.c_void_p(IDC_HAND))
        self._cursor_arrow = user32.LoadCursorW(None, ctypes.c_void_p(IDC_ARROW))

        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="TrayPanelThread")
        self._thread.start()
        self._ready.wait(timeout=2.0)

    # ---- 外部线程安全接口 ----

    def toggle_at(self, px: int, py: int) -> None:
        if not self.hwnd or self._stopped:
            return
        with self._lock:
            self._pending_pos = (int(px), int(py))
        user32.PostMessageW(self.hwnd, WM_USER_TOGGLE, 0, 0)

    def hide(self) -> None:
        if self.hwnd and not self._stopped:
            user32.PostMessageW(self.hwnd, WM_USER_HIDE, 0, 0)

    def stop(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        if self.hwnd:
            user32.PostMessageW(self.hwnd, WM_USER_QUIT, 0, 0)
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1.5)

    # ---- 读实时配置状态 ----

    def _read_autostart(self) -> bool:
        fn = self.config.get('is_autostart')
        try:
            return bool(fn()) if fn else False
        except Exception:
            return False

    def _read_console_visible(self) -> bool:
        fn = self.config.get('is_console_visible')
        try:
            return bool(fn()) if fn else False
        except Exception:
            return False

    def _read_retention_days(self) -> int:
        diary = (self.config.get('client') or {}).get('diary') or {}
        fn = diary.get('get_retention_days')
        try:
            return int(fn()) if fn else 3
        except Exception:
            return 3

    def _sync_live_state(self) -> None:
        self._autostart_val = self._read_autostart()
        self._switch_pos = 1.0 if self._autostart_val else 0.0
        self._switch_from = self._switch_pos
        self._switch_at = 0.0
        self._selected_days = self._read_retention_days()
        self._console_visible = self._read_console_visible()

    # ---- 布局几何与资源缓存 ----

    def _build_assets(self, scale: float) -> None:
        self.s = float(scale)
        s = self.s
        self._icon_cache.clear()
        self._need_redraw = True
        self._cached_frame = None

        self.f_title = _load_font(15 * s, bold=True)
        self.f_badge = _load_font(10.5 * s, bold=True)
        self.f_btn = _load_font(12 * s, bold=False)
        self.f_btn_bold = _load_font(12.5 * s, bold=True)
        self.f_small = _load_font(11 * s, bold=False)
        self.f_small_bold = _load_font(11 * s, bold=True)
        self.f_sub = _load_font(13 * s, bold=True)

        self.m = int(round(20 * s))
        self.card_w = int(round(320 * s))
        self.pad = int(round(16 * s))
        self.cw = self.card_w - 2 * self.pad

        self.header_h = int(round(34 * s))
        self.tab_h = int(round(34 * s)) if self._has_tabs else 0
        if self._has_tabs:
            self.content_h = int(round(220 * s))
        elif self._has_client:
            self.content_h = int(round(220 * s))
        else:
            rows = len((self.config.get('server') or {}).get('actions', []))
            self.content_h = int(round(68 * s + 10 * s + rows * 42 * s + max(0, rows - 1) * 8 * s))

        self.footer_h = int(round(38 * s + 8 * s + 34 * s))
        gap_tab = int(round(12 * s)) if self._has_tabs else 0
        self.card_h = (
            self.pad
            + self.header_h
            + int(round(12 * s))
            + self.tab_h
            + gap_tab
            + self.content_h
            + int(round(10 * s))
            + 1
            + int(round(10 * s))
            + self.footer_h
            + self.pad
        )
        self.win_w = self.card_w + 2 * self.m
        self.win_h = self.card_h + 2 * self.m

        radius = 16 * s
        size = (self.win_w, self.win_h)
        m = self.m

        # 外发光底图
        mask_big = Image.new('L', (self.win_w * 4, self.win_h * 4), 0)
        ImageDraw.Draw(mask_big).rounded_rectangle(
            (m * 4, m * 4, (m + self.card_w) * 4 - 1, (m + self.card_h) * 4 - 1),
            radius=int(round(radius * 4)),
            fill=255,
        )
        card_mask = mask_big.resize(size, Image.LANCZOS)

        glow = _hgrad(self.win_w, self.win_h, ACCENT_C1, ACCENT_C2, 255)
        spread = card_mask.filter(ImageFilter.GaussianBlur(11 * s)).point(
            lambda v: min(255, int(v * 0.72))
        )
        inner_cut = card_mask.point(lambda v: 255 - int(v * 0.85))
        glow.putalpha(ImageChops.multiply(spread, inner_cut))

        # 玻璃主体
        body = Image.new('RGBA', size, BODY_COLOR[:3] + (0,))
        body.putalpha(
            ImageChops.multiply(card_mask, Image.new('L', size, BODY_COLOR[3]))
        )
        # 顶部玻璃高光
        ys = np.arange(self.win_h, dtype=np.float32)
        sheen_col = np.clip(1.0 - (ys - m) / (80.0 * s), 0.0, 1.0) ** 1.8 * 24.0
        sheen_arr = np.broadcast_to(sheen_col[:, None], (self.win_h, self.win_w)).astype(np.uint8)
        sheen = Image.new('RGBA', size, (255, 255, 255, 0))
        sheen.putalpha(ImageChops.multiply(Image.fromarray(sheen_arr, 'L'), card_mask))
        body = Image.alpha_composite(body, sheen)

        # 细微轮廓描边
        border_big = Image.new('RGBA', (self.win_w * 4, self.win_h * 4), (0, 0, 0, 0))
        ImageDraw.Draw(border_big).rounded_rectangle(
            (m * 4, m * 4, (m + self.card_w) * 4 - 1, (m + self.card_h) * 4 - 1),
            radius=int(round(radius * 4)),
            outline=(255, 255, 255, 42),
            width=max(4, int(round(1.1 * s * 4))),
        )
        body = Image.alpha_composite(body, border_big.resize(size, Image.LANCZOS))
        self._bg_base = Image.alpha_composite(glow, body)

    def _icon(self, name: str, size_px: float, color: Tuple[int, int, int]) -> Image.Image:
        sz = max(10, int(round(size_px)))
        key = (name, sz, color)
        ico = self._icon_cache.get(key)
        if ico is None:
            ico = _make_vector_icon(name, sz, color)
            self._icon_cache[key] = ico
        return ico

    # ---- Win32 窗口与消息循环 ----

    def _wnd_proc(self, hwnd, msg, wparam, lparam):
        try:
            if msg == WM_USER_TOGGLE:
                with self._lock:
                    px, py = self._pending_pos
                self._on_toggle(px, py)
                return 0
            if msg == WM_USER_HIDE:
                self._begin_hide()
                return 0
            if msg == WM_USER_QUIT:
                user32.KillTimer(hwnd, TIMER_ANIM)
                user32.KillTimer(hwnd, TIMER_GUARD)
                user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_MOUSEMOVE:
                if not self._tracking_leave:
                    tme = _TRACKMOUSEEVENT(
                        ctypes.sizeof(_TRACKMOUSEEVENT), TME_LEAVE, hwnd, 0xFFFFFFFF
                    )
                    user32.TrackMouseEvent(ctypes.byref(tme))
                    self._tracking_leave = True
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                self._on_mouse_move(x, y)
                return 0
            if msg == WM_MOUSELEAVE:
                self._tracking_leave = False
                if self._hover_id is not None or self._press_id is not None:
                    self._hover_id = None
                    self._press_id = None
                    self._need_redraw = True
                    self._present(time.monotonic())
                return 0
            if msg == WM_SETCURSOR:
                user32.SetCursor(self._cursor_hand if self._hover_id else self._cursor_arrow)
                return 1
            if msg == WM_LBUTTONDOWN:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                hit = self._hit_test(x, y)
                self._press_id = hit[0] if hit else None
                self._need_redraw = True
                self._present(time.monotonic())
                return 0
            if msg == WM_LBUTTONUP:
                x = ctypes.c_short(lparam & 0xFFFF).value
                y = ctypes.c_short((lparam >> 16) & 0xFFFF).value
                press_id = self._press_id
                self._press_id = None
                hit = self._hit_test(x, y)
                if hit and hit[0] == press_id:
                    self._invoke_hit(hit)
                else:
                    self._need_redraw = True
                    self._present(time.monotonic())
                return 0
            if msg == WM_KEYDOWN:
                if wparam == VK_ESCAPE:
                    self._begin_hide()
                return 0
            if msg == WM_ACTIVATE:
                if (wparam & 0xFFFF) == 0:  # WA_INACTIVE
                    if self.visible and self._hide_from is None and (time.monotonic() - self._show_at > 0.16):
                        self._begin_hide()
                return 0
            if msg == WM_TIMER:
                if wparam == TIMER_ANIM:
                    self._on_anim_timer()
                elif wparam == TIMER_GUARD:
                    self._on_guard_timer()
                return 0
            if msg == WM_DESTROY:
                user32.PostQuitMessage(0)
                return 0
        except Exception:
            logger.exception("TrayPanel 消息处理异常")
        return user32.DefWindowProcW(hwnd, msg, wparam, lparam)

    def _run_loop(self) -> None:
        try:
            if hasattr(user32, 'SetThreadDpiAwarenessContext'):
                user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        except Exception:
            pass

        hinst = kernel32.GetModuleHandleW(None)
        self._wndproc_ref = WNDPROC(self._wnd_proc)

        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.lpfnWndProc = self._wndproc_ref
        wc.hInstance = hinst
        wc.hCursor = self._cursor_arrow
        wc.lpszClassName = self._CLASS_NAME

        atom = user32.RegisterClassExW(ctypes.byref(wc))
        if not atom and kernel32.GetLastError() != ERROR_CLASS_ALREADY_EXISTS:
            logger.error(f"RegisterClassExW 失败: {kernel32.GetLastError()}")
            self._ready.set()
            return

        self.hwnd = user32.CreateWindowExW(
            WS_EX_LAYERED | WS_EX_TOPMOST | WS_EX_TOOLWINDOW,
            self._CLASS_NAME,
            "CapsWriter Tray Panel",
            WS_POPUP,
            0, 0, 100, 100,
            None, None, hinst, None,
        )
        if not self.hwnd:
            logger.error(f"CreateWindowExW 失败: {kernel32.GetLastError()}")
            self._ready.set()
            return

        dpi = user32.GetDpiForWindow(self.hwnd) if hasattr(user32, 'GetDpiForWindow') else 96
        self._build_assets((dpi or 96) / 96.0)
        self._surface = _LayeredSurface(self.hwnd, self.win_w, self.win_h)
        self._ready.set()

        msg = wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            user32.TranslateMessage(ctypes.byref(msg))
            user32.DispatchMessageW(ctypes.byref(msg))

        if self._surface:
            self._surface.close()
            self._surface = None
        self.hwnd = None

    # ---- 打开 / 隐藏 / 定时动画 ----

    def _query_monitor_and_dpi(self, px: int, py: int) -> Tuple[wintypes.RECT, float]:
        work = wintypes.RECT(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        dpi = 96
        try:
            mon = user32.MonitorFromPoint(wintypes.POINT(int(px), int(py)), MONITOR_DEFAULTTONEAREST)
            if mon:
                info = _MONITORINFO()
                info.cbSize = ctypes.sizeof(_MONITORINFO)
                if user32.GetMonitorInfoW(mon, ctypes.byref(info)):
                    work = info.rcWork
                if shcore is not None and hasattr(shcore, 'GetDpiForMonitor'):
                    dx, dy = wintypes.UINT(96), wintypes.UINT(96)
                    if shcore.GetDpiForMonitor(mon, 0, ctypes.byref(dx), ctypes.byref(dy)) == 0 and dx.value:
                        dpi = dx.value
                elif hasattr(user32, 'GetDpiForWindow') and self.hwnd:
                    dpi = user32.GetDpiForWindow(self.hwnd) or 96
        except Exception:
            pass
        return work, (dpi or 96) / 96.0

    def _on_toggle(self, px: int, py: int) -> None:
        now = time.monotonic()
        if self.visible and self._hide_from is None:
            self._begin_hide()
            return
        if now - self._hidden_at < 0.28:
            return

        work, new_scale = self._query_monitor_and_dpi(px, py)
        if abs(new_scale - self.s) > 0.01 or self._surface is None:
            if self._surface:
                self._surface.close()
            self._build_assets(new_scale)
            self._surface = _LayeredSurface(self.hwnd, self.win_w, self.win_h)

        s = self.s
        pad_edge = int(round(8 * s))
        card_x = int(max(work.left + pad_edge, min(px - self.card_w // 2, work.right - self.card_w - pad_edge)))
        card_y = int(max(work.top + pad_edge, min(py - self.card_h - int(round(10 * s)), work.bottom - self.card_h - pad_edge)))
        self._win_x = card_x - self.m
        self._win_y = card_y - self.m

        self._sync_live_state()
        self._hover_id = None
        self._press_id = None
        self._hide_from = None
        self._show_at = now
        self._was_foreground = False
        self._need_redraw = True
        self.visible = True

        self._present(now)
        user32.ShowWindow(self.hwnd, SW_SHOW)
        user32.SetForegroundWindow(self.hwnd)
        user32.SetTimer(self.hwnd, TIMER_ANIM, 16, None)
        user32.SetTimer(self.hwnd, TIMER_GUARD, 80, None)

    def _begin_hide(self) -> None:
        if not self.visible or self._hide_from is not None:
            return
        self._hide_from = time.monotonic()
        self._hidden_at = self._hide_from
        user32.SetTimer(self.hwnd, TIMER_ANIM, 16, None)

    def _finish_hide(self) -> None:
        self.visible = False
        self._hide_from = None
        self._hidden_at = time.monotonic()
        self._hover_id = None
        self._press_id = None
        if self.hwnd:
            user32.KillTimer(self.hwnd, TIMER_ANIM)
            user32.KillTimer(self.hwnd, TIMER_GUARD)
            user32.ShowWindow(self.hwnd, SW_HIDE)

    def _on_anim_timer(self) -> None:
        now = time.monotonic()
        if not self.visible:
            user32.KillTimer(self.hwnd, TIMER_ANIM)
            return

        if self._hide_from is not None and (now - self._hide_from) >= 0.16:
            self._finish_hide()
            return

        animating = False
        if self._hide_from is not None or (now - self._show_at) < 0.22:
            animating = True
        if self._tab_at and (now - self._tab_at) < 0.20:
            self._need_redraw = True
            animating = True
        elif self._tab_at:
            self._tab_pos = float(self._active_tab)
            self._tab_at = 0.0
            self._need_redraw = True
        if self._switch_at and (now - self._switch_at) < 0.18:
            self._need_redraw = True
            animating = True
        elif self._switch_at:
            self._switch_pos = 1.0 if self._autostart_val else 0.0
            self._switch_at = 0.0
            self._need_redraw = True

        # 服务端忙碌脉冲动画
        if self._active_tab == 1 and self._has_server:
            get_st = (self.config.get('server') or {}).get('get_status')
            if get_st:
                try:
                    if get_st()[2]:
                        self._need_redraw = True
                        animating = True
                except Exception:
                    pass

        self._present(now)
        if not animating:
            user32.KillTimer(self.hwnd, TIMER_ANIM)

    def _on_guard_timer(self) -> None:
        if not self.visible or self._hide_from is not None or not self.hwnd:
            return
        now = time.monotonic()
        if now - self._show_at < 0.20:
            return
        fg = user32.GetForegroundWindow()
        if fg == self.hwnd:
            self._was_foreground = True
        elif self._was_foreground:
            self._begin_hide()
            return

        # 兜底检测：鼠标在面板外部按下左键或右键时立即收起
        if (user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000) or (user32.GetAsyncKeyState(VK_RBUTTON) & 0x8000):
            pt = wintypes.POINT()
            if user32.GetCursorPos(ctypes.byref(pt)):
                cx0 = self._win_x + self.m
                cy0 = self._win_y + self.m
                if not (cx0 <= pt.x <= cx0 + self.card_w and cy0 <= pt.y <= cy0 + self.card_h):
                    self._begin_hide()

    # ---- 鼠标命中与事件分发 ----

    def _hit_test(self, x: int, y: int):
        for item in reversed(self._hits):
            x0, y0, x1, y1 = item[1]
            if x0 <= x <= x1 and y0 <= y <= y1:
                return item
        return None

    def _on_mouse_move(self, x: int, y: int) -> None:
        hit = self._hit_test(x, y)
        new_hover = hit[0] if hit else None
        if new_hover != self._hover_id:
            self._hover_id = new_hover
            self._need_redraw = True
            self._present(time.monotonic())

    def _invoke_hit(self, hit) -> None:
        hit_id, _, action, close_on_click = hit
        now = time.monotonic()
        if hit_id == 'tab_0':
            if self._active_tab != 0:
                self._tab_from = self._current_tab_pos(now)
                self._active_tab = 0
                self._tab_at = now
                self._need_redraw = True
                user32.SetTimer(self.hwnd, TIMER_ANIM, 16, None)
                self._present(now)
            return
        if hit_id == 'tab_1':
            if self._active_tab != 1:
                self._tab_from = self._current_tab_pos(now)
                self._active_tab = 1
                self._tab_at = now
                self._need_redraw = True
                user32.SetTimer(self.hwnd, TIMER_ANIM, 16, None)
                self._present(now)
            return
        if hit_id == 'autostart':
            self._switch_from = self._current_switch_pos(now)
            self._autostart_val = not self._autostart_val
            self._switch_at = now
            self._need_redraw = True
            user32.SetTimer(self.hwnd, TIMER_ANIM, 16, None)
            self._present(now)
            if action:
                threading.Thread(target=action, daemon=True).start()
            return
        if hit_id.startswith('ret_'):
            days = int(hit_id[4:])
            self._selected_days = days
            self._need_redraw = True
            self._present(now)
            if action:
                threading.Thread(target=action, args=(days,), daemon=True).start()
            return
        if hit_id == 'toggle_console':
            self._console_visible = not self._console_visible
            self._need_redraw = True
            self._present(now)

        if close_on_click:
            self._begin_hide()
        if action:
            threading.Thread(target=action, daemon=True).start()

    def _current_tab_pos(self, now: float) -> float:
        if not self._tab_at:
            return float(self._active_tab)
        t = _ease_in_out((now - self._tab_at) / 0.18)
        return self._tab_from + (float(self._active_tab) - self._tab_from) * t

    def _current_switch_pos(self, now: float) -> float:
        target = 1.0 if self._autostart_val else 0.0
        if not self._switch_at:
            return target
        t = _ease_in_out((now - self._switch_at) / 0.16)
        return self._switch_from + (target - self._switch_from) * t

    # ---- 渲染 ----

    def _present(self, now: float) -> None:
        if not self._surface:
            return
        if self._need_redraw or self._cached_frame is None:
            self._cached_frame = self.render_frame(now)
            self._need_redraw = False
        if self._hide_from is not None:
            p = 1.0 - _ease_out((now - self._hide_from) / 0.15)
            dy = (1.0 - p) * 10.0 * self.s
        else:
            p = _ease_out((now - self._show_at) / 0.18)
            dy = (1.0 - p) * 12.0 * self.s
        self._surface.present(self._cached_frame, self._win_x, int(round(self._win_y + dy)), int(255 * p))

    def render_frame(self, now: Optional[float] = None) -> Image.Image:
        """渲染一帧完整面板 RGBA 图像（也可被离线预览脚本直接调用）。"""
        if now is None:
            now = time.monotonic()
        s = self.s
        frame = self._bg_base.copy()
        d = ImageDraw.Draw(frame)
        hits: List[Tuple[str, Tuple[int, int, int, int], Optional[Callable], bool]] = []

        x0 = self.m + self.pad
        y = self.m + self.pad
        cw = self.cw

        # 1. Header
        y = self._render_header(frame, d, x0, y, cw, hits)
        y += int(round(12 * s))

        # 2. Segmented Tab Bar
        if self._has_tabs:
            y = self._render_tabs(frame, d, x0, y, cw, now, hits)
            y += int(round(12 * s))

        # 3. Content Area
        if self._active_tab == 0 and self._has_client:
            self._render_client_tab(frame, d, x0, y, cw, hits)
        elif self._has_server:
            self._render_server_tab(frame, d, x0, y, cw, now, hits)
        y += self.content_h + int(round(10 * s))

        # 4. Divider
        d.line([(x0, y), (x0 + cw, y)], fill=(255, 255, 255, 24), width=1)
        y += 1 + int(round(10 * s))

        # 5. Footer
        self._render_footer(frame, d, x0, y, cw, now, hits)
        self._hits = hits
        return frame

    def _draw_tile(self, frame: Image.Image, hit_id: str, rect: Tuple[int, int, int, int],
                   radius: float, danger: bool = False, selected: bool = False) -> None:
        x0, y0, x1, y1 = rect
        w, h = x1 - x0, y1 - y0
        if selected:
            pill = _draw_grad_pill(w, h, radius, ACCENT_C1, ACCENT_C2, 245)
            frame.alpha_composite(pill, (x0, y0))
            return

        hovered = self._hover_id == hit_id
        pressed = self._press_id == hit_id
        if danger:
            if pressed:
                fill, outline = (251, 113, 133, 72), (251, 113, 133, 160)
            elif hovered:
                fill, outline = (251, 113, 133, 48), (251, 113, 133, 130)
            else:
                fill, outline = (251, 113, 133, 22), (251, 113, 133, 55)
        else:
            if pressed:
                fill, outline = (56, 189, 248, 56), (56, 189, 248, 150)
            elif hovered:
                fill, outline = (56, 189, 248, 34), (56, 189, 248, 110)
            else:
                fill, outline = (255, 255, 255, 13), (255, 255, 255, 24)
        tile = _draw_rr((w, h), radius, fill=fill, outline=outline, width=1)
        frame.alpha_composite(tile, (x0, y0))

    def _render_header(self, frame: Image.Image, d: ImageDraw.Draw, x0: int, y: int, cw: int, hits: list) -> int:
        s = self.s
        cy = y + self.header_h // 2

        # 绿色在线指示灯 + 标题 + 标签
        dot_x = x0 + int(round(6 * s))
        dr = max(3, int(round(4 * s)))
        hr = int(round(8 * s))
        d.ellipse((dot_x - hr, cy - hr, dot_x + hr, cy + hr), fill=EMERALD + (55,))
        d.ellipse((dot_x - dr, cy - dr, dot_x + dr, cy + dr), fill=EMERALD + (255,))

        title = self.config.get('title', 'CapsWriter')
        tx = dot_x + int(round(13 * s))
        d.text((tx, cy), title, font=self.f_title, fill=TEXT_PRIMARY + (255,), anchor='lm')

        badge = self.config.get('badge')
        if badge:
            tw = int(math.ceil(self.f_title.getlength(title)))
            bw = int(math.ceil(self.f_badge.getlength(badge) + 14 * s))
            bh = int(round(20 * s))
            bx = tx + tw + int(round(8 * s))
            by = cy - bh // 2
            bpill = _draw_rr(
                (bw, bh), bh / 2, fill=(56, 189, 248, 34), outline=(56, 189, 248, 82), width=1
            )
            frame.alpha_composite(bpill, (bx, by))
            d.text((bx + bw // 2, cy), badge, font=self.f_badge, fill=(125, 211, 252, 255), anchor='mm')

        # 右侧 显示/隐藏窗口按钮
        lbl = '隐藏窗口' if self._console_visible else '显示窗口'
        ico_sz = int(round(13 * s))
        lbl_w = self.f_small.getlength(lbl)
        btn_w = int(round(ico_sz + 5 * s + lbl_w + 18 * s))
        btn_h = int(round(26 * s))
        bx0 = x0 + cw - btn_w
        by0 = cy - btn_h // 2
        rect = (bx0, by0, bx0 + btn_w, by0 + btn_h)
        self._draw_tile(frame, 'toggle_console', rect, btn_h / 2)
        ico = self._icon('eye', ico_sz, TEXT_SECONDARY)
        frame.alpha_composite(ico, (bx0 + int(round(9 * s)), cy - ico_sz // 2))
        d.text(
            (bx0 + int(round(9 * s)) + ico_sz + int(round(5 * s)), cy),
            lbl, font=self.f_small, fill=TEXT_SECONDARY + (255,), anchor='lm',
        )
        hits.append(('toggle_console', rect, self.config.get('on_toggle_console'), True))
        return y + self.header_h

    def _render_tabs(self, frame: Image.Image, d: ImageDraw.Draw, x0: int, y: int, cw: int, now: float, hits: list) -> int:
        s = self.s
        h = self.tab_h
        track = _draw_rr(
            (cw, h), 10 * s, fill=(255, 255, 255, 11), outline=(255, 255, 255, 20), width=1
        )
        frame.alpha_composite(track, (x0, y))

        pad_in = int(round(3 * s))
        seg_w = (cw - 2 * pad_in) // 2
        pill_h = h - 2 * pad_in
        pos = self._current_tab_pos(now)
        px = int(round(x0 + pad_in + pos * seg_w))
        pill = _draw_grad_pill(seg_w, pill_h, 8 * s, ACCENT_C1, ACCENT_C2, 240)
        frame.alpha_composite(pill, (px, y + pad_in))

        tabs = [
            ('tab_0', 'mic', '客户端', 0),
            ('tab_1', 'server', '服务端', 1),
        ]
        ico_sz = int(round(14 * s))
        cy = y + h // 2
        for idx, (tid, iname, label, tval) in enumerate(tabs):
            sx = x0 + pad_in + idx * seg_w
            rect = (sx, y + pad_in, sx + seg_w, y + pad_in + pill_h)
            active_amt = 1.0 - min(1.0, abs(pos - tval))
            hovered = self._hover_id == tid
            col = _mix(TEXT_MUTED if not hovered else TEXT_PRIMARY, (255, 255, 255), active_amt)
            font = self.f_btn_bold if active_amt > 0.5 else self.f_btn
            lw = font.getlength(label)
            tot_w = ico_sz + 6 * s + lw
            ix = int(round(sx + (seg_w - tot_w) / 2))
            frame.alpha_composite(self._icon(iname, ico_sz, col), (ix, cy - ico_sz // 2))
            d.text((ix + ico_sz + int(round(6 * s)), cy), label, font=font, fill=col + (255,), anchor='lm')
            hits.append((tid, rect, None, False))

        return y + h

    def _render_client_tab(self, frame: Image.Image, d: ImageDraw.Draw, x0: int, y: int, cw: int, hits: list) -> None:
        s = self.s
        client_cfg = self.config.get('client') or {}
        actions = client_cfg.get('actions', [])

        # 2列 x 3行 快捷操作网格
        gap_x = int(round(8 * s))
        gap_y = int(round(7 * s))
        tile_w = (cw - gap_x) // 2
        tile_h = int(round(33 * s))
        ico_sz = int(round(14 * s))

        for i, (aid, iname, label, cb) in enumerate(actions[:6]):
            col_i, row_i = i % 2, i // 2
            tx = x0 + col_i * (tile_w + gap_x)
            ty = y + row_i * (tile_h + gap_y)
            rect = (tx, ty, tx + tile_w, ty + tile_h)
            hit_id = f'act_{aid}'
            self._draw_tile(frame, hit_id, rect, 9 * s)
            hovered = self._hover_id == hit_id
            ico_col = ACCENT_C1 if hovered else TEXT_SECONDARY
            txt_col = TEXT_PRIMARY if hovered else TEXT_SECONDARY
            cy = ty + tile_h // 2
            frame.alpha_composite(self._icon(iname, ico_sz, ico_col), (tx + int(round(11 * s)), cy - ico_sz // 2))
            d.text(
                (tx + int(round(11 * s)) + ico_sz + int(round(7 * s)), cy),
                label, font=self.f_btn, fill=txt_col + (255,), anchor='lm',
            )
            hits.append((hit_id, rect, cb, True))

        grid_h = 3 * tile_h + 2 * gap_y
        dy = y + grid_h + int(round(9 * s))
        dh = int(round(98 * s))

        # 日记与录音子卡片
        diary = client_cfg.get('diary') or {}
        subcard = _draw_rr(
            (cw, dh), 12 * s, fill=(255, 255, 255, 11), outline=(255, 255, 255, 24), width=1
        )
        frame.alpha_composite(subcard, (x0, dy))

        pad_in = int(round(12 * s))
        rx0 = x0 + pad_in
        rw = cw - 2 * pad_in
        top_cy = dy + int(round(21 * s))
        f_ico_sz = int(round(14 * s))
        frame.alpha_composite(self._icon('folder', f_ico_sz, ACCENT_C1), (rx0, top_cy - f_ico_sz // 2))
        d.text(
            (rx0 + f_ico_sz + int(round(6 * s)), top_cy),
            '日记与录音', font=self.f_sub, fill=TEXT_PRIMARY + (255,), anchor='lm',
        )

        # 右侧两个胶囊按钮：打开目录 / 立即清理
        mini_h = int(round(23 * s))
        mini_gap = int(round(6 * s))
        btn_specs = [
            ('diary_clean', 'trash', '立即清理', diary.get('on_clean_now')),
            ('diary_open', 'folder', '打开目录', diary.get('on_open')),
        ]
        cur_rx = x0 + cw - pad_in
        m_ico_sz = int(round(11 * s))
        for bid, b_ico, blbl, bcb in btn_specs:
            bw = int(round(m_ico_sz + 4 * s + self.f_small.getlength(blbl) + 14 * s))
            bx0 = cur_rx - bw
            by0 = top_cy - mini_h // 2
            brect = (bx0, by0, cur_rx, by0 + mini_h)
            self._draw_tile(frame, bid, brect, mini_h / 2)
            hovered = self._hover_id == bid
            tcol = TEXT_PRIMARY if hovered else TEXT_SECONDARY
            frame.alpha_composite(self._icon(b_ico, m_ico_sz, ACCENT_C1 if hovered else TEXT_MUTED),
                                  (bx0 + int(round(7 * s)), top_cy - m_ico_sz // 2))
            d.text(
                (bx0 + int(round(7 * s)) + m_ico_sz + int(round(4 * s)), top_cy),
                blbl, font=self.f_small, fill=tcol + (255,), anchor='lm',
            )
            hits.append((bid, brect, bcb, True))
            cur_rx = bx0 - mini_gap

        # 中间提示标签
        label_y = dy + int(round(44 * s))
        d.text((rx0, label_y), '自动清理保留天数', font=self.f_small, fill=TEXT_MUTED + (255,), anchor='lm')

        # 底部一排 6 个天数选择胶囊
        py0 = dy + int(round(58 * s))
        ph = int(round(27 * s))
        pgap = int(round(5 * s))
        pw_total = rw - pgap * (len(RETENTION_CHOICES) - 1)
        pw = pw_total // len(RETENTION_CHOICES)
        p_cy = py0 + ph // 2
        set_ret = diary.get('set_retention_days')

        for idx, (days_val, dlabel) in enumerate(RETENTION_CHOICES):
            px0 = rx0 + idx * (pw + pgap)
            px1 = px0 + pw if idx < len(RETENTION_CHOICES) - 1 else (rx0 + rw)
            prect = (px0, py0, px1, py0 + ph)
            hid = f'ret_{days_val}'
            active = self._selected_days == days_val
            self._draw_tile(frame, hid, prect, ph / 2, selected=active)
            font = self.f_small_bold if active else self.f_small
            tcol = (255, 255, 255) if active else (TEXT_PRIMARY if self._hover_id == hid else TEXT_SECONDARY)
            d.text(((px0 + px1) // 2, p_cy), dlabel, font=font, fill=tcol + (255,), anchor='mm')
            hits.append((hid, prect, set_ret, False))

    def _render_server_tab(self, frame: Image.Image, d: ImageDraw.Draw, x0: int, y: int, cw: int, now: float, hits: list) -> None:
        s = self.s
        srv_cfg = self.config.get('server') or {}
        get_status = srv_cfg.get('get_status')
        st_title, st_sub, busy = ('服务端运行中', '本地离线语音引擎', False)
        if get_status:
            try:
                st_title, st_sub, busy = get_status()
            except Exception:
                pass

        # 状态子卡片
        sh = int(round(68 * s))
        scard = _draw_rr(
            (cw, sh), 12 * s, fill=(255, 255, 255, 11), outline=(255, 255, 255, 24), width=1
        )
        frame.alpha_composite(scard, (x0, y))

        dot_col = INDIGO if busy else EMERALD
        pulse = (0.55 + 0.45 * math.sin(now * 6.0)) if busy else 1.0
        dot_x = x0 + int(round(20 * s))
        dot_y = y + int(round(24 * s))
        hr = int(round((9 + (3 * pulse if busy else 0)) * s))
        dr = max(4, int(round(4.5 * s)))
        d.ellipse((dot_x - hr, dot_y - hr, dot_x + hr, dot_y + hr), fill=dot_col + (int(65 * pulse),))
        d.ellipse((dot_x - dr, dot_y - dr, dot_x + dr, dot_y + dr), fill=dot_col + (255,))

        d.text(
            (dot_x + int(round(15 * s)), dot_y),
            st_title, font=self.f_sub, fill=TEXT_PRIMARY + (255,), anchor='lm',
        )
        d.text(
            (dot_x + int(round(15 * s)), y + int(round(47 * s))),
            st_sub, font=self.f_small, fill=TEXT_MUTED + (255,), anchor='lm',
        )

        # 服务端操作行列表
        ry = y + sh + int(round(10 * s))
        rh = int(round(42 * s))
        rgap = int(round(8 * s))
        ico_sz = int(round(15 * s))
        for aid, iname, label, hint, cb in srv_cfg.get('actions', []):
            rect = (x0, ry, x0 + cw, ry + rh)
            hid = f'srv_{aid}'
            self._draw_tile(frame, hid, rect, 10 * s)
            hovered = self._hover_id == hid
            cy = ry + rh // 2
            frame.alpha_composite(
                self._icon(iname, ico_sz, ACCENT_C1 if hovered else TEXT_SECONDARY),
                (x0 + int(round(13 * s)), cy - ico_sz // 2),
            )
            d.text(
                (x0 + int(round(13 * s)) + ico_sz + int(round(9 * s)), cy),
                label, font=self.f_btn, fill=(TEXT_PRIMARY if hovered else TEXT_SECONDARY) + (255,), anchor='lm',
            )
            if hint:
                d.text(
                    (x0 + cw - int(round(24 * s)), cy),
                    hint, font=self.f_small, fill=TEXT_MUTED + (220,), anchor='rm',
                )
            d.text(
                (x0 + cw - int(round(12 * s)), cy - int(round(1 * s))),
                '›', font=self.f_sub, fill=(ACCENT_C1 if hovered else TEXT_MUTED) + (255,), anchor='rm',
            )
            hits.append((hid, rect, cb, True))
            ry += rh + rgap

    def _render_footer(self, frame: Image.Image, d: ImageDraw.Draw, x0: int, y: int, cw: int, now: float, hits: list) -> None:
        s = self.s

        # 1. 开机自启动开关行
        ah = int(round(38 * s))
        arect = (x0, y, x0 + cw, y + ah)
        self._draw_tile(frame, 'autostart', arect, 10 * s)
        cy = y + ah // 2
        ico_sz = int(round(14 * s))
        hovered = self._hover_id == 'autostart'
        sw_pos = self._current_switch_pos(now)
        frame.alpha_composite(
            self._icon('bolt', ico_sz, ACCENT_C1 if sw_pos > 0.5 or hovered else TEXT_SECONDARY),
            (x0 + int(round(12 * s)), cy - ico_sz // 2),
        )
        d.text(
            (x0 + int(round(12 * s)) + ico_sz + int(round(8 * s)), cy),
            '开机自启动', font=self.f_btn, fill=(TEXT_PRIMARY if hovered else TEXT_SECONDARY) + (255,), anchor='lm',
        )

        # 滑动胶囊开关
        sw_w = int(round(38 * s))
        sw_h = int(round(20 * s))
        sw_x = x0 + cw - sw_w - int(round(11 * s))
        sw_y = cy - sw_h // 2
        if sw_pos <= 0.01:
            track = _draw_rr((sw_w, sw_h), sw_h / 2, fill=(255, 255, 255, 34), outline=(255, 255, 255, 48), width=1)
        else:
            c_left = _mix((71, 85, 105), ACCENT_C1, sw_pos)
            c_right = _mix((71, 85, 105), ACCENT_C2, sw_pos)
            track = _draw_grad_pill(sw_w, sw_h, sw_h / 2, c_left, c_right, 255)
        frame.alpha_composite(track, (sw_x, sw_y))

        knob_d = int(round(14 * s))
        knob_margin = (sw_h - knob_d) // 2
        kx = int(round(sw_x + knob_margin + sw_pos * (sw_w - 2 * knob_margin - knob_d)))
        ky = sw_y + knob_margin
        knob = Image.new('RGBA', (knob_d * 4, knob_d * 4), (0, 0, 0, 0))
        ImageDraw.Draw(knob).ellipse((0, 0, knob_d * 4 - 1, knob_d * 4 - 1), fill=(255, 255, 255, 255))
        frame.alpha_composite(knob.resize((knob_d, knob_d), Image.LANCZOS), (kx, ky))
        hits.append(('autostart', arect, self.config.get('on_toggle_autostart'), False))

        # 2. 底部双按钮：全部重启 | 退出程序
        by = y + ah + int(round(8 * s))
        bh = int(round(34 * s))
        bgap = int(round(8 * s))
        w1 = int(round((cw - bgap) * 0.54))
        w2 = cw - bgap - w1

        r1 = (x0, by, x0 + w1, by + bh)
        r2 = (x0 + w1 + bgap, by, x0 + cw, by + bh)
        self._draw_tile(frame, 'restart_all', r1, 9 * s)
        self._draw_tile(frame, 'exit_app', r2, 9 * s, danger=True)

        b_cy = by + bh // 2
        b_ico_sz = int(round(13 * s))

        # 全部重启
        lbl1 = '全部重启'
        tot1 = b_ico_sz + 6 * s + self.f_btn.getlength(lbl1)
        ix1 = int(round(x0 + (w1 - tot1) / 2))
        col1 = TEXT_PRIMARY if self._hover_id == 'restart_all' else TEXT_SECONDARY
        frame.alpha_composite(
            self._icon('refresh', b_ico_sz, ACCENT_C1 if self._hover_id == 'restart_all' else TEXT_SECONDARY),
            (ix1, b_cy - b_ico_sz // 2),
        )
        d.text((ix1 + b_ico_sz + int(round(6 * s)), b_cy), lbl1, font=self.f_btn, fill=col1 + (255,), anchor='lm')
        hits.append(('restart_all', r1, self.config.get('on_restart_all'), True))

        # 退出程序
        lbl2 = '退出程序'
        tot2 = b_ico_sz + 6 * s + self.f_btn.getlength(lbl2)
        ix2 = int(round(r2[0] + (w2 - tot2) / 2))
        col2 = (255, 210, 218) if self._hover_id == 'exit_app' else ROSE_TEXT
        frame.alpha_composite(
            self._icon('power', b_ico_sz, ROSE if self._hover_id == 'exit_app' else ROSE_TEXT),
            (ix2, b_cy - b_ico_sz // 2),
        )
        d.text((ix2 + b_ico_sz + int(round(6 * s)), b_cy), lbl2, font=self.f_btn, fill=col2 + (255,), anchor='lm')
        hits.append(('exit_app', r2, self.config.get('on_exit'), True))
