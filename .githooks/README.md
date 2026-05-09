# .githooks/

Repo-bundled git hooks. Wired in by setting `core.hooksPath = .githooks` —
see [CONTRIBUTING.md](../CONTRIBUTING.md#setup) for the one-time setup
command.

## Layout

| File                          | Purpose                                                              |
| ----------------------------- | -------------------------------------------------------------------- |
| `pre-commit`                  | Dispatcher — runs the PII scanner then the secrets scanner           |
| `pre-commit-pii`              | Bash wrapper that invokes the engine with PII rules                  |
| `pre-commit-secrets`          | Bash wrapper that invokes the engine with secret rules               |
| `_scan_engine.py`             | Shared Python engine — walks staged file content, applies regex rules|
| `pii-rules-generic.txt`       | Public, generic PII patterns (committed)                             |
| `secret-rules.txt`            | Gitleaks-seeded credential patterns (committed)                      |
| `pii-rules.example`           | Template for `.pii-rules.local` (gitignored personal patterns)       |

## Behavior

- The scanner walks **file contents** (not diff lines), so a previously
  committed leak that's been moved around will still re-fire on edit.
- Files >10 MB are skipped.
- Binary files (NUL byte in first 8 KB) are skipped.
- Unknown extensions are skipped — extension allow-list is in
  `_scan_engine.py` (`SCANNED_EXTENSIONS`).
- A hit prints the matched file, the rule file + line number, and a
  truncated preview of the match.
- A hit **blocks the commit** with a non-zero exit code.

## Bypass

`git commit --no-verify` skips both hooks. This is the documented
escape hatch for false positives. **Use sparingly.** A legitimate
bypass is rare; a leak is permanent.

## Adding personal patterns

`.pii-rules.local` is gitignored and marked `binary` in `.gitattributes`
so a slip-and-`git add` doesn't expose its contents through diffs. Copy
`pii-rules.example` to `.pii-rules.local` and add patterns specific to
your client/family/codename namespace.

## Maintenance

Patterns drift. The generic ruleset gets pruned and extended as new
credential formats appear (AWS quarterly format changes, GH PAT format
churn, etc.). When in doubt, follow gitleaks upstream and consider the
overlap.
