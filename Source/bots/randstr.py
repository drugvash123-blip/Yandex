from random import randint

rng = [[0x41, 0x5a], [0x61, 0x7a], [0x30, 0x39]]

def randstr(size):
    ret = ""

    for _ in range(0, size):
        ret += chr(randint(*rng[randint(0, 2)]))

    return ret