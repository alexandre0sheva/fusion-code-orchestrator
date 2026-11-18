def moving_average(values, window):
    """Mean of each full window of ``window`` consecutive values."""
    if window < 1:
        raise ValueError("window must be at least 1")
    return [
        sum(values[start : start + window]) / window
        for start in range(len(values) - window + 1)
    ]
