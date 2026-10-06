def paginate(items, page, per_page):
    """Return the items on a 1-based page, plus paging metadata."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page must be at least 1")
    total = len(items)
    pages = -(-total // per_page)
    start = (page - 1) * per_page
    end = start + per_page
    return {"items": list(items[start:end]), "page": page, "pages": pages, "total": total}
