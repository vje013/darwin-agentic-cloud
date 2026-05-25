# Autonomous Coding Agent Inventory

A catalog of autonomous coding agents on GitHub that can contribute to Darwin Agentic Cloud.

## Agents

### Aider

| Field | Details |
|-------|---------|
| Repository | [paul-gauthier/aider](https://github.com/paul-gauthier/aider) |
| Mention syntax | N/A — CLI-based, not GitHub-native |
| Setup requirements | `pip install aider-chat`, requires API key for LLM provider (OpenAI, Anthropic, etc.) |
| Best for | Local code editing, refactoring, and feature implementation |

**Paste-ready mention block:**

```text
N/A — Aider is a CLI tool. Invoke locally:
aider --message "your task description here"
```

---

### Devin (Cognition)

| Field | Details |
|-------|---------|
| Repository | Proprietary (cognition-labs) |
| Mention syntax | Invite Devin to your repository, then assign issues or mention in comments |
| Setup requirements | Cognition account, repository access grant, Devin GitHub App installed |
| Best for | End-to-end feature implementation, multi-file changes, debugging |

**Paste-ready mention block:**

```text
@devin-ai-integration Please implement this task.
```

---

### GitHub Copilot Coding Agent

| Field | Details |
|-------|---------|
| Repository | GitHub-native (no separate repo) |
| Mention syntax | `@copilot` in issue or PR comments |
| Setup requirements | GitHub Copilot Enterprise or Pro+ plan, repository must have Copilot enabled |
| Best for | Issue resolution, PR creation, code review fixes |

**Paste-ready mention block:**

```text
@copilot Please implement this change following CONTRIBUTING.md guidelines.
```

---

### Cursor Composer

| Field | Details |
|-------|---------|
| Repository | Proprietary (Cursor IDE) |
| Mention syntax | N/A — IDE-based, not GitHub-native |
| Setup requirements | Cursor IDE installed, project opened locally |
| Best for | Multi-file edits, refactoring, feature scaffolding within IDE |

**Paste-ready mention block:**

```text
N/A — Cursor Composer is an IDE feature. Use within Cursor:
Cmd+I / Ctrl+I → describe your task
```

---

### Codex CLI (OpenAI)

| Field | Details |
|-------|---------|
| Repository | [openai/codex](https://github.com/openai/codex) |
| Mention syntax | N/A — CLI-based, not GitHub-native |
| Setup requirements | `npm install -g @openai/codex`, OpenAI API key |
| Best for | Terminal-based code generation, quick edits, scripting |

**Paste-ready mention block:**

```text
N/A — Codex CLI is a terminal tool. Invoke locally:
codex "your task description here"
```

---

### Windsurf (Codeium)

| Field | Details |
|-------|---------|
| Repository | Proprietary (Codeium) |
| Mention syntax | N/A — IDE-based, not GitHub-native |
| Setup requirements | Windsurf IDE installed, project opened locally |
| Best for | Multi-file edits, codebase-wide refactoring, Cascade flows |

**Paste-ready mention block:**

```text
N/A — Windsurf is an IDE. Use within Windsurf:
Open Cascade → describe your task
```

---

### Sweep

| Field | Details |
|-------|---------|
| Repository | [sweepai/sweep](https://github.com/sweepai/sweep) |
| Mention syntax | `sweep:` prefix in issue title, or comment with instructions |
| Setup requirements | Sweep GitHub App installed on repository |
| Best for | Bug fixes, small features, test writing, documentation |

**Paste-ready mention block:**

```text
Create an issue with title prefixed by "sweep:" or comment:
/sweep Implement this task following CONTRIBUTING.md guidelines.
```

---

### PR-Agent (Codium / Qodo)

| Field | Details |
|-------|---------|
| Repository | [Codium-ai/pr-agent](https://github.com/Codium-ai/pr-agent) |
| Mention syntax | `/review`, `/improve`, `/describe` in PR comments |
| Setup requirements | PR-Agent GitHub App installed, or self-hosted with API keys |
| Best for | PR review, code improvement suggestions, PR descriptions |

**Paste-ready mention block:**

```text
Comment on a PR:
/review
/improve
/describe
```

---

## Comparison Matrix

| Agent | GitHub-native | Autonomous PRs | Issue-triggered | Self-hosted option |
|-------|:---:|:---:|:---:|:---:|
| Aider | ❌ | ❌ | ❌ | ✅ |
| Devin | ✅ | ✅ | ✅ | ❌ |
| Copilot Coding Agent | ✅ | ✅ | ✅ | ❌ |
| Cursor Composer | ❌ | ❌ | ❌ | ✅ |
| Codex CLI | ❌ | ❌ | ❌ | ✅ |
| Windsurf | ❌ | ❌ | ❌ | ✅ |
| Sweep | ✅ | ✅ | ✅ | ✅ |
| PR-Agent | ✅ | ❌ | ❌ | ✅ |

## Usage with Darwin Agentic Cloud

For GitHub-native agents (Copilot, Sweep, Devin), create issues labeled `agent-friendly` with:
- Clear acceptance criteria
- Links to relevant source files
- Expected test coverage
- Reference to CONTRIBUTING.md conventions

For CLI/IDE agents (Aider, Codex, Cursor, Windsurf), ensure the local environment is configured per the [Development setup](../CONTRIBUTING.md#development-setup) section.
