def paginate(items, page, per_page):
    """Return the items on a 1-based page, plus paging metadata."""
    total = len(items)
    pages = total // per_page
    start = (page - 1) * per_page
    end = start + per_page
    return {"items": items[start:end], "page": page, "pages": pages, "total": total}
