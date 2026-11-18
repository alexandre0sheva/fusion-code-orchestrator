import time


def retry(attempts=3, delay=1.0, backoff=2.0, exceptions=(Exception,), sleep=time.sleep):
    """Retry the decorated function with exponential backoff."""

    def decorator(fn):
        def wrapper(*args, **kwargs):
            for attempt in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions:
                    if attempt == attempts - 1:
                        raise
                    sleep(delay)

        return wrapper

    return decorator
