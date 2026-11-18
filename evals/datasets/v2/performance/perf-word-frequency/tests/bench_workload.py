from wordfreq import top_words


def setup(size):
    def word(i):
        letters, i = "", i + 1
        while i:
            i, r = divmod(i - 1, 26)
            letters = chr(97 + r) + letters
        return letters

    vocabulary = max(size // 8, 1)
    return " ".join(word((i * 31) % vocabulary) for i in range(size))


def run(state):
    top_words(state, 10)
