import random
import requests
import time

def wait_captcha_response(key, id):
    response = "CAPTCHA_NOT_READY"

    while response == "CAPTCHA_NOT_READY":
        time.sleep(3)
        response = requests.get(
            f"http://api.cap.guru/res.php?key={key}&action=get&id={id}").text

    return response

def wait(minTime=1, maxTime=1):
    time.sleep(minTime + (maxTime - minTime) * random.random())