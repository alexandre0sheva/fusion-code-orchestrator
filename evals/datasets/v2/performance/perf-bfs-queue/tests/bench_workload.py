from bfs import bfs_order


def setup(size):
    return {0: list(range(1, size + 1))}


def run(state):
    bfs_order(state, 0)
