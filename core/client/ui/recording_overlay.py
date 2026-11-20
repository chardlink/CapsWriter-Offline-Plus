"""录音时的桌面悬浮反馈：胶囊外形 + 柔和光晕 + 声波动画 + 逐字打字字幕。

渲染方式：每帧用 Pillow 绘制 RGBA 画面，再通过 Win32 UpdateLayeredWindow 提交，
从而得到真正的逐像素半透明、抗锯齿圆角与外发光。Tk 只作为窗口宿主和事件循环。
所有 Tk / GDI 操作都在窗口线程内执行，外部只通过队列投递事件。
"""

import bisect
import ctypes
import logging
import math
import os
import threading
import time
import tkinter as tk
from ctypes import wintypes
from difflib import SequenceMatcher
from itertools import accumulate
from queue import Empty, Queue

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------- 外观参数
VISIBLE_CHARS = 20          # 字幕区域可容纳的汉字数（始终显示最后这些字）
FONT_PX = 22                # 字幕字号（96 DPI 下像素）
TEXT_COLOR = (246, 248, 255)
BODY_COLOR = (14, 18, 30, 150)   # 玻璃底色，alpha 越小越透明
CHAR_FADE = 0.24            # 单字淡入时长（秒）

THEMES = {
    #            主色 c1           辅色 c2
    'recording': ((56, 189, 248), (139, 92, 246)),
    'processing': ((99, 102, 241), (236, 72, 153)),
    'done': ((52, 211, 153), (34, 211, 238)),
    'error': ((251, 113, 133), (249, 115, 22)),
}


def _clamp(v, lo=0.0, hi=1.0):
    return lo if v < lo else hi if v > hi else v


def _ease_out(t):
    t = _clamp(t)
    return 1 - (1 - t) ** 3


def _mix(c1, c2, t):
    return tuple(int(round(a + (b - a) * t)) for a, b in zip(c1, c2))


def _hgrad(width, height, c1, c2):
    """水平渐变 RGBA 图（不透明）。"""
    t = np.linspace(0.0, 1.0, max(width, 1), dtype=np.float32)[None, :, None]
    rgb = np.asarray(c1, np.float32) * (1 - t) + np.asarray(c2, np.float32) * t
    arr = np.empty((height, width, 4), np.uint8)
    arr[..., :3] = np.broadcast_to(rgb, (height, width, 3)).astype(np.uint8)
    arr[..., 3] = 255
    return Image.fromarray(arr, 'RGBA')


def _scale_alpha(img, k):
    if k >= 0.999:
        return img
    out = img.copy()
    out.putalpha(ImageChops.multiply(img.getchannel('A'),
                                     Image.new('L', img.size, int(255 * _clamp(k)))))
    return out


def _load_font(px):
    fonts_dir = os.path.join(os.environ.get('WINDIR', r'C:\Windows'), 'Fonts')
    for name in ('msyh.ttc', 'msyh.ttf', 'simhei.ttf', 'simsun.ttc', 'segoeui.ttf'):
        try:
            return ImageFont.truetype(os.path.join(fonts_dir, name), px)
        except OSError:
            continue
    return ImageFont.load_default(px)


# ---------------------------------------------------------------- Win32 分层窗口
class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [('biSize', wintypes.DWORD), ('biWidth', wintypes.LONG),
                ('biHeight', wintypes.LONG), ('biPlanes', wintypes.WORD),
                ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', wintypes.LONG),
                ('biYPelsPerMeter', wintypes.LONG), ('biClrUsed', wintypes.DWORD),
                ('biClrImportant', wintypes.DWORD)]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [('bmiHeader', _BITMAPINFOHEADER), ('bmiColors', wintypes.DWORD * 3)]


class _BLENDFUNCTION(ctypes.Structure):
    _fields_ = [('BlendOp', ctypes.c_ubyte), ('BlendFlags', ctypes.c_ubyte),
                ('SourceConstantAlpha', ctypes.c_ubyte), ('AlphaFormat', ctypes.c_ubyte)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]


# 使用独立的 WinDLL 实例，避免与其他模块（如 tray_panel）冲突 argtypes
user32 = ctypes.WinDLL('user32')
gdi32 = ctypes.WinDLL('gdi32')
user32.GetParent.restype = ctypes.c_void_p
user32.GetParent.argtypes = [ctypes.c_void_p]
user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
user32.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, ctypes.c_uint]
user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
user32.GetDC.restype = ctypes.c_void_p
user32.GetDC.argtypes = [ctypes.c_void_p]
user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
user32.GetForegroundWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.restype = ctypes.c_void_p
user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
user32.UpdateLayeredWindow.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
    ctypes.POINTER(wintypes.SIZE), ctypes.c_void_p, ctypes.POINTER(wintypes.POINT),
    wintypes.DWORD, ctypes.POINTER(_BLENDFUNCTION), wintypes.DWORD]
gdi32.CreateCompatibleDC.restype = ctypes.c_void_p
gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
gdi32.CreateDIBSection.restype = ctypes.c_void_p
gdi32.CreateDIBSection.argtypes = [ctypes.c_void_p, ctypes.POINTER(_BITMAPINFO), ctypes.c_uint,
                                   ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p,
                                   wintypes.DWORD]
gdi32.SelectObject.restype = ctypes.c_void_p
gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
gdi32.DeleteDC.argtypes = [ctypes.c_void_p]


class _LayeredSurface:
    """把 RGBA 图像提交到分层窗口（逐像素 alpha）。"""

    def __init__(self, hwnd, width, height):
        self.hwnd, self.width, self.height = hwnd, width, height
        self.screen_dc = user32.GetDC(None)
        self.mem_dc = gdi32.CreateCompatibleDC(self.screen_dc)
        bmi = _BITMAPINFO()
        hdr = bmi.bmiHeader
        hdr.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
        hdr.biWidth, hdr.biHeight = width, -height      # 负高度 = 自上而下
        hdr.biPlanes, hdr.biBitCount, hdr.biCompression = 1, 32, 0
        self.bits = ctypes.c_void_p()
        self.bitmap = gdi32.CreateDIBSection(self.mem_dc, ctypes.byref(bmi), 0,
                                             ctypes.byref(self.bits), None, 0)
        self.old_bitmap = gdi32.SelectObject(self.mem_dc, self.bitmap)
        self.buffer = np.empty((height, width, 4), np.uint8)

    def present(self, img, x, y, alpha=255):
        arr = np.asarray(img, dtype=np.uint8)
        a = arr[..., 3:4].astype(np.uint16)
        # RGBA -> 预乘 BGRA
        self.buffer[..., :3] = ((arr[..., 2::-1].astype(np.uint16) * a + 127) // 255)
        self.buffer[..., 3] = arr[..., 3]
        ctypes.memmove(self.bits, self.buffer.ctypes.data, self.buffer.nbytes)
        blend = _BLENDFUNCTION(0, 0, int(_clamp(alpha, 0, 255)), 1)
        ok = user32.UpdateLayeredWindow(
            self.hwnd, self.screen_dc, ctypes.byref(wintypes.POINT(int(x), int(y))),
            ctypes.byref(wintypes.SIZE(self.width, self.height)), self.mem_dc,
            ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)
        if not ok:
            # Tk 创建 overrideredirect 窗口时会先调用 SetLayeredWindowAttributes，
            # Windows 要求先清除再重新设置 WS_EX_LAYERED 才能切换到 UpdateLayeredWindow。
            ex = user32.GetWindowLongW(self.hwnd, -20)
            user32.SetWindowLongW(self.hwnd, -20, ex & ~0x80000)
            user32.SetWindowLongW(self.hwnd, -20, ex | 0x80000)
            user32.UpdateLayeredWindow(
                self.hwnd, self.screen_dc, ctypes.byref(wintypes.POINT(int(x), int(y))),
                ctypes.byref(wintypes.SIZE(self.width, self.height)), self.mem_dc,
                ctypes.byref(wintypes.POINT(0, 0)), 0, ctypes.byref(blend), 2)

    def close(self):
        gdi32.SelectObject(self.mem_dc, self.old_bitmap)
        gdi32.DeleteObject(self.bitmap)
        gdi32.DeleteDC(self.mem_dc)
        user32.ReleaseDC(None, self.screen_dc)


# ---------------------------------------------------------------- 悬浮窗
class RecordingOverlay:
    def __init__(self):
        self._events = Queue()
        self._level_in = 0.0
        self._thread = threading.Thread(target=self._run, daemon=True, name='RecordingOverlay')
        self._thread.start()

    # ---- 线程安全的外部接口 ----
    def recording(self, started_at):
        self._events.put(('recording', started_at, ''))

    def processing(self, started_at):
        self._events.put(('processing', started_at, ''))

    def text(self, started_at, value, final=False):
        self._events.put(('final' if final else 'text', started_at, value))

    def cancel(self, started_at):
        self._events.put(('cancel', started_at, ''))

    def error(self, started_at, message):
        self._events.put(('error', started_at, message))

    def notify(self, message, theme='done', duration=2.4):
        """在底部胶囊悬浮窗中显示一段简短通知（如开机自启、清除记忆、保留天数等）。"""
        self._events.put(('notify', duration, (theme, message)))

    def level(self, rms):
        """输入当前音量（RMS，float32 音频幅度），驱动声波动画。"""
        db = 20 * math.log10(max(float(rms), 1e-6))
        self._level_in = _clamp((db + 52) / 38)

    def stop(self):
        self._events.put(('stop', None, ''))
        if threading.current_thread() is not self._thread:
            self._thread.join(timeout=1)

    # ---- 窗口线程 ----
    def _run(self):
        try:
            user32.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))  # Per-Monitor V2
        except Exception:
            pass
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.geometry('1x1+-200+-200')
        self.root.update_idletasks()
        self.root.deiconify()
        self.root.update_idletasks()
        self.hwnd = user32.GetParent(self.root.winfo_id())
        style = user32.GetWindowLongW(self.hwnd, -20)
        # 先清除 Tk 预设的 WS_EX_LAYERED（它绑定了 SetLayeredWindowAttributes 模式），
        # 再重新开启，使窗口进入支持 UpdateLayeredWindow 的逐像素 Alpha 模式。
        user32.SetWindowLongW(self.hwnd, -20, style & ~0x80000)
        # 分层 + 点击穿透 + 不抢焦点 + 不出现在任务栏 + 置顶
        user32.SetWindowLongW(self.hwnd, -20,
                              style | 0x80000 | 0x20 | 0x08000000 | 0x80 | 0x8)
        user32.ShowWindow(self.hwnd, 0)

        try:
            dpi = user32.GetDpiForWindow(ctypes.c_void_p(self.hwnd)) or 96
        except Exception:
            dpi = 96
        self.s = dpi / 96
        self._build_assets()
        self.surface = _LayeredSurface(self.hwnd, self.win_w, self.win_h)

        self._reset_session(None)
        self.visible = False
        self.show_at = 0.0
        self.hide_from = None
        self.last_tick = time.monotonic()
        self.root.after(15, self._tick)
        self.root.mainloop()
        self.surface.close()
        self.root = None

    # ---------------------------------------------------- 资源预渲染
    def _build_assets(self):
        s = self.s
        self.font = _load_font(int(round(FONT_PX * s)))
        self._char_w = {}
        self.m = int(30 * s)                      # 光晕外边距
        self.cap_h = int(62 * s)
        self.orb_box = self.cap_h                 # 左侧圆形图标区
        self.bars_w = int(70 * s)
        self.text_w = int(self.font.getlength('国') * VISIBLE_CHARS)
        self.cap_w = self.orb_box + int(4 * s) + self.text_w + int(12 * s) + self.bars_w
        self.win_w = self.cap_w + 2 * self.m
        self.win_h = self.cap_h + 2 * self.m
        self.text_x = self.m + self.orb_box + int(4 * s)
        self.tpad = int(14 * s)
        self.orb_r = 19 * s

        self.capsule_mask = self._capsule_mask(4)
        self.body = self._make_body()
        self.glows = {k: self._make_glow(*v) for k, v in THEMES.items()}
        self.orbs = {k: self._make_orb(k) for k in THEMES}

        fade_w = int(40 * s)
        w = self.text_w + 2 * self.tpad
        ramp = np.clip((np.arange(w) - self.tpad) / fade_w, 0, 1)
        ramp = (ramp * ramp * (3 - 2 * ramp) * 255).astype(np.uint8)
        self.fade_mask = Image.fromarray(np.broadcast_to(ramp, (self.cap_h, w)).copy(), 'L')
        self.glow_lut = [int(v * 0.5) for v in range(256)]

    def _capsule_mask(self, ss):
        """胶囊形状的抗锯齿蒙版（窗口尺寸）。"""
        big = Image.new('L', (self.win_w * ss, self.win_h * ss), 0)
        m = self.m * ss
        ImageDraw.Draw(big).rounded_rectangle(
            (m, m, m + self.cap_w * ss - 1, m + self.cap_h * ss - 1),
            radius=self.cap_h * ss // 2, fill=255)
        return big.resize((self.win_w, self.win_h), Image.LANCZOS)

    def _make_body(self):
        s, ss = self.s, 4
        size = (self.win_w, self.win_h)
        body = Image.new('RGBA', size, BODY_COLOR[:3] + (0,))
        body.putalpha(ImageChops.multiply(self.capsule_mask,
                                          Image.new('L', size, BODY_COLOR[3])))
        # 顶部玻璃高光
        g = np.zeros((self.win_h, 1), np.float32)
        top, mid = self.m, self.m + self.cap_h * 0.55
        ys = np.arange(self.win_h)
        g[:, 0] = np.clip(1 - (ys - top) / (mid - top), 0, 1) ** 1.6 * 26
        sheen = Image.new('RGBA', size, (255, 255, 255, 0))
        sheen.putalpha(ImageChops.multiply(
            Image.fromarray(np.broadcast_to(g, (self.win_h, self.win_w)).astype(np.uint8), 'L'),
            self.capsule_mask))
        body = Image.alpha_composite(body, sheen)
        # 细描边
        big = Image.new('RGBA', (self.win_w * ss, self.win_h * ss), (0, 0, 0, 0))
        m = self.m * ss
        ImageDraw.Draw(big).rounded_rectangle(
            (m, m, m + self.cap_w * ss - 1, m + self.cap_h * ss - 1),
            radius=self.cap_h * ss // 2, outline=(255, 255, 255, 52),
            width=max(ss, int(1.2 * s * ss)))
        return Image.alpha_composite(body, big.resize(size, Image.LANCZOS))

    def _make_glow(self, c1, c2):
        s = self.s
        size = (self.win_w, self.win_h)
        grad = _hgrad(self.win_w, self.win_h, c1, c2)
        spread = self.capsule_mask.filter(ImageFilter.GaussianBlur(14 * s))
        spread = spread.point(lambda v: min(255, int(v * 1.25)))
        # 内部只保留少量色彩，保证字幕对比度
        inner = self.capsule_mask.point(lambda v: 255 - int(v * 0.78))
        grad.putalpha(ImageChops.multiply(spread, inner))
        return grad

    def _make_orb(self, kind):
        ss = 4
        box = self.orb_box * ss
        r = self.orb_r * ss
        c = box / 2
        c1, c2 = THEMES[kind]
        img = Image.new('RGBA', (box, box), (0, 0, 0, 0))
        # 对角线渐变球体
        yy, xx = np.mgrid[0:box, 0:box].astype(np.float32)
        t = np.clip(((xx - c + r) + (yy - c + r)) / (4 * r), 0, 1)[..., None]
        rgb = np.asarray(c1, np.float32) * (1 - t) + np.asarray(c2, np.float32) * t
        arr = np.zeros((box, box, 4), np.uint8)
        arr[..., :3] = rgb.astype(np.uint8)
        mask = Image.new('L', (box, box), 0)
        ImageDraw.Draw(mask).ellipse((c - r, c - r, c + r, c + r), fill=255)
        arr[..., 3] = np.asarray(mask)
        img = Image.fromarray(arr, 'RGBA')
        # 顶部高光
        hl = Image.new('RGBA', (box, box), (255, 255, 255, 0))
        hm = Image.new('L', (box, box), 0)
        ImageDraw.Draw(hm).ellipse((c - r * 0.72, c - r * 0.92, c + r * 0.72, c - r * 0.05),
                                   fill=46)
        hl.putalpha(ImageChops.multiply(hm.filter(ImageFilter.GaussianBlur(r * 0.12)), mask))
        img = Image.alpha_composite(img, hl)

        d = ImageDraw.Draw(img)
        white = (255, 255, 255, 255)
        lw = max(2, int(r * 0.1))
        if kind == 'done':
            d.line([(c - r * 0.36, c + r * 0.02), (c - r * 0.1, c + r * 0.28),
                    (c + r * 0.38, c - r * 0.26)], fill=white, width=int(r * 0.13), joint='curve')
        elif kind == 'error':
            d.rounded_rectangle((c - r * 0.07, c - r * 0.5, c + r * 0.07, c + r * 0.12),
                                radius=r * 0.07, fill=white)
            d.ellipse((c - r * 0.085, c + r * 0.26, c + r * 0.085, c + r * 0.43), fill=white)
        else:  # 麦克风
            d.rounded_rectangle((c - r * 0.19, c - r * 0.56, c + r * 0.19, c + r * 0.12),
                                radius=r * 0.19, fill=white)
            d.arc((c - r * 0.37, c - r * 0.3, c + r * 0.37, c + r * 0.36),
                  start=0, end=180, fill=white, width=lw)
            d.rounded_rectangle((c - lw / 2, c + r * 0.34, c + lw / 2, c + r * 0.54),
                                radius=lw / 2, fill=white)
            d.rounded_rectangle((c - r * 0.22, c + r * 0.5, c + r * 0.22, c + r * 0.5 + lw),
                                radius=lw / 2, fill=white)
        return img.resize((self.orb_box, self.orb_box), Image.LANCZOS)

    # ---------------------------------------------------- 状态
    def _reset_session(self, session):
        self.session = session
        self.phase = 'idle'
        self.target = ''          # 目标字幕（已对齐合并的全部文本）
        self.cum = [0.0]          # 每个字的累计 x 坐标
        self.n = 0                # 已"打"出来的字数
        self.born = []            # 每个字出现的时刻
        self.type_acc = 1.0
        self.offset = 0.0         # 水平滚动偏移（像素）
        self.placeholder = ''
        self.placeholder_color = TEXT_COLOR
        self.hide_at = 0.0
        self.theme = self.prev_theme = 'recording'
        self.theme_at = 0.0
        self.lvl = 0.0
        self.bars = [0.0] * 5
        self.text_cache = None
        self.text_key = None

    def _set_theme(self, name, now):
        if name != self.theme:
            self.prev_theme, self.theme, self.theme_at = self.theme, name, now

    def _width(self, ch):
        w = self._char_w.get(ch)
        if w is None:
            w = self._char_w[ch] = self.font.getlength(ch)
        return w

    def _set_target(self, new):
        p = len(os.path.commonprefix([self.target, new]))
        if self.n > p:            # 修正：回退到公共前缀，再重新"打"出来
            self.n = p
            del self.born[p:]
        self.target = new
        self.cum = list(accumulate((self._width(c) for c in new), initial=0.0))

    def _merge(self, value):
        """把新的识别假设与已有字幕对齐合并，避免整段替换造成的跳动。

        实时预览识别的是最近 8 秒的滑动窗口，新结果不一定以旧结果开头，
        这里在旧字幕尾部寻找与新结果最长的重合片段，从重合处接上。
        """
        old = self.target
        if not old or value.startswith(old):
            return value
        if old.startswith(value) or value in old[-len(value) - 4:]:
            return old
        tail_len = min(len(old), 48)
        tail = old[-tail_len:]
        m = SequenceMatcher(None, tail, value, autojunk=False).find_longest_match(
            0, len(tail), 0, len(value))
        if m.size >= 2 or (m.size == 1 and len(value) <= 2):
            return old[:len(old) - tail_len + m.a] + value[m.b:]
        return old + value

    def _show(self, now):
        self.show_at = now
        self.hide_from = None
        work = wintypes.RECT(0, 0, user32.GetSystemMetrics(0), user32.GetSystemMetrics(1))
        try:
            mon = user32.MonitorFromWindow(user32.GetForegroundWindow(), 2)
            info = _MONITORINFO()
            info.cbSize = ctypes.sizeof(_MONITORINFO)
            if mon and user32.GetMonitorInfoW(mon, ctypes.byref(info)):
                work = info.rcWork
        except Exception:
            pass
        self.pos_x = work.left + (work.right - work.left - self.win_w) // 2
        self.pos_y = work.bottom - self.win_h - int(56 * self.s)
        if not self.visible:
            self.visible = True
            self._present(now)
            user32.ShowWindow(self.hwnd, 4)               # SW_SHOWNOACTIVATE
        user32.SetWindowPos(self.hwnd, ctypes.c_void_p(-1), 0, 0, 0, 0, 0x53)  # 置顶

    def _begin_hide(self, now):
        if self.visible and self.hide_from is None:
            self.hide_from = now

    # ---------------------------------------------------- 主循环
    def _tick(self):
        now = time.monotonic()
        dt = min(0.05, now - self.last_tick)
        self.last_tick = now
        try:
            if not self._handle_events(now):
                return
            if self.visible:
                self._update(now, dt)
                self._present(now)
        except Exception:
            logger.exception('录音悬浮窗刷新失败')
        self.root.after(12 if self.visible else 30, self._tick)

    def _handle_events(self, now):
        try:
            while True:
                kind, started_at, value = self._events.get_nowait()
                if kind == 'stop':
                    user32.ShowWindow(self.hwnd, 0)
                    self.root.destroy()
                    return False
                if kind == 'recording':
                    self._reset_session(started_at)
                    self.phase = 'recording'
                    self.placeholder = '正在聆听…'
                    self.theme_at = now
                    self._level_in = 0.0
                    self._show(now)
                    continue
                if kind == 'notify':
                    # 录音或识别进行中时不打断，仅在空闲或已完成状态下展示系统通知
                    if self.phase in ('recording', 'processing'):
                        continue
                    theme, msg = value
                    theme = theme if theme in THEMES else 'done'
                    self._reset_session(f'notify-{now}')
                    self.phase = 'done'
                    self.theme = self.prev_theme = theme
                    self.theme_at = now
                    self._set_target(msg)
                    self.n = len(msg)
                    self.born = [now - 0.08] * self.n
                    self.offset = 0.0
                    self.hide_at = now + max(1.2, float(started_at or 2.4))
                    self._show(now)
                    continue
                if started_at != self.session:
                    continue
                if kind == 'cancel':
                    self.phase = 'idle'
                    self._begin_hide(now)
                elif kind == 'processing':
                    self.phase = 'processing'
                    self._level_in = 0.0
                    self._set_theme('processing', now)
                    self.placeholder = '正在识别…'
                elif kind == 'error':
                    self.phase = 'done'
                    self._set_theme('error', now)
                    self._set_target('')
                    self.placeholder = value
                    self.placeholder_color = (255, 205, 210)
                    self.hide_at = now + 3
                elif kind == 'text':
                    if self.phase in ('done', 'idle') or not value:
                        continue
                    self._set_target(self._merge(value))
                elif kind == 'final':
                    self.phase = 'done'
                    self._set_theme('done', now)
                    if value:
                        self._set_target(value)
                        # 最终结果与已显示差异较大时，直接补齐到只剩最后几个字待打出
                        jump = len(value) - 8
                        if self.n < jump:
                            self.born.extend([now - 1] * (jump - self.n))
                            self.n = jump
                    else:
                        self._set_target('')
                        self.placeholder = '未识别到语音'
                        self.hide_at = now + 1.8
        except Empty:
            pass
        return True

    def _update(self, now, dt):
        # 音量平滑：快起慢落
        target_lvl = self._level_in if self.phase == 'recording' else 0.0
        k = 22 if target_lvl > self.lvl else 5
        self.lvl += (target_lvl - self.lvl) * min(1.0, dt * k)

        # 逐字打出：按待打字数自适应速度，让字幕在两次识别结果之间连续流动
        pending = len(self.target) - self.n
        if pending > 0:
            if self.phase == 'done':
                rate = max(36.0, pending / 0.35)
            else:
                rate = _clamp(pending / 0.55, 10.0, 50.0)
            self.type_acc += rate * dt
            while self.type_acc >= 1 and self.n < len(self.target):
                self.born.append(now)
                self.n += 1
                self.type_acc -= 1
        else:
            self.type_acc = 1.0

        # 水平平滑滚动：最新的字始终贴在右侧
        goal = max(0.0, self.cum[self.n] - self.text_w)
        self.offset += (goal - self.offset) * (1 - math.exp(-dt * 13))
        if abs(goal - self.offset) < 0.3:
            self.offset = goal

        if self.phase == 'done' and not self.hide_at and self.n >= len(self.target):
            self.hide_at = now + 2.2
        if self.hide_at and now >= self.hide_at:
            self._begin_hide(now)
        if self.hide_from is not None and now - self.hide_from > 0.32:
            self.visible = False
            self.hide_from = None
            self.phase = 'idle'
            self.session = None
            user32.ShowWindow(self.hwnd, 0)

    # ---------------------------------------------------- 绘制
    def _theme_colors(self, now):
        t = _ease_out((now - self.theme_at) / 0.35)
        a1, a2 = THEMES[self.prev_theme]
        b1, b2 = THEMES[self.theme]
        return t, _mix(a1, b1, t), _mix(a2, b2, t)

    def _present(self, now):
        if self.hide_from is not None:
            p = 1 - _ease_out((now - self.hide_from) / 0.3)
        else:
            p = _ease_out((now - self.show_at) / 0.24)
        frame = self._render(now)
        self.surface.present(frame, self.pos_x, self.pos_y + (1 - p) * 14 * self.s, 255 * p)

    def _render(self, now):
        s = self.s
        t, c1, c2 = self._theme_colors(now)
        if self.phase == 'recording':
            glow_k = 0.5 + 0.5 * self.lvl
        elif self.phase == 'processing':
            glow_k = 0.62 + 0.22 * math.sin(now * 3.2)
        else:
            glow_k = 0.75

        frame = Image.new('RGBA', (self.win_w, self.win_h), (0, 0, 0, 0))
        if t < 1:
            frame.alpha_composite(_scale_alpha(self.glows[self.prev_theme], glow_k * (1 - t)))
        frame.alpha_composite(_scale_alpha(self.glows[self.theme], glow_k * t))
        frame.alpha_composite(self.body)
        frame.alpha_composite(self._render_orb(now, t, c1, c2), (self.m, self.m))
        frame.alpha_composite(self._render_bars(now, c1, c2),
                              (self.m + self.cap_w - self.bars_w, self.m))
        frame.alpha_composite(self._render_text(now, c1),
                              (self.text_x - self.tpad, self.m))
        return frame

    def _render_orb(self, now, t, c1, c2):
        ss, s = 3, self.s
        box = self.orb_box
        c, r = box * ss / 2, self.orb_r * ss
        halo = Image.new('RGBA', (box * ss, box * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(halo)
        if self.phase == 'recording':
            rr = r + (2 + 8 * self.lvl) * s * ss
            d.ellipse((c - rr, c - rr, c + rr, c + rr), fill=c1 + (int(45 + 110 * self.lvl),))
            # 周期性扩散波纹
            ph = (now % 1.6) / 1.6
            rr = r + ph * 11 * s * ss
            d.ellipse((c - rr, c - rr, c + rr, c + rr), outline=c2 + (int(170 * (1 - ph)),),
                      width=max(1, int(1.6 * s * ss)))
        elif self.phase == 'processing':
            rr = r + 3.5 * s * ss
            start = (now * 300) % 360
            d.arc((c - rr, c - rr, c + rr, c + rr), start, start + 110,
                  fill=c2 + (235,), width=max(2, int(2.4 * s * ss)))
            d.arc((c - rr, c - rr, c + rr, c + rr), start + 180, start + 230,
                  fill=c1 + (160,), width=max(2, int(2.4 * s * ss)))
        else:
            rr = r + 4 * s * ss
            d.ellipse((c - rr, c - rr, c + rr, c + rr), fill=c1 + (60,))
        halo = halo.resize((box, box), Image.LANCZOS)
        orb = self.orbs[self.theme]
        if t < 1:
            orb = Image.blend(self.orbs[self.prev_theme], orb, t)
        if self.phase == 'recording':     # 随音量轻微"呼吸"
            k = 1 + 0.06 * self.lvl
            size = int(box * k) | 1
            big = orb.resize((size, size), Image.BICUBIC)
            off = (box - size) // 2
            orb = Image.new('RGBA', (box, box), (0, 0, 0, 0))
            orb.alpha_composite(big.crop((-off, -off, -off + box, -off + box)) if off < 0 else big,
                                (max(off, 0), max(off, 0)))
        halo.alpha_composite(orb)
        return halo

    def _render_bars(self, now, c1, c2):
        ss, s = 3, self.s
        n = len(self.bars)
        bw, gap = 4 * s, 5 * s
        min_h, max_h = 4 * s, 28 * s
        weights = (0.55, 0.85, 1.0, 0.85, 0.55)
        for i in range(n):
            if self.phase == 'recording':
                jitter = 0.62 + 0.38 * math.sin(now * (7.0 + i * 1.3) + i * 1.7)
                v = _clamp(self.lvl * weights[i] * jitter * 1.3
                           + 0.07 * (0.5 + 0.5 * math.sin(now * 3 + i)))
            elif self.phase == 'processing':
                v = 0.12 + 0.38 * (0.5 + 0.5 * math.sin(now * 7 - i * 0.9))
            else:
                v = 0.0
            self.bars[i] += (v - self.bars[i]) * 0.35
        img = Image.new('RGBA', (self.bars_w * ss, self.cap_h * ss), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        total = n * bw + (n - 1) * gap
        x0 = (self.bars_w - 22 * s - total) / 2 + 4 * s
        cy = self.cap_h / 2
        for i, v in enumerate(self.bars):
            h = min_h + (max_h - min_h) * v
            x = x0 + i * (bw + gap)
            col = _mix(c1, c2, i / (n - 1)) + (240,)
            d.rounded_rectangle(((x) * ss, (cy - h / 2) * ss, (x + bw) * ss, (cy + h / 2) * ss),
                                radius=bw * ss / 2, fill=col)
        return img.resize((self.bars_w, self.cap_h), Image.LANCZOS)

    def _render_text(self, now, accent):
        s = self.s
        young = self.n > 0 and now - self.born[self.n - 1] < CHAR_FADE
        moving = abs(max(0.0, self.cum[self.n] - self.text_w) - self.offset) > 0.01
        key = (self.target, self.n, round(self.offset, 1), accent, self.placeholder)
        if self.n and not young and not moving and key == self.text_key:
            return self.text_cache

        w, h = self.text_w + 2 * self.tpad, self.cap_h
        layer = Image.new('RGBA', (w, h), TEXT_COLOR + (0,))
        d = ImageDraw.Draw(layer)
        cy = h / 2
        pad = self.tpad
        if self.n == 0:
            if self.placeholder:
                a = int(150 + 70 * math.sin(now * 3.4))
                d.text((pad, cy), self.placeholder, font=self.font,
                       fill=self.placeholder_color + (a,), anchor='lm')
        else:
            cum, off = self.cum, self.offset
            start = max(0, bisect.bisect_right(cum, off - pad) - 1)
            k = self.n
            while k > start and now - self.born[k - 1] < CHAR_FADE:
                k -= 1
            if start < k:
                d.text((pad + cum[start] - off, cy), self.target[start:k], font=self.font,
                       fill=TEXT_COLOR + (255,), anchor='lm')
            for i in range(k, self.n):
                p = _ease_out((now - self.born[i]) / CHAR_FADE)
                d.text((pad + cum[i] - off, cy + (1 - p) * 7 * s), self.target[i],
                       font=self.font, fill=TEXT_COLOR + (int(255 * p),), anchor='lm')
            if off > 0.5:
                layer.putalpha(ImageChops.multiply(layer.getchannel('A'), self.fade_mask))

        # 文字柔光
        glow_a = layer.getchannel('A').filter(ImageFilter.GaussianBlur(7 * s)).point(self.glow_lut)
        glow = Image.new('RGBA', (w, h), _mix(accent, (255, 255, 255), 0.35) + (0,))
        glow.putalpha(glow_a)
        glow.alpha_composite(layer)
        self.text_cache, self.text_key = glow, key
        return glow
