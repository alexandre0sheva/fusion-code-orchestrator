Bug report for this repository (the `semver` package): `Version.next_version("prerelease",
token)` mishandles tokens that contain dots or regex characters and accepts invalid tokens.

Examples of what goes wrong:

- `Version.parse("1.2.3-alpha.beta.1+build.7").next_version("prerelease", "alpha.beta")` should
  continue the token family and give "1.2.3-alpha.beta.2", but restarts it.
- `Version.parse("1.2.3-alphaXbeta9").next_version("prerelease", "alpha.beta")` treats the token
  as a regular expression ("." matches "X") and gives "1.2.3-alpha.beta10"; it should start the
  family at "1.2.3-alpha.beta.1".
- Tokens that are not valid prerelease identifiers, such as "[", "rc.", "rc+build",
  "rc..next", "rc.01", "01" or "ré", are accepted (or crash with a regex error) instead of
  raising `ValueError`.

Old-style prereleases must keep working ("1.2.3-rc3" with token "rc" gives "1.2.3-rc4"; "rc.3"
gives "rc.4"), an empty or `None` token keeps its current meaning, and the token is ignored when
bumping major, minor or patch. The package's existing tests are in `tests/`.
