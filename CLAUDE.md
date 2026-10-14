## Product voice

The bot must feel like a real, good human assistant, everywhere: onboarding, the agent, fixed
replies, errors. This applies to every change that touches what a person reads or does in chat.

- Write every user-facing line as a warm, capable person would text it. No menus, numbered
  options, "reply with 1/2", IDs or jargon (say "Jerusalem", not `Asia/Jerusalem`).
- Decide instead of asking. If a sensible default exists (a zone from the phone number, an hour
  for a meeting), apply it, say what was assumed, and let a plain-words reply change it. Ask only
  what can't be inferred, one question at a time.
- Understand free text. Never require an exact keyword or format; use the model or a forgiving
  match. Confirmation stays only where an action writes somewhere real (adding an event), and
  even then the answer is read from natural language.
- Keep both languages equally natural; Hebrew copy is written as Hebrew, not translated English.
- Voice lives in `messaging/onboarding_text.py`, `messaging/onboarding_prompt.py`,
  `messaging/confirm.py` and the prompt in `messaging/agent.py`. Change it there.

## graphify

This project has a knowledge graph at graphify-out/ with god nodes, community structure, and cross-file relationships.

Rules:
- For codebase questions, first run `graphify query "<question>"` when graphify-out/graph.json exists. Use `graphify path "<A>" "<B>"` for relationships and `graphify explain "<concept>"` for focused concepts. These return a scoped subgraph, usually much smaller than GRAPH_REPORT.md or raw grep output.
- If graphify-out/wiki/index.md exists, use it for broad navigation instead of raw source browsing.
- Read graphify-out/GRAPH_REPORT.md only for broad architecture review or when query/path/explain do not surface enough context.
- After modifying code, run `PYTHONHASHSEED=0 graphify update .` (prefix works around a Nix packaging bug) to keep the graph current (AST-only, no API cost).
