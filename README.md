# guide-kit

Two open, local-first tools that turn your own notes into a personal development guide — using your own AI assistant, on your own machine.

**Status:** scaffold only. Not yet functional — core is being extracted from an internal prototype (see Roadmap below).

## What it does

You bring your own notes (Obsidian, Notion export, plain files, your own database) and your own AI assistant (Claude, ChatGPT, Cursor, a local model — not required to be any specific vendor). guide-kit is two programs:

- **`structurer/`** — walks your notes and classifies them: stable facts about you, a stream of events, domain knowledge, and everything that doesn't fit a structured type. Anything that looks like someone else's private data (PII, secrets, payment info) is quarantined by default and never fed into generation without your explicit say-so.
- **`generator/`** — assembles a personal development guide (today's or this week's focus) from the structured output, using your AI assistant to write the text. A deterministic planner decides *what* to focus on; the LLM only writes, it never decides.

Everything runs locally. Your notes never leave your machine except through an explicit, opt-in call to your own LLM provider (BYOK) for the handful of files the classifier can't confidently place on its own.

## Why

Most personal-knowledge tools either lock you into their own AI, or lock your data into their own cloud. guide-kit does neither: the code is open, the method is documented, and nothing about it requires an account anywhere.

## Structure

```
guide-kit/
├── structurer/   # classifies your notes into portable types, quarantines what shouldn't be indexed
└── generator/    # assembles the guide from structured output + your AI assistant
```

## Roadmap

This repository is currently a scaffold (folders + license only). Core logic is being extracted from a working internal prototype in stages:

1. Generator core (deterministic planner + thin LLM adapter, no cloud dependencies)
2. Structurer (folder-based + per-file classification, mandatory quarantine)
3. Two optional invitation steps (connect to a hosted service; adopt the full toolkit) — entirely opt-in, never required
4. Portability tests: your data must be exportable in under an hour, runnable on a new machine in under a day, and speak only open protocols (no vendor-locked APIs)

## License

MIT — see [LICENSE](LICENSE). Provisional; confirm before any public release.
