from anagrams import group_anagrams


def setup(size):
    import random

    letters = "abcdefghijklmnopqrstuvwxyz"
    words = []
    for i in range(size):
        base = random.Random(i // 2).sample(letters, 5)
        random.Random(i).shuffle(base)
        words.append("".join(base))
    return words


def run(state):
    group_anagrams(state)
