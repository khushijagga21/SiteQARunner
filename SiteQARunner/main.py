# main.py — Site QA Runner (Authorized use only)
# Features:
# - undetected-chromedriver with proxy support
# - QA tagging (toggle ON/OFF; OFF by default)
# - Referrer -> dwell -> same-tab navigation (preserves Referer)
# - Random middle-of-page hover+click (human-ish)
# - Post-click dwell with tiny interactions
# - NEW: Human scroll cycles: pause 3–5s, then smooth scroll 5–10s

import os, random, threading, time, tempfile, json
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode
from string import Template

import tkinter as tk
from tkinter import filedialog, messagebox

# ---------- UC / Selenium ----------
import undetected_chromedriver as uc
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.common.action_chains import ActionChains

# ---------- Visit config ----------
from VisitConfig import VisitConfig   # keep VisitConfig.py next to this file

# ---------- proxy pool ----------
from queue import Queue, Empty

# ===================== Device Profiles =====================

class DeviceProfile:
    def __init__(self, name: str, user_agent: str, viewport: Tuple[int, int], timezone: str, platform: str):
        self.name, self.user_agent, self.viewport, self.timezone, self.platform = \
            name, user_agent, viewport, timezone, platform

DESKTOP_PROFILES: List[DeviceProfile] = [
    DeviceProfile("Win Chrome",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        (1280, 720), "America/New_York", "Win32"),
    DeviceProfile("Mac Safari",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Safari/605.1.15",
        (1440, 900), "Europe/London", "MacIntel"),
]
MOBILE_PROFILES: List[DeviceProfile] = [
    DeviceProfile("Android Chrome",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
        (360, 740), "Asia/Kolkata", "Linux armv8l"),
    DeviceProfile("iPhone Safari",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.2 Mobile/15E148 Safari/604.1",
        (390, 844), "America/Los_Angeles", "iPhone"),
]
ALL_PROFILES = DESKTOP_PROFILES + MOBILE_PROFILES
SMALL_WINDOW = (400, 300)

# ===================== Helpers =====================

def parse_proxy_line(line: str) -> Optional[dict]:
    """Accepts: ip:port  or  ip:port:user:pass . Returns dict or None."""
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    parts = [p.strip() for p in line.split(":")]
    if len(parts) == 2:
        return {"host": parts[0], "port": parts[1], "user": None, "password": None}
    if len(parts) == 4:
        return {"host": parts[0], "port": parts[1], "user": parts[2], "password": parts[3]}
    return None

def create_proxy_auth_extension(host, port, user, pw) -> str:
    """Create a temporary MV3 extension for proxy + basic auth (Template avoids f-string brace issues)."""
    manifest = {
        "name": "Proxy Auth",
        "version": "1.0.0",
        "manifest_version": 3,
        "permissions": ["proxy", "storage", "webRequest", "webRequestBlocking"],
        "host_permissions": ["<all_urls>"],
        "background": {"service_worker": "background.js"}
    }

    bg_tpl = Template(r"""
chrome.runtime.onInstalled.addListener(() => {
  chrome.proxy.settings.set({
    value: {
      mode: "fixed_servers",
      rules: {
        singleProxy: { scheme: "http", host: "$HOST", port: parseInt("$PORT") },
        bypassList: ["localhost", "127.0.0.1"]
      }
    },
    scope: "regular"
  });
});

chrome.webRequest.onAuthRequired.addListener(
  function(details) {
    return { authCredentials: { username: "$USER", password: "$PW" } };
  },
  { urls: ["<all_urls>"] },
  ["blocking"]
);
""")
    bg = bg_tpl.substitute(HOST=host, PORT=str(port), USER=user or "", PW=pw or "")

    tmp = tempfile.mkdtemp(prefix="proxy_ext_")
    with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f)
    with open(os.path.join(tmp, "background.js"), "w", encoding="utf-8") as f:
        f.write(bg)
    return tmp

def add_qa_tags(url: str, enabled: bool, qa_value: str, utm_source: str, utm_medium: str, utm_campaign: str) -> str:
    if not enabled: return url
    u = urlparse(url); q = dict(parse_qsl(u.query, keep_blank_values=True))
    q["qa_runner"] = qa_value or "1"
    q.setdefault("utm_source", utm_source or "qa-runner")
    q.setdefault("utm_medium", utm_medium or "test")
    q.setdefault("utm_campaign", utm_campaign or "qa")
    return urlunparse((u.scheme,u.netloc,u.path,u.params,urlencode(q,doseq=True),u.fragment))

def wait_for_full_load(driver, timeout=60):
    WebDriverWait(driver, timeout).until(lambda d: d.execute_script("return document.readyState")=="complete")

def pick_clickable(driver):
    els = driver.find_elements(By.XPATH, "//a[@href] | //button | //*[@role='button']")
    random.shuffle(els)
    seen=set()
    for el in els:
        try:
            if not el.is_displayed(): continue
            key=(el.tag_name,(el.text or "").strip()[:50])
            if key in seen: continue
            seen.add(key)
            sz=el.size
            if sz.get("width",0)>5 and sz.get("height",0)>5: return el
        except Exception: pass
    return None

def navigate_with_referrer(driver, target_url: str):
    """Same-tab navigation via hidden <a>, keeps Referer."""
    js = r"""
        const url = arguments[0];
        if (!document.body) {
          const body = document.createElement('body');
          document.documentElement.appendChild(body);
        }
        const a = document.createElement('a');
        a.href = url;
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
    """
    try:
        driver.execute_script(js, target_url)
    except Exception:
        driver.execute_script("window.location.href = arguments[0];", target_url)

# ---------- Middle-of-page hover + click ----------

def pick_middle_clickable(driver):
    js = r"""
    const vw = window.innerWidth, vh = window.innerHeight;
    const middleX = [vw*0.20, vw*0.80];
    const middleY = [vh*0.30, vh*0.70];

    const nodes = Array.from(document.querySelectorAll(
      'a[href], button, [role="button"], input[type="button"], input[type="submit"]'
    ));

    const visible = nodes.filter(el => {
      const r = el.getBoundingClientRect();
      if (!r || r.width < 40 || r.height < 15) return false;
      if (el.offsetParent === null) return false;
      const cs = window.getComputedStyle(el);
      if (cs.visibility === 'hidden' || cs.opacity === '0') return false;
      const isFixed = cs.position === 'fixed';
      if (isFixed && (r.top <= 0 || r.bottom >= vh)) return false;

      const cx = r.left + r.width/2;
      const cy = r.top + r.height/2;
      const inMidX = cx > middleX[0] && cx < middleX[1];
      const inMidY = cy > middleY[0] && cy < middleY[1];
      return inMidX && inMidY;
    });

    for (let i = visible.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1));
      [visible[i], visible[j]] = [visible[j], visible[i]];
    }
    return visible[0] || null;
    """
    try:
        return driver.execute_script(js)
    except Exception:
        return None

def random_hover_and_click_middle(driver, logger=None):
    el = pick_middle_clickable(driver)
    if not el:
        if logger: logger("No middle-area clickable element found (fallback will be used)")
        return False

    rect = driver.execute_script(
        "const r = arguments[0].getBoundingClientRect(); return {w:r.width,h:r.height};", el
    )
    try:
        jitter_x = random.randint(-min(8, int(rect['w']//6)), min(8, int(rect['w']//6)))
        jitter_y = random.randint(-min(6, int(rect['h']//6)), min(6, int(rect['h']//6)))
    except Exception:
        jitter_x, jitter_y = 0, 0

    try:
        actions = ActionChains(driver)
        actions.move_to_element_with_offset(el, jitter_x, jitter_y)\
               .pause(random.uniform(0.25, 0.9))\
               .click()\
               .perform()
        if logger: logger(f"Middle click performed (jitter {jitter_x},{jitter_y})")
        
        time.sleep(random.uniform(0.4, 1.1))
        return True
    except Exception as e:
        if logger: logger(f"Middle click failed (ignored): {e!r}")
        return False

# ---------- NEW: Human-like scrolling ----------

def smooth_scroll_session(driver, min_seconds=5, max_seconds=10, min_px=800, max_px=2000):
    """
    Smoothly scrolls down over a single continuous session.
    Duration: random between min_seconds..max_seconds
    Distance: random between min_px..max_px (capped by doc height)
    """
    duration = random.randint(int(min_seconds*1000), int(max_seconds*1000))  # ms
    total = random.randint(min_px, max_px)
    js = r"""
        const duration = arguments[0];
        let total = arguments[1];
        const startY = window.scrollY || document.documentElement.scrollTop || 0;
        const maxY = Math.max(0, (document.documentElement.scrollHeight || document.body.scrollHeight) - window.innerHeight);
        // Cap total so we don't overshoot the page
        total = Math.min(total, Math.max(0, maxY - startY));
        let start = null, last = 0;
        function step(ts){
            if(!start) start = ts;
            const prog = ts - start;
            const y = Math.min((prog / duration) * total, total);
            window.scrollBy(0, y - last);
            last = y;
            if(prog < duration){ requestAnimationFrame(step); }
        }
        requestAnimationFrame(step);
    """
    driver.execute_script(js, duration, total)
    # Sleep in small chunks to allow stop checks elsewhere if needed
    slept = 0.0
    total_sleep = duration / 1000.0
    while slept < total_sleep:
        time.sleep(min(0.5, total_sleep - slept))
        slept += 0.5

def human_scroll_cycle(driver, logger=None,
                       pause_range=(3,5), session_range=(5,10)):
    """
    One human-ish scroll cycle:
      - wait 3–5s (random)
      - do one smooth scroll session for 5–10s (random)
    """
    pause_s = random.uniform(*pause_range)
    if logger: logger(f"Pause {pause_s:.1f}s before smooth scroll")
    # pause but allow UI to remain responsive
    waited = 0.0
    while waited < pause_s:
        time.sleep(min(0.5, pause_s - waited))
        waited += 0.5

    dur = random.randint(*tuple(int(x) for x in session_range))
    if logger: logger(f"Smooth scroll for ~{dur}s")
    smooth_scroll_session(driver, min_seconds=dur, max_seconds=dur)

# ===================== UC Chrome Builder =====================

def build_driver_uc(profile: DeviceProfile,
                    proxy: Optional[dict],
                    small_window: Tuple[int,int] = SMALL_WINDOW,
                    minimize: bool = True) -> uc.Chrome:
    opts = uc.ChromeOptions()
    opts.add_argument(f"--window-size={small_window[0]},{small_window[1]}")
    if minimize:
        opts.add_argument("--start-minimized")

    # Stability flags
    opts.add_argument("--disable-gpu")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-notifications")
    opts.add_argument("--disable-infobars")
    opts.add_argument("--no-default-browser-check")
    opts.add_argument("--no-first-run")
    opts.add_argument("--disable-features=RendererCodeIntegrity,IsolateOrigins,site-per-process")
    opts.add_argument("--disable-renderer-backgrounding")
    opts.add_argument("--no-sandbox")

    opts.add_argument(f"--user-agent={profile.user_agent}")

    user_dir = tempfile.mkdtemp(prefix="qa_uc_profile_")
    opts.add_argument(f"--user-data-dir={user_dir}")
    opts.add_argument("--profile-directory=Default")

    ext_dir = None
    if proxy:
        if proxy.get("user") and proxy.get("password"):
            ext_dir = create_proxy_auth_extension(proxy["host"], proxy["port"], proxy["user"], proxy["password"])
            opts.add_argument(f"--load-extension={ext_dir}")
        else:
            opts.add_argument(f"--proxy-server=http://{proxy['host']}:{proxy['port']}")

    driver = uc.Chrome(options=opts, suppress_welcome=True, log_level=3)

    import shutil
    orig_quit = driver.quit
    def wrapped_quit():
        try:
            orig_quit()
        finally:
            shutil.rmtree(user_dir, ignore_errors=True)
            if ext_dir:
                shutil.rmtree(ext_dir, ignore_errors=True)
    driver.quit = wrapped_quit

    try:
        driver.execute_cdp_cmd("Emulation.setUserAgentOverride",
                               {"userAgent": profile.user_agent, "platform": profile.platform})
    except Exception: pass
    try:
        driver.execute_cdp_cmd("Emulation.setTimezoneOverride",
                               {"timezoneId": profile.timezone})
    except Exception: pass

    return driver

def safe_launch_uc(profile, proxy, small_window=SMALL_WINDOW, minimize=True, retries=2, delay=2.0):
    last_err = None
    for _ in range(retries + 1):
        try:
            return build_driver_uc(profile, proxy, small_window, minimize)
        except Exception as e:
            last_err = e
            time.sleep(delay)
    raise last_err

# ===================== Worker Thread =====================

class VisitWorker(threading.Thread):
    def __init__(self, idx: int, app, cfg: VisitConfig, proxies: List[dict], qa_mark: bool,
                 qa_value: str, utm_source: str, utm_medium: str, utm_campaign: str,
                 stop_event: threading.Event, diagnostics: bool,
                 proxy_pool: Optional[Queue] = None, one_per_proxy: bool = False):
        super().__init__(daemon=True)
        self.idx = idx
        self.app = app
        self.cfg = cfg
        self.proxies = proxies or []
        self.qa_mark = qa_mark
        self.qa_value = qa_value
        self.utm_source = utm_source
        self.utm_medium = utm_medium
        self.utm_campaign = utm_campaign
        self.stop_event = stop_event
        self.diagnostics = diagnostics
        self.proxy_pool = proxy_pool
        self.one_per_proxy = one_per_proxy
        self._proxy_i = 0

        self.effective_url = add_qa_tags(cfg.target_url, qa_mark, qa_value, utm_source, utm_medium, utm_campaign)
        self.base_netloc = urlparse(self.effective_url).netloc.lower()

    def next_proxy(self) -> Optional[dict]:
        if self.proxy_pool:
            try:
                p = self.proxy_pool.get_nowait()
                if not self.one_per_proxy:
                    self.proxy_pool.put(p)
                return p
            except Empty:
                return None
        if not self.proxies: return None
        p = self.proxies[self._proxy_i % len(self.proxies)]
        self._proxy_i += 1
        return p

    def next_profile(self) -> DeviceProfile:
        return random.choice(ALL_PROFILES)

    def run(self):
        for visit_num in range(1, self.cfg.visits_per_worker + 1):
            if self.stop_event.is_set():
                break

            proxy = self.next_proxy()
            if self.one_per_proxy and self.proxy_pool and proxy is None:
                self.app.log(f"[Worker {self.idx}] No proxy left; stopping (one-per-proxy).")
                break

            profile = self.next_profile()
            self.app.log(f"[Worker {self.idx}] Visit #{visit_num} | Proxy: {proxy if proxy else 'NONE'} | Profile: {profile.name}")

            driver = None
            start_ts = time.time()
            visit_started = False
            try:
                self.app.log(f"[Worker {self.idx}] Launching Chrome…")
                driver = safe_launch_uc(profile, proxy, SMALL_WINDOW, minimize=not self.diagnostics)
                self.app.log(f"[Worker {self.idx}] Driver ready")

                # 1) Referrer
                driver.get(self.cfg.referrer)
                self.app.log(f"[Worker {self.idx}] Referrer opened: {self.cfg.referrer}")

                # Dwell on referrer
                pre_wait_min = max(0.0, float(self.app.pre_min_var.get()))
                pre_wait_max = max(pre_wait_min, float(self.app.pre_max_var.get()))
                dwell = random.uniform(pre_wait_min, pre_wait_max)
                self.app.log(f"[Worker {self.idx}] Waiting on referrer for {dwell:.1f}s")
                t0 = time.time()
                while time.time() - t0 < dwell:
                    if self.stop_event.is_set(): break
                    time.sleep(0.25)

                # 2) Navigate FROM referrer -> target
                navigate_with_referrer(driver, self.effective_url)
                wait_for_full_load(driver, timeout=60)
                current = driver.current_url
                self.app.log(f"[Worker {self.idx}] Target loaded: {current}")

                if self.base_netloc and self.base_netloc not in urlparse(current).netloc.lower():
                    self.app.log(f"[Worker {self.idx}] Cross-domain hop blocked: {current}")
                    continue

                visit_started = True

                # 3) First interactions: do one human scroll cycle, then try middle click
                human_scroll_cycle(driver, logger=lambda m: self.app.log(f"[Worker {self.idx}] {m}"))
                clicked = random_hover_and_click_middle(
                    driver,
                    logger=lambda msg: self.app.log(f"[Worker {self.idx}] {msg}")
                )
                if not clicked:
                    el = pick_clickable(driver)
                    if el:
                        try:
                            el.click()
                            self.app.log(f"[Worker {self.idx}] Fallback click performed")
                            time.sleep(random.uniform(0.5, 1.5))
                            clicked = True
                        except Exception as ce:
                            self.app.log(f"[Worker {self.idx}] Fallback click failed (ignored): {ce!r}")

                # 4) Post-click dwell window with human scroll cycles
                if clicked:
                    pc_min = max(1, int(float(self.app.post_click_min_var.get())))
                    pc_max = max(pc_min, int(float(self.app.post_click_max_var.get())))
                    pc_dwell = random.randint(pc_min, pc_max)
                    self.app.log(f"[Worker {self.idx}] Post-click dwell for {pc_dwell}s")
                    tpc_end = time.time() + pc_dwell
                    while time.time() < tpc_end:
                        if self.stop_event.is_set(): break
                        # each cycle blocks for ~ (3–5 + 5–10)s, so check remaining time
                        remaining = tpc_end - time.time()
                        # run a trimmed cycle if time is short
                        if remaining < 4:
                            time.sleep(remaining)
                            break
                        # run one cycle
                        human_scroll_cycle(driver, logger=lambda m: self.app.log(f"[Worker {self.idx}] {m}"))

                # 5) Main dwell with occasional human scroll cycles
                dwell_target = random.randint(self.cfg.min_stay, self.cfg.max_stay)
                self.app.log(f"[Worker {self.idx}] Dwell on target for {dwell_target}s")
                tend = time.time() + dwell_target
                while time.time() < tend:
                    if self.stop_event.is_set(): break
                    # roughly 35% chance to run a scroll cycle if there's enough time left
                    remaining = tend - time.time()
                    if remaining > 9 and random.random() < 0.35:
                        human_scroll_cycle(driver, logger=lambda m: self.app.log(f"[Worker {self.idx}] {m}"))
                    else:
                        # idle tick to keep UI stats fresh
                        elapsed = int(time.time() - start_ts)
                        self.app.update_elapsed(elapsed)
                        time.sleep(1.0)

                self.app.increment_visits()
                self.app.log(f"[Worker {self.idx}] Visit #{visit_num} done")

            except Exception as e:
                import traceback
                self.app.log(f"[Worker {self.idx}] Error: {repr(e)}")
                self.app.log(traceback.format_exc())
                if visit_started:
                    self.app.increment_visits()
                    self.app.log(f"[Worker {self.idx}] Visit #{visit_num} counted after partial dwell (target loaded).")
            finally:
                try:
                    if driver: driver.quit()
                except Exception: pass

        self.app.log(f"[Worker {self.idx}] Finished")

# ===================== GUI App =====================

class SiteQARunnerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Site QA Runner (Authorized use only)")
        self.geometry("960x780")
        self.resizable(False, False)

        self.total_visits = 0
        self.start_ts = None
        self.workers: List[VisitWorker] = []
        self.stop_event = threading.Event()
        self.proxies: List[dict] = []
        self.proxy_pool: Optional[Queue] = None

        # QA toggle default OFF
        self.qa_var = tk.BooleanVar(value=False)

        self.build_ui()

    def build_ui(self):
        row = 0
        tk.Label(self, text="Target URL:").grid(row=row, column=0, sticky="e", padx=6, pady=6)
        self.url_var = tk.StringVar(value="https://example.com/")
        tk.Entry(self, textvariable=self.url_var, width=80).grid(row=row, column=1, columnspan=6, sticky="w", padx=4)
        row += 1

        tk.Label(self, text="Referrer URL:").grid(row=row, column=0, sticky="e")
        self.referrer_var = tk.StringVar(value="https://www.google.com/")
        tk.Entry(self, textvariable=self.referrer_var, width=60).grid(row=row, column=1, columnspan=4, sticky="w", padx=4)
        tk.Label(self, text="Tip: disable 'Diagnostics' for real referrer").grid(row=row, column=5, columnspan=2, sticky="w")
        row += 1

        tk.Label(self, text="Workers (max 3):").grid(row=row, column=0, sticky="e")
        self.workers_var = tk.IntVar(value=1)
        tk.Spinbox(self, from_=1, to=3, textvariable=self.workers_var, width=5).grid(row=row, column=1, sticky="w")

        tk.Label(self, text="Visits/worker:").grid(row=row, column=2, sticky="e")
        self.vpw_var = tk.IntVar(value=1)
        tk.Spinbox(self, from_=1, to=100, textvariable=self.vpw_var, width=5).grid(row=row, column=3, sticky="w")

        tk.Button(self, text="Load Proxy List", command=self.load_proxies).grid(row=row, column=4, sticky="w", padx=4)

        self.start_btn = tk.Button(self, text="Start", bg="#89d089", command=self.on_start)
        self.start_btn.grid(row=row, column=5, sticky="w", padx=4)
        self.stop_btn = tk.Button(self, text="Stop", bg="#f2a3a3", command=self.on_stop)
        self.stop_btn.grid(row=row, column=6, sticky="w", padx=4)
        row += 1

        tk.Label(self, text="Referrer dwell (s):").grid(row=row, column=0, sticky="e")
        self.pre_min_var = tk.DoubleVar(value=10.0)
        self.pre_max_var = tk.DoubleVar(value=10.0)
        tk.Entry(self, textvariable=self.pre_min_var, width=6).grid(row=row, column=1, sticky="w")
        tk.Label(self, text="to").grid(row=row, column=2, sticky="w")
        tk.Entry(self, textvariable=self.pre_max_var, width=6).grid(row=row, column=3, sticky="w")

        tk.Label(self, text="Stay on target (s):").grid(row=row, column=4, sticky="e")
        self.stay_min_var = tk.IntVar(value=60)
        self.stay_max_var = tk.IntVar(value=180)
        tk.Entry(self, textvariable=self.stay_min_var, width=6).grid(row=row, column=5, sticky="w")
        tk.Label(self, text="to").grid(row=row, column=6, sticky="w")
        tk.Entry(self, textvariable=self.stay_max_var, width=6).grid(row=row, column=7, sticky="w")
        row += 1

        # Post-click dwell controls
        tk.Label(self, text="Post-click dwell (s):").grid(row=row, column=0, sticky="e")
        self.post_click_min_var = tk.DoubleVar(value=10.0)
        self.post_click_max_var = tk.DoubleVar(value=20.0)
        tk.Entry(self, textvariable=self.post_click_min_var, width=6).grid(row=row, column=1, sticky="w")
        tk.Label(self, text="to").grid(row=row, column=2, sticky="w")
        tk.Entry(self, textvariable=self.post_click_max_var, width=6).grid(row=row, column=3, sticky="w")
        row += 1

        # QA toggle (OFF by default)
        self.qa_toggle_btn = tk.Button(self, text="QA Tag: OFF", width=12, bg="#e6e6e6",
                                       relief="raised", command=self.toggle_qa)
        self.qa_toggle_btn.grid(row=row, column=0, sticky="w", padx=6, pady=(4,2))

        tk.Label(self, text="qa_runner=").grid(row=row, column=1, sticky="e")
        self.qa_value_var = tk.StringVar(value="1")
        self.qa_value_entry = tk.Entry(self, textvariable=self.qa_value_var, width=6, state="disabled")
        self.qa_value_entry.grid(row=row, column=2, sticky="w")

        tk.Label(self, text="utm_source").grid(row=row, column=3, sticky="e")
        self.utm_source_var = tk.StringVar(value="qa-runner")
        self.utm_source_entry = tk.Entry(self, textvariable=self.utm_source_var, width=12, state="disabled")
        self.utm_source_entry.grid(row=row, column=4, sticky="w")

        tk.Label(self, text="utm_medium").grid(row=row, column=5, sticky="e")
        self.utm_medium_var = tk.StringVar(value="test")
        self.utm_medium_entry = tk.Entry(self, textvariable=self.utm_medium_var, width=12, state="disabled")
        self.utm_medium_entry.grid(row=row, column=6, sticky="w")
        row += 1

        tk.Label(self, text="utm_campaign").grid(row=row, column=0, sticky="e")
        self.utm_campaign_var = tk.StringVar(value="qa")
        self.utm_campaign_entry = tk.Entry(self, textvariable=self.utm_campaign_var, width=12, state="disabled")
        self.utm_campaign_entry.grid(row=row, column=1, sticky="w")

        # Modes
        self.one_per_proxy_var = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="One visit per proxy (use each proxy exactly once)",
                       variable=self.one_per_proxy_var)\
            .grid(row=row, column=4, columnspan=3, sticky="w", padx=6)
        row += 1

        self.diagnostics_var = tk.BooleanVar(value=True)
        tk.Checkbutton(self, text="Diagnostics mode (about:blank referrer, shorter, not minimized)",
                       variable=self.diagnostics_var)\
            .grid(row=row, column=0, columnspan=6, sticky="w", padx=6)
        row += 1

        # Log box
        self.log_box = tk.Text(self, width=112, height=26)
        self.log_box.grid(row=row, column=0, columnspan=8, padx=8, pady=8, sticky="w")
        row += 1

        # Footer stats
        self.stats_var = tk.StringVar(value="Elapsed: 0s | Total visits: 0 | Proxies loaded: 0")
        tk.Label(self, textvariable=self.stats_var, anchor="w").grid(row=row, column=0, columnspan=8, sticky="we", padx=8, pady=(0,8))

    # ---------- QA toggle behavior ----------
    def toggle_qa(self):
        current = self.qa_var.get()
        new_state = not current
        self.qa_var.set(new_state)
        if new_state:
            self.qa_toggle_btn.configure(text="QA Tag: ON", bg="#c8f7c5", relief="sunken")
            self.qa_value_entry.configure(state="normal")
            self.utm_source_entry.configure(state="normal")
            self.utm_medium_entry.configure(state="normal")
            self.utm_campaign_entry.configure(state="normal")
        else:
            self.qa_toggle_btn.configure(text="QA Tag: OFF", bg="#e6e6e6", relief="raised")
            self.qa_value_entry.configure(state="disabled")
            self.utm_source_entry.configure(state="disabled")
            self.utm_medium_entry.configure(state="disabled")
            self.utm_campaign_entry.configure(state="disabled")

    # ---------- Logging / stats ----------
    def log(self, text: str):
        self.log_box.insert(tk.END, text + "\n")
        self.log_box.see(tk.END)
        self.update_idletasks()

    def update_elapsed(self, seconds: int):
        self.stats_var.set(f"Elapsed: {seconds}s | Total visits: {self.total_visits} | Proxies loaded: {len(self.proxies)}")

    def increment_visits(self):
        self.total_visits += 1
        elapsed = int(time.time() - self.start_ts) if self.start_ts else 0
        self.stats_var.set(f"Elapsed: {elapsed}s | Total visits: {self.total_visits} | Proxies loaded: {len(self.proxies)}")

    # ---------- Proxy handling ----------
    def load_proxies(self):
        path = filedialog.askopenfilename(title="Select proxies.txt", filetypes=[("Text files", "*.txt"), ("All files", "*.*")])
        if not path: return
        loaded=[]
        with open(path,"r",encoding="utf-8",errors="ignore") as f:
            for line in f:
                p = parse_proxy_line(line)
                if p: loaded.append(p)
        random.shuffle(loaded)
        self.proxies = loaded
        self.log(f"Loaded {len(self.proxies)} proxies from {os.path.basename(path)}")
        self.update_elapsed(int(time.time()-self.start_ts) if self.start_ts else 0)

    # ---------- Start / Stop ----------
    def on_start(self):
        try:
            url = self.url_var.get().strip()
            if not url:
                messagebox.showerror("Error","Target URL is required.")
                return

            max_workers = max(1, min(3, int(self.workers_var.get())))
            visits_per_worker = max(1, int(self.vpw_var.get()))
            one_per_proxy = bool(self.one_per_proxy_var.get())
            diagnostics = bool(self.diagnostics_var.get())

            ui_referrer = self.referrer_var.get().strip() or "https://www.google.com/"
            chosen_referrer = ("about:blank" if diagnostics else ui_referrer)

            cfg = VisitConfig(
                target_url=url,
                min_pre_wait=float(self.pre_min_var.get() if not diagnostics else 1.0),
                max_pre_wait=float(self.pre_max_var.get() if not diagnostics else 2.0),
                min_stay=int(self.stay_min_var.get() if not diagnostics else 8),
                max_stay=int(self.stay_max_var.get() if not diagnostics else 12),
                referrer=chosen_referrer
            ).clamp()

            self.total_visits = 0
            self.start_ts = time.time()
            self.stop_event.clear()
            self.workers.clear()

            qa_mark  = bool(self.qa_var.get())  # OFF by default
            qa_value = self.qa_value_var.get().strip() or "1"
            utm_src  = self.utm_source_var.get().strip() or "qa-runner"
            utm_med  = self.utm_medium_var.get().strip() or "test"
            utm_camp = self.utm_campaign_var.get().strip() or "qa"

            self.proxy_pool = None
            if self.proxies:
                self.proxy_pool = Queue()
                for p in self.proxies:
                    self.proxy_pool.put(p)

            if one_per_proxy and self.proxies:
                total_visits = len(self.proxies)
                base = total_visits // max_workers
                extra = total_visits % max_workers
                self.log(f"One-per-proxy: scheduling {total_visits} visits across {max_workers} workers")
            else:
                base = visits_per_worker
                extra = 0

            self.log(f"Starting on {url} | referrer={chosen_referrer} | workers={max_workers} | vpw={visits_per_worker} | proxies={len(self.proxies)} | QA Tag={'ON' if qa_mark else 'OFF'}")

            for i in range(1, max_workers+1):
                vpw = (base + (1 if (one_per_proxy and self.proxies and i <= extra) else 0)) if (one_per_proxy and self.proxies) else visits_per_worker
                worker_cfg = VisitConfig(
                    target_url=cfg.target_url,
                    min_pre_wait=cfg.min_pre_wait,
                    max_pre_wait=cfg.max_pre_wait,
                    min_stay=cfg.min_stay,
                    max_stay=cfg.max_stay,
                    referrer=cfg.referrer,
                    visits_per_worker=vpw
                )
                w = VisitWorker(
                    idx=i, app=self, cfg=worker_cfg, proxies=self.proxies,
                    qa_mark=qa_mark, qa_value=qa_value, utm_source=utm_src,
                    utm_medium=utm_med, utm_campaign=utm_camp,
                    stop_event=self.stop_event, diagnostics=diagnostics,
                    proxy_pool=self.proxy_pool, one_per_proxy=self.one_per_proxy_var.get()
                )
                self.workers.append(w)
                w.start()

            self.update_elapsed(0)

        except Exception as e:
            messagebox.showerror("Error", str(e))

    def on_stop(self):
        self.stop_event.set()
        self.log("Stop requested. Waiting for workers to finish…")

# ===================== Main =====================

if __name__ == "__main__":
    # First time: pip install undetected-chromedriver==3.5.5 selenium
    app = SiteQARunnerApp()
    app.mainloop()
    
