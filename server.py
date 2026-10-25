import ctypes
import ctypes.wintypes as wt
import threading
import time

import cv2
import mss
import numpy as np
import pybase64
import pygetwindow as gw
import uvicorn
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

GWL_STYLE = -16
GWL_EXSTYLE = -20
WS_POPUP = 0x80000000
WS_CHILD = 0x40000000
WS_CAPTION = 0x00C00000
WS_THICKFRAME = 0x00040000
WS_EX_TOOLWINDOW = 0x00000080
SW_RESTORE = 9
PW_RENDERFULLCONTENT = 0x00000002

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    user32.SetProcessDPIAware()

state = {
    'mode': 'screen',
    'title': '',
    'hwnd': None,
    'last_frame': None,
    'w': 320,
    'h': 180,
}

sct = mss.mss()


def window_ok(hwnd):
    if not user32.IsWindow(hwnd):
        return False

    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
    if style & WS_CHILD or ex & WS_EX_TOOLWINDOW:
        return False

    buf = ctypes.create_unicode_buffer(256)
    if user32.GetWindowTextW(hwnd, buf, 256) == 0:
        r = wt.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        if r.right - r.left < 800 or r.bottom - r.top < 600:
            return False
    return True


def visible_windows():
    out, seen = [], set()
    for w in gw.getAllWindows():
        try:
            t = w.title.strip()
            if not t or t in seen:
                continue
            if w.width > 300 and w.height > 300 and window_ok(w._hWnd):
                out.append(t)
                seen.add(t)
        except Exception:
            continue
    return out


def find_window(part):
    part = part.lower().strip()
    cands = []
    for w in gw.getAllWindows():
        try:
            if part in w.title.lower() and w.width > 300 and w.height > 300 and window_ok(w._hWnd):
                cands.append(w)
        except Exception:
            continue
    if not cands:
        return None
    return max(cands, key=lambda w: w.width * w.height)._hWnd


def grab_window(hwnd):
    if not user32.IsWindow(hwnd):
        return None

    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
        time.sleep(0.2)

    style = user32.GetWindowLongW(hwnd, GWL_STYLE)
    if (style & WS_POPUP) and not (style & (WS_CAPTION | WS_THICKFRAME)):
        user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_CAPTION | WS_THICKFRAME)
        time.sleep(0.3)

    r = wt.RECT()
    user32.GetClientRect(hwnd, ctypes.byref(r))
    w, h = r.right - r.left, r.bottom - r.top
    if w <= 0 or h <= 0:
        return None

    hdc = user32.GetDC(hwnd)
    mdc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    gdi32.SelectObject(mdc, bmp)

    ok = user32.PrintWindow(hwnd, mdc, PW_RENDERFULLCONTENT)
    if not ok:
        ok = user32.PrintWindow(hwnd, mdc, 0)
    if not ok:
        user32.ReleaseDC(hwnd, hdc)
        gdi32.DeleteDC(mdc)
        gdi32.DeleteObject(bmp)
        return None

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ('biSize', ctypes.c_uint32), ('biWidth', ctypes.c_long),
            ('biHeight', ctypes.c_long), ('biPlanes', ctypes.c_ushort),
            ('biBitCount', ctypes.c_ushort), ('biCompression', ctypes.c_uint32),
            ('biSizeImage', ctypes.c_uint32), ('biXPelsPerMeter', ctypes.c_long),
            ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', ctypes.c_uint32),
            ('biClrImportant', ctypes.c_uint32),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [('bmiHeader', BITMAPINFOHEADER)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biSizeImage = w * h * 4

    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mdc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    img = np.frombuffer(buf.raw, np.uint8).reshape((h, w, 4)).copy()

    img[img[:, :, 3] < 128, :3] = 0
    img[:, :, 3] = 255

    user32.ReleaseDC(hwnd, hdc)
    gdi32.DeleteDC(mdc)
    gdi32.DeleteObject(bmp)
    return img


def grab_screen():
    shot = sct.grab(sct.monitors[1])
    img = np.frombuffer(shot.rgb, np.uint8).reshape((shot.height, shot.width, 3))
    return img[:, :, ::-1]


def to_565(img, tw, th):
    img = cv2.resize(img[:, :, :3], (tw, th), interpolation=cv2.INTER_AREA)
    b = img[:, :, 0].astype(np.uint16)
    g = img[:, :, 1].astype(np.uint16)
    r = img[:, :, 2].astype(np.uint16)
    px = (r >> 3) << 11 | (g >> 2) << 5 | (b >> 3)
    return pybase64.b64encode(px.astype('<u2').tobytes()).decode()


def last_good(tw, th):
    if state['last_frame']:
        return {'status': 'ok', 'data': state['last_frame'], 'w': tw, 'h': th}
    return None


app = FastAPI(docs_url=None, redoc_url=None)
app.add_middleware(CORSMiddleware, allow_origins=['*'], allow_methods=['*'], allow_headers=['*'])


@app.get('/api')
async def api(action: str = Query(None), title: str = Query(None),
              level: str = Query(None), val: str = Query(None)):
    if action == 'list':
        return {'windows': visible_windows()}

    if action == 'status':
        return {'mode': state['mode'], 'window': state['title'],
                'resolution': '%dx%d' % (state['w'], state['h'])}

    if action == 'window' and title:
        if title == '__FULLSCREEN__':
            state.update(mode='screen', title='', hwnd=None, last_frame=None)
            return {'status': 'ok', 'message': 'fullscreen'}

        hwnd = find_window(title)
        if not hwnd:
            return {'status': 'error', 'message': 'window not found'}
        state.update(mode='window', title=title, hwnd=hwnd, last_frame=None)
        return {'status': 'ok', 'message': title}

    if action == 'quality' and level in ('high', 'low'):
        state['w'], state['h'] = (320, 180) if level == 'high' else (160, 90)
        return {'status': 'ok', 'message': '%dx%d' % (state['w'], state['h'])}

    if action == 'res' and val:
        try:
            w, h = (int(x) for x in val.lower().split('x'))
            if w > 0 and h > 0:
                state['w'], state['h'] = w, h
                return {'status': 'ok', 'message': '%dx%d' % (w, h)}
        except Exception:
            pass
        return {'status': 'error', 'message': 'format is WxH'}

    return {'status': 'error', 'message': 'unknown action'}


@app.get('/screen')
async def screen(res: str = Query(None)):
    tw, th = state['w'], state['h']
    if res == 'low':
        tw, th = 160, 90

    try:
        img = None
        if state['mode'] == 'window' and state['title']:
            hwnd = state['hwnd']
            if not hwnd or not user32.IsWindow(hwnd):
                hwnd = find_window(state['title'])
                state['hwnd'] = hwnd
            if hwnd:
                img = grab_window(hwnd)
            if img is None:
                prev = last_good(tw, th)
                if prev:
                    return prev
                return {'status': 'error', 'message': 'window unavailable'}
        else:
            img = grab_screen()

        data = to_565(img, tw, th)
        state['last_frame'] = data
        return {'status': 'ok', 'data': data, 'w': tw, 'h': th}

    except Exception as e:
        print('[screen]', e)
        prev = last_good(tw, th)
        if prev:
            return prev
        return {'status': 'error', 'message': str(e)}


def console():
    print('list | res <w> <h> | status | quit')
    while True:
        try:
            cmd = input('> ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            break

        if not cmd:
            continue

        if cmd == 'list':
            titles = visible_windows()
            print(' 0. full screen')
            for i, t in enumerate(titles, 1):
                print(' %d. %s' % (i, t[:60]))
            pick = input('number: ').strip()
            if not pick.isdigit():
                continue
            n = int(pick)
            if n == 0:
                state.update(mode='screen', title='', hwnd=None, last_frame=None)
                print('ok, full screen')
            elif 1 <= n <= len(titles):
                hwnd = find_window(titles[n - 1])
                if hwnd:
                    state.update(mode='window', title=titles[n - 1], hwnd=hwnd, last_frame=None)
                    print('ok,', titles[n - 1])
                else:
                    print('window not found')
            else:
                print('invalid number')

        elif cmd.startswith('res'):
            p = cmd.split()
            if len(p) == 3:
                try:
                    w, h = int(p[1]), int(p[2])
                    if w > 0 and h > 0:
                        state['w'], state['h'] = w, h
                        print('ok, %dx%d' % (w, h))
                    else:
                        print('values must be > 0')
                except ValueError:
                    print('res <w> <h>')
            else:
                print('res <w> <h>')

        elif cmd == 'status':
            print('mode:', state['mode'], '| window:', state['title'] or '-',
                  '| %dx%d' % (state['w'], state['h']))

        elif cmd == 'quit':
            break


if __name__ == '__main__':
    threading.Thread(target=console, daemon=True).start()
    print('screen:  http://localhost:5000/screen')
    print('windows: http://localhost:5000/api?action=list')
    uvicorn.run(app, host='0.0.0.0', port=5000, log_level='warning')
