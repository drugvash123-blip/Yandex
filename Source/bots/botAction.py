import time

from selenium.webdriver import ActionChains

from timeLib import wait


class BotAction(ActionChains):
    def __init__(self, driver, height, duration = 250, devices = None, isMobile = False):
        self.isMobile = isMobile
        self.height = height
        super().__init__(driver, duration, devices)

    def mobile_scroll(self, x, y):
        print("SCROLL")
        self.w3c_actions.pointer_action.move_to_location(
            int(self._driver.execute_script("return window.innerWidth")) / 2,
            int(self._driver.execute_script("return window.innerHeight")) / 2
        ).pointer_down()
        self.w3c_actions.pointer_action.move_by(-x, -y).release()
        self.w3c_actions.perform()


    def scroll(self, x, y):
        self.scroll_by_amount(x, y).perform()

    def safe_click(self, element):
        try:
            if self.isMobile:
                self.w3c_actions.pointer_action.move_to(element).pointer_down().pointer_up()
                self.perform()
            else:
                self.move_to_element(element).click().perform()
                print("Perform")
        except Exception as _:
            print(_)

    def typing(self, text):
        for c in text:
            self.send_keys(c).perform()
            wait(1)
