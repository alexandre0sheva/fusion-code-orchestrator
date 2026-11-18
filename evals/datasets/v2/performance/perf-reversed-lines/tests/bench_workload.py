from loglines import render_newest_first


def setup(size):
    return [f"event number {i} happened" for i in range(size)]


def run(state):
    render_newest_first(state)
