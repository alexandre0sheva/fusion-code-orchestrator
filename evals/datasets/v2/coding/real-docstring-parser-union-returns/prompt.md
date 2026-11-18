Bug report for this repository (the `docstring_parser` package): a Google-style `Returns:`
section whose type is a PEP 604 union is not parsed.

For this docstring

```
Returns:
    int | str: multi-type return
```

`parse(...).returns.type_name` should be "int | str" and the description "multi-type return";
instead the whole line ends up in the description and the type is lost. Unions of more members
("int | str | bool") must work too, and the existing forms (`int: ...`, `List[int]: ...`, an
untyped description) must keep parsing as they do now.

Fix the Google parser. The package's existing tests are in `docstring_parser/tests/` (a copy of
the Google-style tests is in `tests/`).
