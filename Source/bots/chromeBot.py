from selenium.webdriver.support import expected_conditions
from selenium.webdriver.support.wait import WebDriverWait
from selenium.webdriver import Chrome

class ChromeBot(Chrome):
    def wait_for_element(self, by, selector, timeout=60):
        print(f"WAIT: {selector}")
        try:
            return WebDriverWait(self, timeout).until(
                expected_conditions.presence_of_element_located((by, selector)))
        except:
            return None

    def current_window(self):
        handles = self.window_handles

        for i in range(1, len(handles)):
            self.switch_to.window(handles[i])
            self.close()

        self.switch_to.window(handles[0])

    def safe_find_element(self, by, value):
        print(f"SAFE: {value}")
        safe_elements = self.find_elements(by, value)

        return None if not safe_elements else safe_elements[0]