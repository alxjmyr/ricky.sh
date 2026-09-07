# Ricky documentation

Ricky is a personal AI agent that runs from your terminal. It combines a language model with
tools, permissions, memory, reusable instructions, and structured automation.

Start with the short path if you want to use Ricky. Use the advanced path when you want to
customize how Ricky works or keep it running as a service.

## Start here

1. [Understand Ricky](overview.md)
2. [Install and configure Ricky](getting-started.md)
3. [Chat with Ricky](chat.md)
4. Add the features you need:
   - [Profiles](profiles.md)
   - [Memory](memory.md)
   - [Skills](skills.md)
   - [Browser control](browser-control.md)
   - [Browser transaction approvals](browser-transactions.md)
   - [Protected values](protected-values.md)
   - [Slack, Gmail, Google Calendar, and Web search](integrations.md)
   - [Durable tasks](durable-tasks.md)

## Build repeatable automation

- [Workflows](workflows.md) define a visible, repeatable sequence with typed data and review
  gates.
- [Jobs and schedules](jobs-and-schedules.md) run bounded work now or on a verified cron
  schedule.

## Run Ricky persistently

- [Messaging and the gateway](messaging-and-gateway.md) connect a supported messaging service
  to persistent conversations.
- [Operations](operations.md) covers recovery, health, retention, notifications, background
  executions, and delegated authority.

## Reference

- [Configuration reference](configuration.md)
- [CLI reference](cli-reference.md)
- [Data and safety model](data-and-safety.md)
- [Troubleshooting](troubleshooting.md)
- [Ask Ricky using its bundled docs](skills.md#ask-ricky-about-itself)
- [Contributor guide](https://github.com/alxjmyr/ricky.sh/blob/main/docs/development.md)

## Command convention

The user guides assume Ricky was installed as a `uv` tool. Examples use the installed command:

```bash
ricky <command>
```

Repository contributors should continue to use the sanctioned `uv run ricky`
development command from the project operating manual.
