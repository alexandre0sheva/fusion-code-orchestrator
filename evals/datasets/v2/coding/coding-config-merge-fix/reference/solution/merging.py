import copy


def merge(base, override):
    """Merge ``override`` into ``base``."""
    result = {}
    for key, value in base.items():
        if key in override and override[key] is None:
            continue
        if key in override and isinstance(value, dict) and isinstance(override[key], dict):
            result[key] = merge(value, override[key])
        elif key in override:
            result[key] = copy.deepcopy(override[key])
        else:
            result[key] = copy.deepcopy(value)
    for key, value in override.items():
        if key not in base and value is not None:
            result[key] = merge({}, value) if isinstance(value, dict) else copy.deepcopy(value)
    return result
