"""Owner-authorized persistent service profiles, separate from personal browsers."""
import re
from pathlib import Path
from urllib.parse import urlsplit
import conversation as c

SERVICES={'google_flow':{'url':'https://flow.google.com/','hosts':{'flow.google.com','labs.google','accounts.google.com'},
    'generation_hosts':{'flow.google.com','labs.google'},
    'frame_hosts':{'www.google.com','www.recaptcha.net','www.gstatic.com','accounts.google.com'},'title':'Google Flow'}}

def service(profile):
    if profile not in SERVICES:raise ValueError('Unknown browser service profile')
    policy=c.read_json(c.HOME/'browser-services.json',{}).get(profile,{})
    if policy.get('enabled') is not True:raise PermissionError('服务通道尚未由主人启用')
    return SERVICES[profile],policy


def authorizations():
    configured=c.read_json(c.HOME/'browser-services.json',{})
    return {name:{'included_credit_consumption_authorized':True,'currency_spend_allowed':False,
                  'purchases_allowed':False,'api_billing_allowed':False,
                  'per_generation_reconfirmation_required':False,
                  'meaning':'可消耗已包含的订阅点数；0元现金支出不等于0点数消耗。仍须核验实际余额、费用与数量。'}
            for name in SERVICES if configured.get(name,{}).get('enabled') is True}

def profile_path(profile):
    service(profile)
    path=c.HOME/'browser-service-profiles'/profile
    path.mkdir(parents=True,exist_ok=True)
    return path

def initialized(profile):
    return (profile_path(profile)/'Local State').is_file()

def allowed(profile,url):
    config,_=service(profile);parsed=urlsplit(url)
    return parsed.scheme=='https' and parsed.hostname in config['hosts'] and not parsed.username and not parsed.password and parsed.port in (None,443)

async def launch(pw,profile,interactive=False):
    config,_=service(profile)
    candidates=[Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')]
    executable=next((str(p) for p in candidates if p.is_file()),None)
    if not executable:raise FileNotFoundError('Chrome/Edge 未安装')
    # This profile is created for the bot. Never copy/decrypt the owner's browser cookies.
    return await pw.chromium.launch_persistent_context(str(profile_path(profile)),executable_path=executable,
        headless=not interactive,viewport={'width':1440,'height':1000},locale='zh-CN',accept_downloads=True,service_workers='block')

def credit_number(text,value):
    if not re.search(r'credits?|积分|点数|额度',text,re.I):return False
    numbers={float(v.replace(',','')) for v in re.findall(r'(?<!\d)\d[\d,]*(?:\.\d+)?(?!\d)',text)}
    return float(value) in numbers

async def login(profile):
    """Interactive CLI entry point; the owner enters credentials directly into Google."""
    import asyncio
    config,_=service(profile)
    candidates=[Path('C:/Program Files/Google/Chrome/Application/chrome.exe'),Path('C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe')]
    executable=next((str(p) for p in candidates if p.is_file()),None)
    if not executable:raise FileNotFoundError('Chrome/Edge 未安装')
    # Human sign-in uses an ordinary browser, no automation flags or debug port.
    process=await asyncio.create_subprocess_exec(executable,'--user-data-dir='+str(profile_path(profile)),
        '--no-first-run','--no-default-browser-check',config['url'],
        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.DEVNULL)
    await asyncio.sleep(1)
    if process.returncode is not None and not initialized(profile):
        raise RuntimeError('独立浏览器未成功启动，请手动使用桌面“大肥鱼 Flow 登录”入口，不能把进程已创建说成窗口已打开')
    print('登录入口已启动；请确认可见窗口，在其中登录Google AI Pro，完成后关闭独立窗口。',flush=True)
    await process.wait()

if __name__=='__main__':
    import asyncio,sys
    from network_runtime import configure
    configure(c.HOME)
    asyncio.run(login(sys.argv[1] if len(sys.argv)>1 else 'google_flow'))
