---
description: Full llm-scrub CLI surface — invoke any subcommand with arguments
argument-hint: [subcommand] [args...]
---

You are running aurochs-llm-scrub on behalf of the user. The user invoked `/aurochs-llm-scrub` with these arguments: $ARGUMENTS

Run the llm-scrub CLI with the provided arguments via the Bash tool:

```bash
llm-scrub $ARGUMENTS
```

If `$ARGUMENTS` is empty, run `llm-scrub --help` to show the user the available subcommands.

If llm-scrub is not installed (you see `llm-scrub: command not found` or equivalent), inform the user they need to `pip install aurochs-llm-scrub` first.

Show the user the output. If the output indicates an error, help them interpret it and suggest a fix.
