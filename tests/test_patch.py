"""Patches: finding the one in an answer, and applying it tolerantly but safely."""

from __future__ import annotations

import json

import pytest

from fusion.bench.patch import (
    MAX_PATCH_BYTES,
    PatchError,
    apply_patch,
    diff_files,
    extract_patch,
    patch_as_blobs,
)
from fusion.orchestration.claims import patch_text_key

FILES = {
    "a.py": "def f(x):\n    return x+1\n\n\ndef g():\n    return 2\n",
    "pkg/c.py": "x = 1\n",
}
AFTER = {
    **FILES,
    "a.py": "def f(x):\n    return x + 1\n\n\ndef g():\n    return 3\n",
    "n.py": "new = 1\n",
}
DIFF = diff_files(FILES, AFTER)


# -- finding the patch in an answer --------------------------------------------------------------


def test_the_patch_field_of_a_json_answer_is_the_patch() -> None:
    found = extract_patch(json.dumps({"summary": "s", "claims": [], "patch": DIFF}))
    assert found.text == DIFF and found.problem == ""


def test_the_structured_field_wins_over_the_text() -> None:
    found = extract_patch("```diff\n--- a/z\n+++ b/z\n@@ -1 +1 @@\n-a\n+b\n```", {"patch": DIFF})
    assert found.text == DIFF


def test_a_fenced_block_is_found_in_prose_whatever_its_language_tag() -> None:
    for tag in ("diff", "patch", "", "text"):
        found = extract_patch(f"Here is the fix:\n```{tag}\n{DIFF}```\nDone.")
        assert found.text is not None and patch_text_key(found.text) == patch_text_key(DIFF)


def test_a_longer_fence_can_hold_backticks_and_an_unclosed_fence_still_counts() -> None:
    inner = "=== notes.md ===\nuse ``` for code\n"
    assert extract_patch(f"````\n{inner}````").text == inner.rstrip("\n")
    assert extract_patch(f"cut off:\n```diff\n{DIFF}").text is not None


def test_a_bare_patch_is_accepted() -> None:
    assert extract_patch(DIFF).text == DIFF
    assert extract_patch("=== a.py ===\nx = 1\n").text is not None


def test_no_patch_and_several_different_patches_are_reported_not_guessed() -> None:
    assert (
        extract_patch("I fixed it by changing the return value.").problem
        == "the answer contains no patch"
    )
    other = DIFF.replace("return 3", "return 4")
    two = extract_patch(f"```diff\n{DIFF}```\nor\n```diff\n{other}```")
    assert two.text is None and "2 different patches" in two.problem
    same = extract_patch(f"```diff\n{DIFF}```\nagain\n```diff\n{DIFF}\n```")
    assert same.text is not None  # the same patch twice is one patch


def test_a_json_answer_without_a_patch_has_none_even_if_prose_mentions_one() -> None:
    answer = json.dumps({"summary": "no change needed", "patch": None})
    assert extract_patch(answer).text is None


def test_a_patch_field_that_is_not_a_patch_is_refused() -> None:
    found = extract_patch(json.dumps({"patch": "just change line 3"}))
    assert found.text is None and "not a diff" in found.problem


def test_an_enormous_patch_is_refused() -> None:
    big = "=== a.py ===\n" + "x" * (MAX_PATCH_BYTES + 10)
    assert extract_patch(big).text is None
    with pytest.raises(PatchError, match="larger"):
        apply_patch({}, big)


# -- applying ------------------------------------------------------------------------------------


def test_a_diff_applies_and_creates_files() -> None:
    changed = apply_patch(FILES, DIFF)
    assert changed == {"a.py": AFTER["a.py"], "n.py": "new = 1\n"}


def test_hunk_numbers_and_counts_are_not_trusted() -> None:
    shifted = "--- a/a.py\n+++ b/a.py\n@@ -90,3 +90,3 @@\n def g():\n-    return 2\n+    return 9\n"
    assert apply_patch(FILES, shifted)["a.py"].endswith("def g():\n    return 9\n")
    wrong_counts = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,99 +1,99 @@\n def g():\n-    return 2\n+    return 9\n"
    )
    assert "return 9" in apply_patch(FILES, wrong_counts)["a.py"]


def test_trailing_whitespace_in_context_and_blank_context_lines_are_forgiven() -> None:
    sloppy = (
        "--- a/a.py\n+++ b/a.py\n@@ -1,3 +1,3 @@\n"
        " def f(x):   \n-    return x+1\n+    return x + 2\n\n"
    )
    assert "return x + 2" in apply_patch(FILES, sloppy)["a.py"]


def test_the_nearest_match_is_used_when_the_context_repeats() -> None:
    files = {"r.py": "a\nx\nb\nx\nc\nx\n"}
    patch = "--- a/r.py\n+++ b/r.py\n@@ -4,1 +4,1 @@\n-x\n+y\n"
    assert apply_patch(files, patch)["r.py"] == "a\nx\nb\ny\nc\nx\n"


def test_deleting_and_renaming_files() -> None:
    delete = "--- a/pkg/c.py\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n"
    assert apply_patch(FILES, delete) == {"pkg/c.py": None}
    rename = "--- a/pkg/c.py\n+++ b/pkg/d.py\n@@ -1 +1 @@\n-x = 1\n+x = 2\n"
    assert apply_patch(FILES, rename) == {"pkg/d.py": "x = 2\n", "pkg/c.py": None}


def test_git_style_headers_and_plain_paths() -> None:
    git = (
        "diff --git a/pkg/c.py b/pkg/c.py\nindex 1..2 100644\n"
        "--- a/pkg/c.py\n+++ b/pkg/c.py\n@@ -1 +1 @@\n-x = 1\n+x = 5\n"
    )
    assert apply_patch(FILES, git) == {"pkg/c.py": "x = 5\n"}
    plain = "--- pkg/c.py\n+++ pkg/c.py\n@@ -1 +1 @@\n-x = 1\n+x = 6\n"
    assert apply_patch(FILES, plain) == {"pkg/c.py": "x = 6\n"}


def test_a_diff_that_does_not_match_the_files_is_an_error_naming_the_hunk() -> None:
    bad = "--- a/a.py\n+++ b/a.py\n@@ -1 +1 @@\n-    return nothing_like_this\n+    return 0\n"
    with pytest.raises(PatchError, match=r"a\.py: hunk 1 does not apply"):
        apply_patch(FILES, bad)
    with pytest.raises(PatchError, match="does not exist"):
        apply_patch(FILES, "--- a/ghost.py\n+++ b/ghost.py\n@@ -1 +1 @@\n-a\n+b\n")
    with pytest.raises(PatchError, match="no file diffs"):
        apply_patch(FILES, "@@ -1 +1 @@\n-a\n+b\n")


def test_file_blobs_replace_whole_files() -> None:
    blobs = "=== a.py ===\nVALUE = 1\n=== pkg/new.py ===\n```python\nz = 2\n```\n"
    assert apply_patch(FILES, blobs) == {"a.py": "VALUE = 1\n", "pkg/new.py": "z = 2\n"}
    with pytest.raises(PatchError, match="must each start"):
        apply_patch(FILES, "stray text\n=== a.py ===\nx\n")


def test_a_diff_and_its_blob_form_agree() -> None:
    assert apply_patch(FILES, patch_as_blobs(FILES, DIFF)) == apply_patch(FILES, DIFF)


@pytest.mark.parametrize(
    "evil",
    [
        "--- a/../x.py\n+++ b/../x.py\n@@ -1 +1 @@\n-a\n+b\n",
        "--- /dev/null\n+++ b//etc/passwd\n@@ -0,0 +1 @@\n+x\n",
        "--- /dev/null\n+++ b/.git/hooks/pre-commit\n@@ -0,0 +1 @@\n+x\n",
        "--- /dev/null\n+++ b/a/../../x\n@@ -0,0 +1 @@\n+x\n",
        "=== ../outside.py ===\nx\n",
        "=== /etc/cron.d/job ===\nx\n",
        "=== ~/.bashrc ===\nx\n",
        "=== a\\..\\b.py ===\nx\n",
    ],
)
def test_paths_that_escape_are_rejected_before_anything_is_written(evil: str) -> None:
    with pytest.raises(PatchError, match="unsafe path"):
        apply_patch(FILES, evil)


def test_too_many_files_are_refused() -> None:
    blobs = "".join(f"=== f{n}.py ===\nx\n" for n in range(101))
    with pytest.raises(PatchError, match="at most"):
        apply_patch({}, blobs)


def test_diff_files_round_trips() -> None:
    changed = apply_patch(FILES, diff_files(FILES, AFTER))
    assert {**FILES, **changed} == AFTER
    assert diff_files(FILES, FILES) == ""
