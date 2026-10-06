The shopping cart in `cart.py` and `pricing.py` computes totals with floats. Finance reports baskets that are a cent off (for example a price of `2.675` is totalled as `2.67`, and `2.665` as `2.66`), and the discount rule is not what the spec says.

Fix the two modules so that:

- Prices are given as `str`, `int` or `Decimal`; a `float` price raises `TypeError` from `Cart.add`. A quantity must be a positive `int`, else `ValueError`.
- `Cart.total()` returns a `Decimal` rounded to two places with **round half up** (`ROUND_HALF_UP`), once, at the very end. An empty cart totals `Decimal("0.00")`.
- `Cart.add_discount(percent)` takes a percentage from 0 to 100 inclusive (`ValueError` otherwise). Discounts are applied in the order they were added, each one to the running amount **without rounding in between**.
- `pricing.line_total`, `pricing.apply_discount` and `pricing.round_money` work on `Decimal` values and keep full precision until `round_money` is called.
