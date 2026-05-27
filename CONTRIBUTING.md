# Contributing to Drift

Thanks for your interest. Drift is open source under [Apache License 2.0](./LICENSE) and maintained by **Scope Creep Labs LLC**. Contributions of all sizes are welcome — bug fixes, new features, docs improvements, prompt-engineering tweaks, edge-agent ports to new platforms.

## Before you start

1. **Sign the CLA.** All contributions require a signed [Individual Contributor License Agreement](./CLA.md). On your first pull request, our CLA Assistant bot posts a one-click link in the PR comments — sign once and it covers every future PR. The bot blocks merging until the signature is on file.
2. **Open an issue first for non-trivial work.** For bug fixes or small docs changes, a PR is fine. For features, architectural changes, or new tools/render-blocks, please file an issue first so we can align on scope before you spend time.
3. **Read [CLAUDE.md](./CLAUDE.md)** if you're touching agent behavior, tool definitions, or the streaming protocol. It documents the load-bearing conventions (the dataRef pattern, prompt-cache stability, SSE event ordering, etc.) that look like style choices but are actually the architecture.

## Development setup

See [README.md → Quickstart](./README.md#quickstart). The short version:

```bash
# Backend
cd drift-agent
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
uvicorn app.main:app --reload --host 127.0.0.1 --port 8000

# Frontend (separate terminal)
npm install
cat > .env.local <<EOF
VITE_ENGINE=agent
VITE_AGENT_DEV_URL=http://localhost:8000
EOF
npm run dev
```

For UI work that doesn't need real LLM calls, set `VITE_ENGINE=mock` — five hard-coded scenarios cover the streaming protocol surface.

## Type-checking

```bash
npx tsc --noEmit                                  # frontend
.venv/bin/python -c "from app.main import app"    # backend (drift-agent/)
```

There are no automated tests yet. Verification is manual end-to-end via the UI; please describe your test plan in the PR description.

## Pull requests

- **Branch off `main`.** Branch names: `feat/<short-name>`, `fix/<short-name>`, `docs/<short-name>`.
- **Keep PRs focused.** One concern per PR — easier to review, easier to revert. If you find yourself touching unrelated code, that's a second PR.
- **Match the surrounding code style.** No strict linter — read nearby files and match. Python uses type hints; TypeScript uses strict mode.
- **Don't add comments that re-state what the code does.** The repo's convention is to write self-documenting code and reserve comments for *why* — hidden constraints, workarounds for specific bugs, surprising behavior. See the comments around `_set_winsize` or the prompt-cache stability notes for examples.
- **Update docs.** If you change behavior visible to operators, update README/ARCHITECTURE/DEPLOY/ALERTING as relevant. If you add a new tool, update CLAUDE.md's "Where to add things" table.
- **Write a useful PR description.** What you changed, why, how to test it. Screenshots/GIFs for UI changes.

## What we welcome

- **New agent tools** — discovery, query, analysis, render-block emission. The extension pattern is documented in [ARCHITECTURE.md → Extension points](./ARCHITECTURE.md#extension-points).
- **New engine adapters** — Drift's frontend is engine-agnostic via the `EngineAdapter` interface (`src/adapters/`). Adding Langflow, OpenAI, a local model, etc., is a single file plus one line in `getAdapter()`.
- **New render-block types** — variant in `src/types/blocks.ts` + `drift-agent/app/schemas.py`, React component in `src/components/blocks/`, register in `BlockRenderer.tsx`, emit tool in `drift-agent/app/tools/emit.py`.
- **Edge-agent ports** — install.sh currently handles Debian/Ubuntu/Alpine and detects Synology DSM. Other distros and embedded platforms welcome.
- **Docs improvements** — typos, clarifications, missing scenarios, better examples.
- **Bug reports with repro steps.** Even better with a failing test plan.

## What we'd discuss first

- **New required dependencies.** The dependency surface is intentionally small. New deps need a clear justification.
- **Changes to the SSE protocol** or the persisted Zustand schema. These have backwards-compat implications across the frontend, backend, and stored investigations.
- **Changes to the dataRef pattern, prompt-cache stability, or propose-then-apply semantics.** These are load-bearing and documented in CLAUDE.md.
- **New LLM providers.** Happy to add them via the engine-adapter pattern, but the trust-boundary considerations (env-var credentials, prompt-cache markers, sampling-param differences per model) need discussion.

## Reporting security issues

Please **don't** open a public issue for security reports. Email **support@scopecreeplabs.com** with details. We'll respond within a few business days.

## Communication

- **Bugs / features:** GitHub issues on [kidproquo/drift-public](https://github.com/kidproquo/drift-public/issues).
- **Questions:** GitHub Discussions on the same repo.
- **Security:** support@scopecreeplabs.com (see above).

## License

By contributing, you agree your contributions are licensed under [Apache 2.0](./LICENSE) and subject to the terms of the [Individual CLA](./CLA.md), which permits Scope Creep Labs LLC to relicense the project (or portions of it) under different terms in the future. The Apache 2.0 license on the codebase itself is permanent for existing releases.

Thanks for contributing.
