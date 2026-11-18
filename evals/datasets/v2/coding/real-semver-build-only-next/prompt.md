Bug report for this repository (the `semver` package): `next_version()` does not bump a version
that has build metadata but no prerelease.

`Version.parse("1.2.3+build.5").next_version("patch")` returns "1.2.3" (the build is stripped
and nothing is bumped) instead of "1.2.4"; likewise "1.0.0+build.5" with "major" should give
"2.0.0" and "1.2.0+build.5" with "minor" should give "1.3.0". Build metadata does not take part
in version precedence, so it must not trigger the finalization that a prerelease does:
"1.2.3-rc.1+build.5" with "patch" must still give "1.2.3".

Fix `next_version`. The package's existing tests are in `tests/`.
