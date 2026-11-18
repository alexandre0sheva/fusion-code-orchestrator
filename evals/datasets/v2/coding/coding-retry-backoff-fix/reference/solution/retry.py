import functools
import time


def retry(attempts=3, delay=1.0, backoff=2.0, exceptions=(Exception,), sleep=time.sleep):
    """Retry the decorated function with exponential backoff."""
    if attempts < 1:
        raise ValueError("attempts must be at least 1")

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            wait = delay
            for attempt in range(1, attempts + 1):
                try:
                    return fn(*args, **kwargs)
                except exceptions:
                    if attempt == attempts:
                        raise
                    sleep(wait)
                    wait *= backoff

        return wrapper

    return decorator
