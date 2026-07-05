import sys
from os.path import dirname

sourcePath = sys.argv[1]
cwd = str(dirname(__file__))
with open(f"{sourcePath}/key.txt", "r") as f:
    key = f.read()

def log_write(msg):
    print(msg)
    with open(f"{sourcePath}/logs.txt", "a") as f:
        f.write(msg)