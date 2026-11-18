Bug report for this repository (the `toml` package): a naive datetime with microseconds does
not survive a round trip.

`toml.loads(toml.dumps({"x": datetime.datetime.now()}))` raises an error (or does not return the
same value) whenever the datetime has a fractional second and no timezone, which is what
`datetime.now()` returns. Loading a local date-time such as `x = 1979-05-27T00:32:00.999999`
should give a naive `datetime` equal to the one dumped; datetimes with an offset must keep
loading as they do now.

Fix how the decoder reads the fractional seconds and offset of a datetime. The package's
existing tests are in `tests/`.
