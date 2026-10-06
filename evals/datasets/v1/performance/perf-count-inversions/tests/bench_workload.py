from inversions import count_inversions


def setup(size):
    return [(i * 7919) % 10007 for i in range(size)]


def run(state):
    count_inversions(state)
