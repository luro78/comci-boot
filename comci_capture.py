# -*- coding: utf-8 -*-
"""
컴시간 시간표 수집기 (2026-11-07 이후 암호화 대응)

2026년 11월 7일부터 컴시간 알리미는 시간표·학교 검색 응답을 암호화('enc5:…')해서 보냅니다.
그래서 크롬(또는 엣지)을 화면 없이 띄워 실제로 컴시간을 열고, 학교를 검색·선택한 뒤
페이지가 스스로 푼 직후(JSON.parse)의 시간표를 가로챕니다. 주 선택 목록의 주(보통 이번 주·다음 주)를 모두 받습니다.

결과: { code, name, region, changedAt, weeks: [ { r, label, raw } ] }   (raw = 컴시간이 푼 시간표 원본)

파이썬 기본 기능만 씁니다 (추가 설치 없음). 크롬 원격 디버깅(CDP)으로 브라우저를 움직입니다.

사용법
  python comci_capture.py --school 연제중                     결과 요약을 화면에 표시
  python comci_capture.py --school 연제중 --out timetable.json 결과를 파일로 저장 (시간표가 바뀌었을 때만 고침)
  python comci_capture.py --school 연제중학교 --code 12345      같은 이름 학교가 여럿이면 학교 코드로 고름
  환경변수 CHROME_PATH 로 브라우저 실행 파일을, COMCI_HOST 로 컴시간 주소를, COMCI_CHROME_ARGS 로 브라우저 옵션을 바꿀 수 있습니다.
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


# ───────────────────── 페이지에 심는 코드 ─────────────────────
# 페이지 스크립트보다 먼저 실행되어 JSON.parse 를 감싼다 → 페이지가 푼 시간표·검색 결과를 모아 둔다
JS_INIT = r"""(() => {
  window.__comci = [];
  const orig = JSON.parse;
  JSON.parse = function (t, r) {
    const v = orig.call(this, t, r);
    try { if (v && typeof v === 'object' && (v['학급수'] || v['학교검색'])) window.__comci.push(v); } catch (e) {}
    return v;
  };
})();"""

JS_TAKE = "(() => (window.__comci || []).splice(0))()"

# 검색 결과에서 onclick="sc_disp(코드)" 항목들 (코드는 여기서만 진짜 — 검색 결과 숫자는 가짜)
JS_SCHOOLS = r"""(() => [...document.querySelectorAll('[onclick]')]
  .filter(e => /sc_disp\(\s*\d+/.test(e.getAttribute('onclick') || ''))
  .map(e => { e.scrollIntoView({block: 'center'}); const r = e.getBoundingClientRect();
    return { code: (e.getAttribute('onclick').match(/sc_disp\(\s*(\d+)/) || [])[1], text: (e.innerText || '').trim(),
             x: r.x + r.width / 2, y: r.y + r.height / 2 }; }))()"""

JS_CLICK_CODE = r"""((code) => { const e = [...document.querySelectorAll('[onclick]')]
  .find(x => new RegExp('sc_disp\\(\\s*' + code + '\\b').test(x.getAttribute('onclick') || ''));
  if (!e) return false; e.click(); return true; })"""

# 주 선택 목록 (option 글자에 '~' 가 있는 select)
JS_WEEKS = r"""(() => { const s = [...document.querySelectorAll('select')].find(s => [...s.options].some(o => /~/.test(o.text)));
  if (!s) return null; return { sel: s.selectedIndex, texts: [...s.options].map(o => o.text.trim()) }; })()"""

JS_PICK_WEEK = r"""((i) => { const s = [...document.querySelectorAll('select')].find(s => [...s.options].some(o => /~/.test(o.text)));
  if (!s) return null; s.selectedIndex = i; s.dispatchEvent(new Event('change', {bubbles: true}));
  return s.options[i] ? s.options[i].text.trim() : ''; })"""


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

def _week_of(raw, label, fallback):
    for row in raw.get("일자자료") or []:
        try:
            if str(row[1]).strip() == str(label).strip():
                return int(row[0])
        except Exception:
            pass
    return fallback


def capture(school=DEFAULT_SCHOOL, host=None, browser=None, log=print, code=None, region=None):
    """학교를 검색·선택해 주별 해독된 시간표를 모은다 → {code, name, region, weeks}"""
    host = host or COMCI_HOST
    browser = browser or find_browser()
    if not browser:
        raise RuntimeError("크롬이나 엣지를 찾지 못했습니다.")
    port = random.randint(30000, 39000)
    profile = tempfile.mkdtemp(prefix="comci_cdp_")
    args = [browser, "--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
            "--disable-extensions", "--remote-debugging-port=%d" % port, "--user-data-dir=" + profile,
            "--window-size=1280,900", "--lang=ko-KR"]
    args += [x for x in os.environ.get("COMCI_CHROME_ARGS", "").split() if x]
    args.append("about:blank")
    if os.name != "nt" and hasattr(os, "geteuid") and os.geteuid() == 0 and "--no-sandbox" not in args:
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
        cdp.call("Page.addScriptToEvaluateOnNewDocument", {"source": JS_INIT})
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
        found = cdp.js(JS_SCHOOLS) or []
        if not found:
            btn = cdp.js(JS_BUTTON)
            if btn:
                cdp.click(btn[0], btn[1])
                cdp.pump(3)
                found = cdp.js(JS_SCHOOLS) or []
        decoded = [v for v in (cdp.js(JS_TAKE) or []) if isinstance(v, dict) and v.get("학교검색")]
        regions = {}
        for v in decoded:
            for row in v.get("학교검색") or []:
                if isinstance(row, list) and len(row) >= 3:
                    regions.setdefault(str(row[2]).strip(), str(row[1]).strip())
        if not found:
            raise RuntimeError("컴시간에서 '%s'(으)로 검색한 결과가 없습니다." % school)
        if code:
            hits = [f for f in found if str(f.get("code")) == str(code)]
        else:
            hits = [f for f in found if f.get("text") == school] or [f for f in found if school in (f.get("text") or "")]
            if region:
                hits = [f for f in hits if region in regions.get(f.get("text"), "")] or hits
        if not hits:
            raise RuntimeError("검색 결과에 맞는 학교가 없습니다. 찾은 학교: " + ", ".join("%s(%s)" % (f.get("text"), f.get("code")) for f in found[:8]))
        hit = hits[0]
        if len(hits) > 1:
            log("같은 이름 학교가 %d곳이라 첫 번째(%s, 코드 %s)를 씁니다. --code 로 고를 수 있습니다." % (len(hits), hit["text"], hit["code"]))
        cdp.click(hit["x"], hit["y"])
        cdp.pump(3)
        raws = [v for v in (cdp.js(JS_TAKE) or []) if isinstance(v, dict) and v.get("학급수")]
        if not raws:                                         # 마우스 클릭이 안 먹으면 직접 누름
            cdp.js("(" + JS_CLICK_CODE + ")(" + json.dumps(str(hit["code"])) + ")")
            cdp.pump(3)
            raws = [v for v in (cdp.js(JS_TAKE) or []) if isinstance(v, dict) and v.get("학급수")]
        if not raws:
            raise RuntimeError("학교를 골랐지만 시간표를 가로채지 못했습니다 (컴시간 화면이 바뀌었을 수 있음).")
        weeks = {}
        info = cdp.js(JS_WEEKS)
        first_label = info["texts"][info["sel"]] if info and 0 <= info["sel"] < len(info["texts"]) else ""
        r0 = _week_of(raws[-1], first_label, 1)
        weeks[r0] = {"r": r0, "label": first_label, "raw": raws[-1]}
        if info:
            for i, label in enumerate(info["texts"]):
                if i == info["sel"]:
                    continue
                cdp.js("(" + JS_PICK_WEEK + ")(%d)" % i)
                cdp.pump(2.5)
                got = [v for v in (cdp.js(JS_TAKE) or []) if isinstance(v, dict) and v.get("학급수")]
                if not got:
                    cdp.pump(1.5)
                    got = [v for v in (cdp.js(JS_TAKE) or []) if isinstance(v, dict) and v.get("학급수")]
                if got:
                    r = _week_of(got[-1], label, i + 1)
                    weeks.setdefault(r, {"r": r, "label": label, "raw": got[-1]})
        name = hit.get("text") or school
        return {"code": int(hit["code"]), "name": name, "region": regions.get(name, ""),
                "weeks": [weeks[k] for k in sorted(weeks)]}
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
    """시간표가 바뀌었을 때만 파일을 고친다 (같으면 그대로 → 저장소에 커밋이 쌓이지 않음)"""
    prev = {}
    try:
        with open(path, encoding="utf-8") as f:
            prev = json.load(f)
    except Exception:
        pass
    same = (prev.get("code") == res["code"] and prev.get("name") == res["name"]
            and json.dumps(prev.get("weeks"), ensure_ascii=False, sort_keys=True) == json.dumps(res["weeks"], ensure_ascii=False, sort_keys=True))
    if same and prev.get("changedAt"):
        return False
    out = {"code": res["code"], "name": res["name"], "region": res.get("region", ""),
           "changedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
           "note": "컴시간 시간표 (comci_capture.py 가 실제 브라우저로 가로챈, 컴시간이 푼 원본)",
           "weeks": res["weeks"]}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        f.write("\n")
    return True


def summary(res):
    return "%s(%s) %s" % (res["name"], res["code"], ", ".join("%d주 %s" % (w["r"], w["label"]) for w in res["weeks"]))


def main():
    a = sys.argv[1:]
    school = a[a.index("--school") + 1] if "--school" in a else DEFAULT_SCHOOL
    code = a[a.index("--code") + 1] if "--code" in a else None
    region = a[a.index("--region") + 1] if "--region" in a else None
    try:
        res = capture(school, code=code or None, region=region or None)
    except Exception as e:
        print("[실패] " + str(e))
        sys.exit(1)
    print(summary(res))
    if "--out" in a:
        path = a[a.index("--out") + 1]
        print("파일 갱신" if save_if_changed(path, res) else "시간표가 같아 그대로 둠")


if __name__ == "__main__":
    main()
