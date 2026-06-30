---
name: load-context
description: Use at the start of any session, or when the user says "start", "load context", "pre-load memory", or "what do we know about X", BEFORE doing real work. Calls the NeoHive `memory_context` MCP tool with the current task description so directives, conventions, and relevant prior knowledge land in context before exploration or editing begins.
---

# Load NeoHive Context for This Session

Call the `memory_context` MCP tool immediately to pre-load relevant knowledge for this session. The tool name in this opencode instance is typically prefixed with the MCP server name — e.g. `neohive-gist_memory_context`. Prefer this skill over manually crafting an opening `memory_recall` query — `memory_context` is tuned to load both directives (project rules) and task-specific snippets in one pass.

## Task Description

If the user provided arguments after the skill invocation: use those as the task description.

If no arguments were provided: summarize the current task based on conversation context so far. If there is no context yet, ask the user what they're working on.

Phrase the task affirmatively with specific domain terms:
- GOOD: `"implementing OAuth refresh flow in the auth proxy"`
- BAD: `"how does auth work?"`

## After Loading

Once `memory_context` returns, briefly report:
- How many relevant memories were loaded
- The key topics/directives that were surfaced (1-3 bullet points max)
- Whether any directives or conventions were found that should guide this session

Then proceed with whatever the user asked for. Do not ask for confirmation to continue.
