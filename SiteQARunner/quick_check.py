print("BOOT: quick_check starting")  # debug so you always see it run

import os, time, random, argparse, json, tempfile
from typing import Optional, Tuple, List
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

from VisitConfig import VisitConfig  # file must be VisitConfig.py in the same folder

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager

class DeviceProfile:
    def __init__(self, name, user_agent, viewport, timezone, platform):
        self.name, self.user_agent, self.viewport, self.timezone, self.platform = \
            name, user_agent, viewport, timezone, platform

DESKTOP = [
    DeviceProfile("Win Chrome",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        (1280, 720), "America/New_York", "Win32"),
    DeviceProfile("Mac Safari",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_2) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.3 Safari/605.1.15",
        (1440, 900), "Europe/London", "MacIntel"),
]
MOBILE = [
    DeviceProfile("Android Chrome",
        "Mozilla/5.0 (Linux; Android 13; Pixel 7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Mobile Safari/537.36",
        (360, 740), "Asia/Kolkata", "Linux armv8l"),
    DeviceProfile("iPhone Safari",
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_2 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.2 Mobile/15E148 Safari/604.1",
        (390, 844), "America/Los_Angeles", "iPhone"),
]

def parse_proxy_line(line: str) -> Optional[dict]:
    line = line.strip()
    if not line or line.startswith("#"): return None
    parts = [p.strip() for p in line.split(":")]
    if len(parts) == 2:  return {"host": parts[0], "port": parts[1], "user": None, "password": None}
    if len(parts) == 4:  return {"host": parts[0], "port": parts[1], "user": parts[2], "password": parts[3]}
    return None

def read_first_proxy(path: Optional[str]) -> Optional[dict]:
    if not path or not os.path.exists(path): return None
    with open(path, "r", encoding="utf-8", errors="ignore") as f:
        for line in f:
            p = parse_proxy_line(line)
            if p: return p
    return None

def create_proxy_auth_extension(h, p, u, pw) -> str:
    manifest = {
        "name": "Proxy Auth", "version": "1.0.0", "manifest_version": 3,
        "permissions": ["proxy","storage","webRequest","webRequestBlocking"],
        "host_permissions": ["<all_urls>"], "background": {"service_worker": "background.js"}
    }
    bg = f"""
chrome.runtime.onInstalled.addListener(()=>{{
  chrome.proxy.settings.set({{value:{{mode:"fixed_servers",rules:{{
    singleProxy:{{scheme:"http",host:"{h}",port:parseInt("{p}")}},
    bypassList:["localhost","127.0.0.1"]}}}},scope:"regular"}});
}});
chrome.webRequest.onAuthRequired.addListener(()=>({{
  authCredentials:{{username:"{u}",password:"{pw}"}}
}}),{{urls:["<all_urls>"]}},["blocking"]);
"""
    tmp = tempfile.mkdtemp(prefix="proxy_ext_")
    with open(os.path.join(tmp,"manifest.json"),"w",encoding="utf-8") as f: json.dump(manifest,f)
    with open(os.path.join(tmp,"background.js"),"w",encoding="utf-8") as f: f.write(bg)
    return tmp

def add_qa_tags(url: str, enabled: bool, qa_value: str, utm_source: str, utm_medium: str, utm_campaign: str) -> str:
    if not enabled: return url
    u = urlparse(url); q = dict(parse_qsl(u.query, keep_blank_values=True))
    q["qa_runner"] = qa_value or "1"
    q.setdefault("utm_source", utm_source or "qa-runner")
    q.setdefault("utm_medium", utm_medium or "test")
    q.setdefault("utm_campaign", utm_campaign or "qa")
    return urlunparse((u.scheme,u.netloc,u.path,u.params,urlencode(q,doseq=True),u.fragment))

def wait_for_full_load(drv, timeout=60):
    WebDriverWait(drv, timeout).until(lambda d: d.execute_script("return document.readyState")=="complete")

def do_random_scrolls(drv, m=1, M=3):
    for _ in range(random.randint(m,M)):
        drv.execute_script(f"window.scrollBy(0,{random.randint(200,1200)});")
        time.sleep(random.uniform(0.6,1.6))

def pick_clickable(drv):
    els = drv.find_elements("xpath", "//a[@href] | //button | //*[@role='button']")
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

def build_driver(profile, proxy, small=(400,300), minimize=True):
    from selenium.webdriver.common.desired_capabilities import DesiredCapabilities
    opts = Options()
    opts.add_argument(f"--window-size={small[0]},{small[1]}")
    opts.add_argument("--start-minimized")
    opts.add_argument("--disable-gpu"); opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-notifications"); opts.add_argument("--disable-infobars")
    opts.add_argument("--no-default-browser-check"); opts.add_argument("--no-first-run")
    opts.add_argument(f"--user-agent={profile.user_agent}")
    ext_dir=None
    if proxy:
        if proxy.get("user") and proxy.get("password"):
            ext_dir=create_proxy_auth_extension(proxy["host"],proxy["port"],proxy["user"],proxy["password"])
            opts.add_argument(f"--load-extension={ext_dir}")
        else:
            opts.add_argument(f"--proxy-server=http://{proxy['host']}:{proxy['port']}")
    drv = webdriver.Chrome(service=Service(ChromeDriverManager().install()), options=opts)
    try:
        if minimize: drv.minimize_window()
    except Exception: pass
    try: drv.execute_cdp_cmd("Emulation.setUserAgentOverride", {"userAgent": profile.user_agent, "platform": profile.platform})
    except Exception: pass
    try: drv.execute_cdp_cmd("Emulation.setTimezoneOverride", {"timezoneId": profile.timezone})
    except Exception: pass
    if ext_dir:
        import shutil
        orig_quit=drv.quit
        def wrapped_quit():
            try: orig_quit()
            finally: shutil.rmtree(ext_dir, ignore_errors=True)
        drv.quit=wrapped_quit
    return drv

def main():
    ap = argparse.ArgumentParser(description="Authorized QA single-visit smoke test")
    ap.add_argument("--url", required=True)
    ap.add_argument("--referrer", default="https://www.google.com/")
    ap.add_argument("--prewait", nargs=2, type=float, default=[3.0,5.0])
    ap.add_argument("--stay",    nargs=2, type=int,   default=[60,180])
    ap.add_argument("--proxies", default=None)
    ap.add_argument("--profile", choices=["desktop","mobile","random"], default="random")
    ap.add_argument("--qa", action="store_true", default=True)
    ap.add_argument("--qa-value", default="1")
    ap.add_argument("--utm-source", default="qa-runner")
    ap.add_argument("--utm-medium", default="test")
    ap.add_argument("--utm-campaign", default="qa")
    args = ap.parse_args()

    cfg = VisitConfig(args.url, args.prewait[0], args.prewait[1], args.stay[0], args.stay[1], args.referrer).clamp()
    effective_url = add_qa_tags(cfg.target_url, args.qa, args.qa_value, args.utm_source, args.utm_medium, args.utm_campaign)
    profile = random.choice(DESKTOP+MOBILE) if args.profile=="random" else \
              random.choice(DESKTOP) if args.profile=="desktop" else random.choice(MOBILE)
    proxy = read_first_proxy(args.proxies)

    print(f"[info] profile={profile.name} | proxy={'NONE' if not proxy else proxy}")
    print(f"[info] referrer={cfg.referrer}")
    print(f"[info] target={effective_url}")
    print(f"[info] prewait={cfg.min_pre_wait}-{cfg.max_pre_wait}s, stay={cfg.min_stay}-{cfg.max_stay}s")

    drv=None; t_start=time.time()
    try:
        drv = build_driver(profile, proxy, (400,300), True)
        drv.get(cfg.referrer); print("[step] referrer opened")
        time.sleep(random.uniform(cfg.min_pre_wait, cfg.max_pre_wait))
        drv.get(effective_url); print(f"[step] navigating to target…")
        WebDriverWait(drv, 60).until(lambda d: d.execute_script("return document.readyState")=="complete")
        print(f"[step] target loaded: {drv.current_url}")

        do_random_scrolls(drv,1,3)
        el = pick_clickable(drv)
        if el:
            try: el.click(); print("[step] one-click performed"); time.sleep(random.uniform(0.5,1.5))
            except Exception: print("[warn] click failed (ignored)")
        dwell = random.randint(cfg.min_stay, cfg.max_stay)
        print(f"[step] dwell for {dwell}s …")
        t0=time.time()
        while time.time()-t0<dwell:
            if random.random()<0.25: do_random_scrolls(drv,1,1)
            time.sleep(1.0)
        print("[ok] VISIT COMPLETE")
    except Exception as e:
        print(f"[error] {e}")
    finally:
        try:
            if drv: drv.quit()
        except Exception: pass
        print(f"[done] total elapsed: {int(time.time()-t_start)}s")

if __name__ == "__main__":
    main()
