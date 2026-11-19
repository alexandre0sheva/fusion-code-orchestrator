# The 30-second demo in README.md. Re-record from the repository root (needs vhs, ttyd, ffmpeg):
#   uv sync --all-groups && vhs docs/assets/demo.tape
#
# It runs `fusion review-diff --diff-ref HEAD~1 --mock` in a throwaway git repository. `--mock` is the
# offline provider: it answers with canned text and calls no API, so the findings are NOT a real
# review of this diff. It shows the flow (Fusion reads the git diff itself, asks the panel, returns
# claims with agreement, confidence and cost). With a key, the same command asks real models.
Output docs/assets/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1100
Set Height 680
Set Padding 16
Set Theme "Catppuccin Mocha"
Set TypingSpeed 35ms
Set PlaybackSpeed 1

Hide
Type "export PATH=$PWD/.venv/bin:$PATH FUSION_DATA_DIR=$(mktemp -d) PS1='$ ' && cd $(mktemp -d) && git init -q . && git config user.email d@example.com && git config user.name demo" Enter
Type "printf 'def charge(card, cents):\n    return gateway.send(card, cents)\n' > pay.py && git add . && git commit -qm base" Enter
Type "printf 'def charge(card, cents):\n    log.debug(card)\n    return gateway.send(card, cents * 100)\n' > pay.py && git commit -qam 'log and scale' && clear" Enter
Show

Type "# the host (Claude Code) names the range; Fusion reads the git diff itself"
Enter
Sleep 600ms
Type "git diff HEAD~1 --stat"
Enter
Sleep 1s
Type "fusion review-diff --diff-ref HEAD~1 --mock -q"
Enter
Sleep 4s
Type "# offline demo (--mock): canned findings, no API call. With a key: real models."
Enter
Sleep 4s
