from runs import longest_unique_run


def setup(size):
    return "".join(chr(0x4E00 + i) for i in range(size))


def run(state):
    longest_unique_run(state)
