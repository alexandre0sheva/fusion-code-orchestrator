from pairs import count_pairs


def setup(size):
    return list(range(size))


def run(state):
    count_pairs(state, 1)
