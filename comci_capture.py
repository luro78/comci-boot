# -*- coding: utf-8 -*-
"""
컴시간 접속 정보 수집기

2026년 11월부터 컴시간 알리미는 페이지 스크립트를 알아볼 수 없게 바꾸고, 시간표 자료 주소를 하루에도 여러 번 바꿉니다.
그래서 크롬(또는 엣지)을 화면 없이 띄워 실제로 컴시간을 열고 학교를 검색·선택한 뒤,
그때 페이지가 보내는 요청 주소에서 접속 정보를 알아냅니다.

  주소(route)   : http://comci.net:4082/<route>?...
  검색(search)  : <route>?<search>l<학교이름>
  앞부분(prefix): <route>?base64('<prefix><학교코드>_0_<주차><suffix>')
  꼬리(suffix)  : 2026-11-05부터 붙는 '_enc2' 같은 끝부분

파이썬 기본 기능만 씁니다 (추가 설치 없음). 크롬 원격 디버깅(CDP)으로 브라우저를 움직입니다.

사용법
  python comci_capture.py                       결과를 화면에 표시
  python comci_capture.py --out comci-boot.json 결과를 파일로 저장 (값이 바뀌었을 때만 capturedAt 갱신)
  python comci_capture.py --school 연제중        검색할 학교 이름 (기본: 연제중)
  환경변수 CHROME_PATH 로 브라우저 실행 파일을, COMCI_HOST 로 컴시간 주소를 바꿀 수 있습니다.
"""
import base64
import json
import os
import random
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

COMCI_HOST = os.environ.get("COMCI_HOST", "http://comci.net:4082")
DEFAULT_SCHOOL = "연제중"


# ───────────────────── 브라우저 찾기 ─────────────────────
def find_browser():
    env = os.environ.get("CHROME_PATH")
    if env and os.path.exists(env):
        return env
    cands = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")):
            if not base:
                continue
            cands += [os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"),
                      os.path.join(base, "Google", "Chrome", "Application", "chrome.exe")]
    else:
        for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
            p = shutil.which(name)
            if p:
                cands.append(p)
    for c in cands:
        if c and os.path.exists(c):
            return c
    return None


# ───────────────────── 아주 작은 WebSocket 클라이언트 (CDP용) ─────────────────────
class WS:
    def __init__(self, url, timeout=15):
        u = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((u.hostname, u.port or 80), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = ("GET %s HTTP/1.1\r\nHost: %s:%s\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               "Sec-WebSocket-Key: %s\r\nSec-WebSocket-Version: 13\r\n\r\n") % (path, u.hostname, u.port, key)
        self.sock.sendall(req.encode())
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(1024)
            if not chunk:
                raise IOError("WebSocket 연결이 끊겼습니다")
            head += chunk
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise IOError("WebSocket 연결 실패: " + head[:80].decode("latin1"))
        self.buf = head.split(b"\r\n\r\n", 1)[1]

    def send(self, text):
        data = text.encode("utf-8")
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr.append(0x80 | 126); hdr += struct.pack(">H", n)
        else:
            hdr.append(0x80 | 127); hdr += struct.pack(">Q", n)
        mask = os.urandom(4)
        hdr += mask
        self.sock.sendall(bytes(hdr) + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise IOError("WebSocket 연결이 끊겼습니다")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def recv(self):
        msg = b""
        while True:
            b1, b2 = self._read(2)
            op, fin = b1 & 0x0F, b1 & 0x80
            n = b2 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            mask = self._read(4) if b2 & 0x80 else None
            data = self._read(n)
            if mask:
                data = bytes(b ^ mask[i % 4] for i, b in enumerate(data))
            if op == 0x8:
                raise IOError("WebSocket 닫힘")
            if op in (0x9, 0xA):          # ping/pong 은 건너뜀
                continue
            msg += data
            if fin:
                return msg.decode("utf-8", "replace")

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class CDP:
    """크롬 원격 디버깅 명령 보내기 + 네트워크 요청 주소 모으기"""

    def __init__(self, ws):
        self.ws = ws
        self.n = 0
        self.urls = []
        self.events = []

    def _handle(self, m):
        if m.get("method") == "Network.requestWillBeSent":
            self.urls.append(m["params"]["request"]["url"])
        elif m.get("method"):
            self.events.append(m["method"])

    def call(self, method, params=None, timeout=15):
        self.n += 1
        my = self.n
        self.ws.send(json.dumps({"id": my, "method": method, "params": params or {}}))
        end = time.time() + timeout
        while time.time() < end:
            m = json.loads(self.ws.recv())
            if m.get("id") == my:
                if "error" in m:
                    raise IOError("%s 실패: %s" % (method, m["error"].get("message")))
                return m.get("result", {})
            self._handle(m)
        raise IOError(method + " 응답 시간 초과")

    def pump(self, seconds):
        """seconds 동안 들어오는 이벤트(요청 주소)를 모은다"""
        end = time.time() + seconds
        self.ws.sock.settimeout(0.3)
        try:
            while time.time() < end:
                try:
                    self._handle(json.loads(self.ws.recv()))
                except socket.timeout:
                    pass
        finally:
            self.ws.sock.settimeout(15)

    def js(self, expr):
        r = self.call("Runtime.evaluate", {"expression": expr, "returnByValue": True, "awaitPromise": True})
        return (r.get("result") or {}).get("value")

    def click(self, x, y):
        for t in ("mouseMoved", "mousePressed", "mouseReleased"):
            p = {"type": t, "x": x, "y": y}
            if t != "mouseMoved":
                p.update({"button": "left", "clickCount": 1})
            self.call("Input.dispatchMouseEvent", p)

    def enter(self):
        for t in ("keyDown", "keyUp"):
            p = {"type": t, "key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "nativeVirtualKeyCode": 13}
            if t == "keyDown":
                p["text"] = "\r"
            self.call("Input.dispatchKeyEvent", p)


# ───────────────────── 요청 주소에서 접속 정보 뽑기 ─────────────────────
def analyze(urls, host=COMCI_HOST):
    out = {}
    hp = re.escape(urllib.parse.urlparse(host).netloc)
    for u in urls:
        m = re.match(r"^https?://" + hp + r"/(\d+)\?(.+)$", u)
        if not m:
            continue
        route, q = m.group(1), m.group(2)
        s = re.match(r"^(\d+l)", q)
        if s:
            out["route"], out["search"] = route, s.group(1)
            continue
        try:
            raw = base64.b64decode(urllib.parse.unquote(q) + "===").decode("latin1")
        except Exception:
            continue
        p = re.match(r"^(\d*_)(\d+)_0_\d+(_\w+)?$", raw)
        if p:
            out["route"], out["prefix"], out["suffix"] = route, p.group(1), p.group(3) or ""
            out["schoolCode"] = p.group(2)
    return out


# 화면에 보이는 첫 글자 입력칸 좌표
JS_INPUT = r"""(() => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const list = [...document.querySelectorAll('input')].filter(e => (!e.type || e.type === 'text' || e.type === 'search') && vis(e));
  if (!list.length) return null;
  const e = list[0]; e.scrollIntoView({block: 'center'}); e.focus(); e.value = '';
  const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2];
})()"""

# 입력칸 근처의 '검색' 버튼 좌표
JS_BUTTON = r"""(() => {
  const vis = e => { const r = e.getBoundingClientRect(); return r.width > 2 && r.height > 2; };
  const list = [...document.querySelectorAll('button,input[type=button],input[type=submit],a,img,span')]
    .filter(e => vis(e) && /검색|찾기|search/i.test((e.innerText || e.value || e.alt || e.title || '')));
  if (!list.length) return null;
  const r = list[0].getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2];
})()"""

# 학교 이름이 들어 있는 가장 작은(안쪽) 요소들의 좌표
JS_SCHOOL = r"""((name) => {
  const vis = e => { const r = e.getBoundingClientRect(); const s = getComputedStyle(e);
    return r.width > 2 && r.height > 2 && s.visibility !== 'hidden' && s.display !== 'none'; };
  const all = [...document.querySelectorAll('body *')].filter(e =>
    e.tagName !== 'INPUT' && e.tagName !== 'SCRIPT' && vis(e) && (e.innerText || '').indexOf(name) >= 0 && !/검색/.test(e.innerText || ''));
  const leaf = all.filter(e => ![...e.children].some(c => (c.innerText || '').indexOf(name) >= 0));
  return leaf.slice(0, 8).map(e => { e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect(); return [r.x + r.width / 2, r.y + r.height / 2]; });
})"""


def capture(school=DEFAULT_SCHOOL, host=COMCI_HOST, browser=None, log=print, keep_open=False):
    browser = browser or find_browser()
    if not browser:
        raise RuntimeError("크롬이나 엣지를 찾지 못했습니다.")
    port = random.randint(30000, 39000)
    profile = tempfile.mkdtemp(prefix="comci_cdp_")
    args = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--remote-debugging-port=%d" % port, "--user-data-dir=" + profile,
            "--window-size=1280,900", "--lang=ko-KR"]
    args += [x for x in os.environ.get("COMCI_CHROME_ARGS", "").split() if x]   # 필요하면 브라우저 옵션 추가
    args.append("about:blank")
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0:
        args.insert(1, "--no-sandbox")
    flags = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
    proc = subprocess.Popen(args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
    ws = None
    try:
        page = None
        for _ in range(60):
            try:
                with urllib.request.urlopen("http://127.0.0.1:%d/json/list" % port, timeout=2) as r:
                    pages = [p for p in json.load(r) if p.get("type") == "page" and p.get("webSocketDebuggerUrl")]
                if pages:
                    page = pages[0]
                    break
            except Exception:
                pass
            time.sleep(0.25)
        if not page:
            raise RuntimeError("브라우저 원격 연결을 열지 못했습니다.")
        ws = WS(page["webSocketDebuggerUrl"])
        cdp = CDP(ws)
        cdp.call("Network.enable")
        cdp.call("Page.enable")
        cdp.call("Page.navigate", {"url": host + "/st"})
        cdp.pump(4)
        for _ in range(10):                                  # 입력칸이 나올 때까지 (최대 5초 더)
            if cdp.js(JS_INPUT):
                break
            cdp.pump(0.5)
        pos = cdp.js(JS_INPUT)
        if not pos:
            raise RuntimeError("컴시간 화면에서 학교 검색칸을 찾지 못했습니다.")
        cdp.click(pos[0], pos[1])
        cdp.call("Input.insertText", {"text": school})
        cdp.enter()
        cdp.pump(3)
        if not analyze(cdp.urls, host).get("search"):
            btn = cdp.js(JS_BUTTON)
            if btn:
                cdp.click(btn[0], btn[1])
                cdp.pump(3)
        for p in (cdp.js("(" + JS_SCHOOL + ")(" + json.dumps(school) + ")") or []):
            if analyze(cdp.urls, host).get("prefix") is not None:
                break
            cdp.click(p[0], p[1])
            cdp.pump(3)
        res = analyze(cdp.urls, host)
        if not res.get("route") or not res.get("search") or res.get("prefix") is None:
            seen = [u for u in cdp.urls if urllib.parse.urlparse(u).netloc == urllib.parse.urlparse(host).netloc]
            raise RuntimeError("접속 정보를 다 찾지 못했습니다. 찾은 것: %s / 컴시간 요청 %d건" % (json.dumps(res, ensure_ascii=False), len(seen)))
        return res
    finally:
        if ws:
            ws.close()
        try:
            proc.terminate()
            proc.wait(5)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass
        shutil.rmtree(profile, ignore_errors=True)


def save_if_changed(path, res):
    """값이 바뀌었을 때만 파일을 고친다 (같으면 그대로 → 저장소에 커밋이 쌓이지 않음)"""
    prev = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    keys = ("route", "search", "prefix", "suffix")
    if all(str(prev.get(k, "")) == str(res.get(k, "")) for k in keys) and prev.get("capturedAt"):
        return False
    out = {k: res.get(k, "") for k in keys}
    out["capturedAt"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    out["note"] = "컴시간 접속 정보 (comci_capture.py 가 실제 브라우저로 알아낸 값)"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
        f.write("\n")
    return True


def main():
    a = sys.argv[1:]
    school = a[a.index("--school") + 1] if "--school" in a else DEFAULT_SCHOOL
    try:
        res = capture(school)
    except Exception as e:
        print("[실패] " + str(e))
        sys.exit(1)
    print(json.dumps(res, ensure_ascii=False))
    if "--out" in a:
        path = a[a.index("--out") + 1]
        print("파일 갱신" if save_if_changed(path, res) else "값이 같아 그대로 둠")


if __name__ == "__main__":
    main()
