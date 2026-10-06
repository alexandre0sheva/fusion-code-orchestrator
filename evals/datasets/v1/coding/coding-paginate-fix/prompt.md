`paginate(items, page, per_page)` in `pagination.py` is supposed to return one page of a list, with pages numbered from 1. Users report that page 1 shows the second page of results, and that a list of 11 items at 5 per page claims to have 2 pages.

Fix it so that it behaves as documented:

- It returns a dict with `"items"` (the slice for that page), `"page"`, `"pages"` (the number of pages: the total divided by `per_page`, rounded up; `0` for no items) and `"total"` (the number of items).
- Asking for a page past the last one is not an error: `"items"` is `[]`.
- `page` below 1 or `per_page` below 1 raises `ValueError`.
- The input list is not modified and `"items"` is a new list.
