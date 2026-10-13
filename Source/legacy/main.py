import os
import shutil
import threading
import time

from selenium.webdriver import ChromeOptions
from bot import Bot, get_task
from fake_useragent import UserAgent

from vars import sourcePath, cwd, log_write

from random import randint, choice

thread_count = 0

print(cwd)

extentionPath = f"{sourcePath}/extensions"
cacheFolder = f"{sourcePath}/cache"
usedIPs = []

with open(f"{sourcePath}/proxy.txt", "r") as f:
    proxy = f.read().split("\n")

with open(f"{sourcePath}/maxCount.txt", "r") as f:
    maxCount = int(f.read())

# Check streets


def createFile(path, text):
    print(path)
    with open(path, "w") as f:
        f.write(text)


user_agent = UserAgent(browsers=["Chrome" , "Firefox", "Safari", "Edge"])
mobile_agent = UserAgent(os=["iOS", "Android"], browsers=["Opera Mobile" , "Firefox Mobile", "Mobile Safari", "Edge Mobile", "Chrome Mobile iOS", "Firefox iOS"])

ios_models = ["iPhone 4", "iPhone 5/SE", "iPhone 6/7/8", "iPhone 6/7/8 Plus", "iPhone X", "iPhone SE", "iPhone XR", "iPhone 12 Pro", "iPhone 14 Pro Max"]
android_models = ["Samsung Galaxy S8+", "Samsung Galaxy S20 Ultra", "Galaxy Z Fold 5", "Pixel 4", "Pixel 3", "Nexus 7", "Nexus 5X", "Nexus 6P", "Pixel 7"]

def changeUserAgent(ep, ip, port, username, password):
    if not os.path.exists(ep):
        os.makedirs(ep)

        createFile(f"{ep}/manifest.json", """{
    "name": "Mega+",
    "description": "Zur Ehre des Herrn",
    "author": "Mr. Beagle",
    "version": "1.0",
    "manifest_version": 3,
    "background": {
        "service_worker": "background.js"
    },
    "permissions": [
        "declarativeNetRequestWithHostAccess",
        "proxy",
        "webRequest",
        "webRequestAuthProvider"
    ],
    "host_permissions": ["<all_urls>"]
}""")

    createFile(f"{ep}/background.js", """var config = {
  mode: "pac_script",
  pacScript: {
    data: "function FindProxyForURL(url, host) {return 'PROXY %s:%s';}"
  }
};
chrome.proxy.settings.set(
  {value: config, scope: 'regular'},
  function() {}
);

chrome.webRequest.onAuthRequired.addListener((_, callbackFn)=>{
    callbackFn({
        authCredentials: {
            username: "%s",
            password: "%s"
        }
    })
}, {urls: ["<all_urls>"]}, ["asyncBlocking"])

chrome.declarativeNetRequest.updateDynamicRules(
    {
        "addRules": [{
            "id": 1,
            "action": { "type": "block" },
            "condition": {"urlFilter": "https://mc.yandex.ru/metrika/tag.js"}
        }],
        "removeRuleIds": [1]
    }
)

chrome.declarativeNetRequest.getDynamicRules(
    e => console.log(e)
);""" % (ip, port, username, password))

def generate_cmd_arg(isMobile, options):
    cmd_arg = {
        "userAgent": "",
        "userAgentMetadata": {
            "brands": [],
            "fullVersionList": [],
            "fullVersion": "",
            "platform": "",
            "platformVersion": "",
            "architecture": "x86",
            "model": "",
            "mobile": False
        }
    }
    userAgentMetadata = cmd_arg["userAgentMetadata"]


    if isMobile:
        uar = mobile_agent.getRandom
        cmd_arg["userAgent"] = uar["useragent"]

        if uar["os"] == "iOS":
            model = choice(ios_models)
            options.add_experimental_option("mobileEmulation", {"deviceName": choice(ios_models)})
            # return cmd_arg, uar["os"]
        else:
            model = choice(android_models)

        options.add_experimental_option("mobileEmulation", {"deviceName": model})
        userAgentMetadata["model"] = model
    else:
        uar = user_agent.getRandom
        cmd_arg["userAgent"] = uar["useragent"]

    if uar["browser"] == "Chrome":
        full_version = uar["browser_version"]
        version = full_version.split(".")[0]

        userAgentMetadata["brands"] = [
            {"brand": "Not A;Brand", "version": "24"},
            {"brand": "Chromium", "version": version},
            {"brand": "Google Chrome", "version": version}
        ]
        userAgentMetadata["fullVersionList"] = [
            {"brand": "Chromium", "version": full_version},
            {"brand": "Not A;Brand", "version": "24.0.0.0"},
            {"brand": "Google Chrome", "version": full_version}
        ]
        userAgentMetadata["fullVersion"] = full_version

    userAgentMetadata["platform"] = uar["os"]
    userAgentMetadata["platformVersion"] = uar["os_version"]
    userAgentMetadata["mobile"] = isMobile

    return cmd_arg, uar["os"]

def createBot(ip):
    global thread_count

    if ip in usedIPs:
        return

    thread_count += 1
    streets = get_task()

    if not streets:
        thread_count -= 1
        return

    usedIPs.append(ip)

    p = ip.split(":")

    options = ChromeOptions()

    cachePath = f"{cacheFolder}/{p[0]}.{p[1]}"

    ep = f"{extentionPath}/{p[0]}"

    isMobile = randint(0, 1) == 1

    cmd_arg, platform = generate_cmd_arg(isMobile, options)
    print(cmd_arg)

    options.add_argument("--disable-gpu")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-features=DisableLoadExtensionCommandLineSwitch")
    options.add_argument(f"--user-data-dir={cachePath}")
    options.add_argument("--profile-directory=Profile")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-browser-side-navigation")
    options.add_argument("--disable-gpu")
    options.page_load_strategy = 'eager'

    changeUserAgent(ep, *p)
    options.add_argument(f"--load-extension={ep}")
    # options.add_extension(ep)

    print(streets)

    bot = None

    try:
        bot = Bot(options, p[0], isMobile)
        bot.driver.execute_cdp_cmd("Network.setUserAgentOverride", cmd_arg)
        bot.driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": f"Object.defineProperty(navigator, \"platform\", {{ value: \"{platform}\" }});"
        })
    except Exception as e:
        print(f"Specific ERROR: {e}")

    if bot is not None:
        for keyword in streets:
            try:
                time.sleep(5)

                print("Run!")
                log_write(f"[LOG: {ip}] : Run!\n")

                if bot.search(*keyword.split(" | ")):
                    time.sleep(100)
                    bot.driver.current_window()
            except Exception as e:
                print("FATAL ERROR!")
                print(e)

        print(bot.stats)
        bot.res_task(ip=ip)

        bot.driver.quit()

    print("Программа отработала.")
    log_write(f"[LOG: {ip}] : END HARD WORK!\n")

    shutil.rmtree(cachePath)
    thread_count -= 1
    usedIPs.remove(ip)

    raise Exception("KILL BOT")

while True:
    for ip in proxy:
        threading.Thread(target=createBot, args=[ip]).start()

        while thread_count >= maxCount:
            time.sleep(1)

# End poop code :)
