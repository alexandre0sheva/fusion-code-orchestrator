from intersect import intersect_sorted


def setup(size):
    return list(range(0, 2 * size, 2)), list(range(0, 3 * size, 3))


def run(state):
    intersect_sorted(*state)
