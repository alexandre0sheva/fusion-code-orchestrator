Bug report for this repository (the `validators` package): `domain()` accepts an underscore in
the last label.

`validators.domain("foo.ba_r")` returns `True`, with or without `rfc_2782=True`, and so does
`domain("example.co_uk")`. An underscore may appear in a service label such as
`_sip._tcp.example.com` when `rfc_2782=True`, but never in the top-level label. Those values
must be rejected (the function returns a falsy `ValidationError`), while every domain that is
valid today stays valid.

Fix `domain`. The package's existing tests are in `tests/`.
