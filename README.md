# guide-kit

Two open, local-first tools that turn your own notes into a personal development guide — using your own AI assistant, on your own machine.

**Status:** generator core and structurer both work end-to-end locally (deterministic planner + adapter + hard-fail policy; folder- and per-file classification with mandatory quarantine, freshness, sidecar overrides, pluggable media extractors — see Roadmap below). The two optional invitation steps are also built (see below).

## What it does

You bring your own notes (Obsidian, Notion export, plain files, your own database) and your own AI assistant (Claude, ChatGPT, Cursor, a local model — not required to be any specific vendor). guide-kit is two programs:

- **`structurer/`** — walks your notes and classifies them: stable facts about you, a stream of events, domain knowledge, and everything that doesn't fit a structured type. Anything that looks like someone else's private data (PII, secrets, payment info) is quarantined by default and never fed into generation without your explicit say-so.
- **`generator/`** — assembles a personal development guide (today's or this week's focus) from the structured output, using your AI assistant to write the text. A deterministic planner decides *what* to focus on; the LLM only writes, it never decides.

Everything runs locally. Your notes never leave your machine except through an explicit, opt-in call to your own LLM provider (BYOK) for the handful of files the classifier can't confidently place on its own.

## Why

Most personal-knowledge tools either lock you into their own AI, or lock your data into their own cloud. guide-kit does neither: the code is open, the method is documented, and nothing about it requires an account anywhere.

## Relationship to IWE

guide-kit is the standalone form of the same guide engine that runs [IWE](https://github.com/iwesys/IWE) (a personal work environment built on top of Claude Code) and the hosted Aisystant platform — one engine, three ways to get it: use guide-kit on its own, get it bundled inside the IWE template, or use it hosted on the platform. Connecting to either of those is entirely opt-in (see Roadmap) — guide-kit works fully standalone with no account anywhere.

## Structure

```
guide-kit/
├── structurer/   # classifies your notes into portable types, quarantines what shouldn't be indexed
└── generator/    # assembles the guide from structured output + your AI assistant
```

## Usage (generator core)

```bash
cd generator
cp ../guide-kit.config.yaml.example ../guide-kit.config.yaml   # edit: pick a backend, set a key or point at a local model
python3 adapter.py --profile profile.yaml --config ../guide-kit.config.yaml
```

No `profile.yaml`? That's a valid cold start — you get a generic first plan instead of an error. No `curriculum_path` configured? The planner picks a generic practice on its own and marks it `llm-assisted` in the decision log instead of failing or inventing a source. A required fact with no source at all — not even an LLM attempt — produces a diagnostic YAML explaining what's missing, never a silently empty or made-up guide.

## Optional next steps

The generated guide can end with two short invitation blocks — pure text, no
account, no code in guide-kit that decides anything on your behalf. guide-kit
never tracks or drives your onboarding state: whichever platform you connect
to is always asked for its own next step, and its answer is shown to you
as-is. Turn both off with `onboarding_ctas: false` in your config.

- **Connect to the hosted platform** — a pointer telling your AI agent to
  ask the platform's own MCP server what to do next. Defaults to the hosted
  Aisystant platform's public connector (`https://mcp.aisystant.com/mcp`);
  override `platform_connect_url` to point at a different platform, or set it
  to `""` to suppress the link entirely.
- **Adopt the full IWE template** — a pointer to `setup.sh` for users whose
  AI agent is Claude Code.

## Roadmap

Core logic is being extracted from a working internal prototype in stages:

1. ✅ Generator core (deterministic planner + thin LLM adapter, no cloud dependencies)
2. ✅ Structurer (folder-based + per-file classification, mandatory quarantine)
3. ✅ Two optional invitation steps (connect to a hosted service; adopt the full toolkit) — entirely opt-in, never required
4. Portability tests: your data must be exportable in under an hour, runnable on a new machine in under a day, and speak only open protocols (no vendor-locked APIs)

## License

MIT — see [LICENSE](LICENSE).
