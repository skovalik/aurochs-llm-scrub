---
description: Scan a file for LLM writing patterns (sycophantic openers, hollow conclusions, em-dash overuse, etc.)
argument-hint: [file-path]
---

User invoked `/scrub` with: $ARGUMENTS

If $ARGUMENTS is empty, ask the user which file to scan.

Run via Bash:

```bash
llm-scrub scan $ARGUMENTS
```

Show the user the findings table with severity (P0/P1/P2), rule_id, line, snippet. If findings exist, ask if they want to apply fixes (`llm-scrub scrub --in-place $ARGUMENTS` — backs up to .bak first).
