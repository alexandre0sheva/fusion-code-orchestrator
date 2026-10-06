`normalize(path)` in `pathutil.py` is meant to clean up POSIX-style paths, but every result starts with `/`, `..` can crash it, and an empty path becomes `/`.

Fix it so that:

- Repeated slashes collapse to one, `.` segments are dropped, and `name/..` pairs cancel.
- An absolute path (one starting with `/`) stays absolute; a `..` that would go above the root is dropped, so `/../a` is `/a`.
- A relative path stays relative; `..` segments that cannot cancel are kept at the front: `../../a` stays `../../a`, and `a/../../b` is `../b`.
- No trailing slash, except the root path `/` itself.
- A path that normalises to nothing relative (`""`, `"."`, `"a/.."`) is `"."`.
- `normalize("//a")` is `/a` (two leading slashes are one root).
