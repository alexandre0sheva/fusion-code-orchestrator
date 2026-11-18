from topwords import top_k_words


def setup(size):
    return [f"w{(i * 7919) % (size // 2)}" for i in range(size)]


def run(state):
    top_k_words(state, 10)
