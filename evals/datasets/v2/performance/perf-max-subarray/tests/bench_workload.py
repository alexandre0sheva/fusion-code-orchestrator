from maxsub import max_subarray_sum


def setup(size):
    return [((i * 7919) % 201) - 100 for i in range(size)]


def run(state):
    max_subarray_sum(state)
