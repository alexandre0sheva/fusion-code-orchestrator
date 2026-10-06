from movavg import moving_average


def setup(size):
    return [(i * 17) % 1000 for i in range(size)], size // 4


def run(state):
    moving_average(*state)
