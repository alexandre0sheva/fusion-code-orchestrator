from rangesums import range_sums


def setup(size):
    values = [(i * 37) % 101 for i in range(size)]
    queries = [((i * 13) % size, min(size, (i * 13) % size + size // 2)) for i in range(size)]
    return values, queries


def run(state):
    range_sums(*state)
