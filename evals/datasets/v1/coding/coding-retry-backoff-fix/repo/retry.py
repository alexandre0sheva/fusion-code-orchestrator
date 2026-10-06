import time


def retry(attempts=3, delay=1.0, backoff=2.0, exceptions=(Exception,), sleep=time.sleep):
    """Retry the decorated function with exponential backoff."""

    def decorator(fn):
        def wrapper(*args, **kwargs):
            wait = delay
            last = None
            for _ in range(attempts):
                try:
                    return fn(*args, **kwargs)
                except exceptions as exc:
                    last = exc
                    sleep(wait)
                    wait *= backoff
            raise last

        return wrapper

    return decorator
