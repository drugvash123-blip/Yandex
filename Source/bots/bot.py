import base64
import datetime
import http
import json
import random
import time
from socket import send_fds

import requests
from selenium.webdriver.common.by import By
from selenium.webdriver import ChromeService

from botAction import BotAction
from chromeBot import ChromeBot
from randstr import randstr
from timeLib import wait, wait_captcha_response
from vars import key, log_write, cwd


def generate_from_data(args, boundary):
    body = bytes()

    for name, data in args:
        body += bytes(f"--{boundary}\r\n"
                      f"Content-Disposition: form-data; name={name};\r\n"
                      f"Content-Type: text/plain\r\n"
                      f"\r\n"
                      f"{data}\r\n", "utf-8")

    body += bytes(f"--{boundary}--\r\n", "utf-8")

    return body


def analytic_request(path, data):
    boundary = randstr(25)

    conn = http.client.HTTPSConnection("prstar.ru", 443)
    conn.request("POST", path, generate_from_data(data, boundary),
                 {"Content-type": f"multipart/form-data; boundary={boundary}"})

    return conn


botsKey = "651654adrgagdfgadg81513489198161351"

def get_task():
    try:
        print("GET_TASK")
        response = analytic_request("/help/botsSreetsFile", [["botsKey", botsKey], ["engine", "yandex"]]).getresponse()
        return response.getcode() == 200 and response.read().decode("utf-8").replace("\r\n\r\n\r\n",
                                                                                     "\r\n").splitlines() or []
    except Exception as e:
        print(e)
        return []


class Bot:
    stats = []

    def res_task(self, ip):
        try:
            print("RES_TASK")
            print(analytic_request("/help/botsStatsFile", [
                ["botsKey", botsKey],
                ["stats", json.dumps(self.stats)],
                ["fileName", ip],
                ["engine", "yandex"]
            ]).getresponse().read().decode("utf-8"))
            self.stats = []
        except Exception as e:
            print(e)

    def check_captcha(self):
        if self.driver.title == "Вы не робот?":
            checkbox_captcha = self.driver.wait_for_element(timeout=60, by=By.CSS_SELECTOR, selector=".CheckboxCaptcha-Button")
            self.action.safe_click(checkbox_captcha)

            while self.driver.title == "Вы не робот?":
                send_button = self.driver.wait_for_element(by=By.CSS_SELECTOR,
                                                           selector=".CaptchaButton-ProgressWrapper, #captcha-slider",
                                                           timeout=60)
                click_button = self.driver.wait_for_element(by=By.CSS_SELECTOR,
                                                           selector=".CaptchaButton_view_action",
                                                           timeout=60)

                if not send_button:
                    self.driver.get(self.driver.current_url)
                    self.check_captcha()
                    print("driver.get")
                    break
                else:
                    if send_button.get_attribute("id") == "captcha-slider":
                        old = self.driver.execute_script("return window.__SSR_DATA__.imageSrc;")
                        print("Slider Captcha...")
                        id = requests.post("http://api.cap.guru/in.php", json={
                            "key": "7b7a290e4e602b1deeaa924b963c6683",
                            "method": "base64",
                            "click": "oth2",
                            "textinstructions": self.driver.execute_script(
                                "let cT = window.__SSR_DATA__.task;return 'puzzlekal-' + cT.substr(1, cT.length-2);"),
                            "body": base64.b64encode(requests.get(old).content).decode(
                                "ascii"),
                        })

                        print(id.text)

                        id = id.text.split("|")[1]

                        res = wait_captcha_response(key, id)

                        if res != "ERROR_CAPTCHA_UNSOLVABLE":
                            control = self.driver.find_element(By.CLASS_NAME, "ControlButton")

                            self.action.move_to_element(control)

                            print(res)

                            wait(3)

                            for _ in range(0, int(res.split("|")[1])):
                                self.action.click().perform()
                                wait(2)

                            wait(5)

                            print("SEND_BUTTON:")
                            print(send_button)

                            while self.driver.title == "Вы не робот?" and self.driver.execute_script(
                                    "let ssr = window.__SSR_DATA__; if (ssr != null) return window.__SSR_DATA__.imageSrc; else null") == old:
                                self.action.safe_click(click_button)
                                time.sleep(5)

                            continue
                    else:
                        captcha_element = self.driver.safe_find_element(By.CLASS_NAME, "Textinput")

                        if captcha_element:
                            print("Text Captcha...")

                            view = self.driver.wait_for_element(By.CLASS_NAME, "AdvancedCaptcha-View")

                            if view:
                                id = requests.post(url="http://api.cap.guru/in.php", json={
                                    "key": key,
                                    "method": "base64",
                                    "vernet": 18,
                                    "body": view.screenshot_as_base64,
                                }).text.split("|")[1]

                                res = wait_captcha_response(key, id)

                                if res != "ERROR_CAPTCHA_UNSOLVABLE":
                                    text = res.split("t\":\"")[0].split("\"")[0].split("|")[1]
                                    self.action.safe_click(captcha_element)
                                    print(text)
                                    self.action.typing(text)

                                    self.action.safe_click(click_button)

                                    continue
                        else:
                            print("Img Captcha")

                            image = self.driver.wait_for_element(By.CLASS_NAME, "AdvancedCaptcha")

                            if image:
                                time.sleep(10)
                                id = requests.post(url="http://api.cap.guru/in.php", json={
                                    "key": key,
                                    "method": "base64",
                                    "textinstructions": "yandex",
                                    "click": "oth",
                                    "body": image.screenshot_as_base64,
                                }).text.split("|")[1]

                                print(id)

                                res = wait_captcha_response(key, id)
                                print(res)
                                if res != "ERROR_CAPTCHA_UNSOLVABLE":
                                    for i in str(res).split(":")[1].split(";"):
                                        cord = i.split(",")
                                        x = int(cord[0].split("=")[1])
                                        y = int(cord[1].split("=")[1])

                                        self.action.move_to_element_with_offset(image,
                                                                                -image.size.get("width") / 2 + x,
                                                                                -image.size.get(
                                                                                    "height") / 2 + y).click().perform()
                                        wait(5)

                                    self.action.safe_click(click_button)

                                    continue

                self.action.safe_click(
                    self.driver.find_element(By.CLASS_NAME,
                                             "CaptchaButton_view_clear"))
                print("CAPTCHA CLEAR")

    def edit_stats(self, keyword, site, page, position):
        self.stats.append(
            {"domain": site, "page": page, "position": str(position), "date": str(datetime.datetime.now()), "key": keyword,
             "ip": self.ip})

    def check_page(self, site, keyword, page):
        print("2")
        ads = self.driver.find_elements(By.CSS_SELECTOR, ".path.organic__path")

        for i in range(0, len(ads)):
            ad = ads[i]
            print(ad.text)
            if site in ad.text:
                print("Find")

                position = i + 1
                log_write(f"[LOG: {self.ip}] : Click by {site}, his position {position}. Keywords: {keyword}\n")

                self.edit_stats(keyword=keyword, site=site, page=page, position=position)

                self.scrollToElement(ad, .25)
                self.action.safe_click(ad)

                return True
        return False

    def close_alert(self):
        button_close = self.driver.wait_for_element(timeout=3, by=By.CSS_SELECTOR, selector=".Distribution-ButtonClose")
        print(button_close)

        if button_close:
            self.action.safe_click(button_close)
            time.sleep(3)

    def search(self, keyword, site=None):
        self.driver.get("https://ya.ru/")

        while "/search/" not in self.driver.current_url:
            self.close_alert()
            self.check_captcha()
            print("WORK")
            # current_window(self.driver)

            search_input = self.driver.safe_find_element(By.CSS_SELECTOR, ".input__control, .search3__inner, .search-f, .mini-suggest__row")

            if search_input is not None:
                self.action.safe_click(search_input)

                wait(4)
                self.action.typing(keyword + "\n")
                wait(2)

                self.check_captcha()
                break
            else:
                try:
                    print(f"GO BACK, ip: {self.ip}")
                    self.driver.get("https://ya.ru/")
                    time.sleep(2)
                except Exception as e:
                    print(e)

        self.close_alert()

        if site is not None:
            if self.check_page(site, keyword, "1"):
                return True

            for page_count in range(1, 10):
                print("1")
                new_page = self.driver.wait_for_element(By.XPATH, f"//a[text()='{page_count+1}'] | //button/span[text()='Показать ещё']")

                # time.sleep(99999999)

                if new_page is None:
                    print("NO BUTTON")
                    # time.sleep(10000000)
                    break


                print(new_page)
                self.scrollToElement(new_page, .25)
                time.sleep(5)
                self.action.safe_click(new_page)
                time.sleep(5)
                self.check_captcha()

                if self.check_page(site, keyword, str(page_count)):
                    return True

            self.edit_stats(keyword=keyword, site=site, page="Not found", position="Not found")

            return False

        else:
            while True:
                self.check_captcha()
                wait(5)

                all_ads = self.driver.find_elements(By.CLASS_NAME, "organic__url")

                if len(all_ads) == 0 or random.randint(0, 10) == 0:
                    ad = self.driver.wait_for_element(By.CSS_SELECTOR, ".Pager-Item_type_next, .Pager-More")
                else:
                    ad = random.choice(all_ads)

                if ad is not None:
                    self.scrollToElement(ad, .25)
                    wait(5)
                    self.action.safe_click(ad)
                    time.sleep(5)

                    if self.driver.safe_find_element(By.CLASS_NAME, "neterror"):
                        print("Neterror")
                        log_write(f"[LOG: {self.ip}] : Neterror...\n")
                        time.sleep(1)

                        if len(self.driver.window_handles) > 0:
                            self.driver.close()
                        else:
                            self.driver.back()
                    else:
                        self.driver.current_window()
                        return True

    def scrollToElement(self, element, timed):
        count = 50

        steps = int(
            (element.location["y"] - int(
                self.driver.execute_script("return window.scrollY")) - self.jsHalfHeight) / count)

        if steps < 0:
            count = -count

        if self.isMobile:
            for _ in range(0, steps):
                self.action.mobile_scroll(0, count)
                wait(timed)
        else:
            for _ in range(0, steps):
                self.action.scroll(0, count)
                wait(timed)

    def __init__(self, options, ip, isMobile):
        self.ip = ip
        self.width, self.height = random.randint(800, 1000), random.randint(800, 1000)

        self.driver = ChromeBot(service=ChromeService(executable_path=f"{cwd}/drivers/chromedriver"),
                                options=options)
        self.driver.set_window_size(self.width, self.height)
        self.jsHalfHeight = int(self.driver.execute_script("return window.innerHeight;")) / 2
        self.action = BotAction(self.driver, 0, isMobile=isMobile)
        self.isMobile = isMobile
