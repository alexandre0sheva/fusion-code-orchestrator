def moving_average(values, window):
    """Mean of each full window of ``window`` consecutive values."""
    if window < 1:
        raise ValueError("window must be at least 1")
    if window > len(values):
        return []
    total = sum(values[:window])
    averages = [total / window]
    for index in range(window, len(values)):
        total += values[index] - values[index - window]
        averages.append(total / window)
    return averages
