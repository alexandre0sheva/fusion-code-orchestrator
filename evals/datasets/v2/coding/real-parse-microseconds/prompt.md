Bug report for this repository (the `parse` module): timestamps lose microsecond precision.

`parse.parse("{:ti}", "2023-10-14T15:09:08.501902Z")[0].microsecond` is 501901, not 501902: the
fractional seconds are converted through a float and truncated. Fractions with fewer than six
digits must be padded (".1" is 100000 microseconds) and fractions with more than six digits
must be truncated to six (".1234567" is 123456).

Fix the conversion of fractional seconds in timestamps. The existing tests are in `tests/`.
