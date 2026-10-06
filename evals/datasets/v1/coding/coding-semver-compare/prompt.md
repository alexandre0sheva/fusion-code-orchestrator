Implement `compare_versions(a, b)` in `semver.py`. It returns `-1`, `0` or `1` as `a` is lower than, equal to, or higher than `b`, following Semantic Versioning 2.0.0 precedence.

- A version is `MAJOR.MINOR.PATCH`, optionally followed by `-PRERELEASE` and/or `+BUILD`. The three numbers are non-negative integers without leading zeros (`0` is fine, `01` is not).
- Compare MAJOR, then MINOR, then PATCH numerically.
- A version with a prerelease is lower than the same version without one.
- Two prereleases are compared by their dot-separated identifiers from the left: identifiers made only of digits compare as integers; any others compare as ASCII strings; a numeric identifier is lower than a non-numeric one; when all shared identifiers are equal, the one with fewer identifiers is lower. So `1.0.0-alpha < 1.0.0-alpha.1 < 1.0.0-alpha.beta < 1.0.0-beta < 1.0.0-beta.2 < 1.0.0-beta.11 < 1.0.0-rc.1 < 1.0.0`.
- Build metadata (after `+`) is ignored: `1.0.0+a` equals `1.0.0+b`.
- Raise `ValueError` for a malformed version: too few or too many numeric parts (`1.2`, `1.2.3.4`), leading zeros in a number (`01.2.3`), non-numeric parts (`1.2.x`), an empty prerelease (`1.0.0-`), or an empty identifier in it (`1.0.0-a..b`).
