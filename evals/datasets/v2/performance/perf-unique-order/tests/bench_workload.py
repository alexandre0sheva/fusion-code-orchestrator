from unique import unique


def setup(size):
    return [(i * 7) % (size // 2) for i in range(size)]


def run(state):
    unique(state)
