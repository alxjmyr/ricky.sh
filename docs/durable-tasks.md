# Track durable tasks

A durable task is a persistent statement of responsibility for an outcome. Use one when work must
survive the chat that discovered it or must pass between you and Ricky. Memory stores knowledge;
temporary chat tasks store model working state; workflow runs store execution of one graph.

## Choose an execution mode

| Mode | Responsibility |
|---|---|
| `agent` | Ricky owns progress and can autonomously claim and advance the task. |
| `joint` | You and Ricky share progress and can hand the next action back and forth. |
| `user` | You own progress. Ricky updates the record on your direct instruction. |

The default is `user`.

## Create a task

```bash
ricky task create \
  --title "Review renewal proposal" \
  --objective "Decide whether to accept the Northwind renewal proposal" \
  --closure-criteria "The proposal is accepted, rejected, or returned with changes" \
  --mode joint \
  --profile work \
  --tag customer:northwind \
  --tag review
```

The command returns an opaque task ID such as `task_...`. You can also ask Ricky to create or park
work during chat.

## List and inspect tasks

Commands use the configured default profile unless you pass `--profile`:

```bash
ricky task list
ricky task list --profile work
ricky task list --profile personal --include-closed

ricky task show TASK_ID --profile work
ricky task activity TASK_ID --profile work
ricky task artifacts TASK_ID --profile work
```

These commands do not construct a model provider. In a multi-profile chat or gateway route, Ricky
can aggregate tasks across every accessible profile and labels each result with its owner.

## Update tags and lifecycle

Tags are exact, lowercase coordination keys:

```bash
ricky task tag TASK_ID \
  --profile work \
  --add queue:weekly-review \
  --remove review

ricky task complete TASK_ID \
  --profile work \
  --summary "Accepted the revised proposal after legal review."

ricky task cancel TASK_ID \
  --profile work \
  --reason "The vendor withdrew the proposal."

ricky task reopen TASK_ID \
  --profile work \
  --reason "The vendor submitted a new revision."
```

Cancellation keeps the task and append-only history. Ricky does not provide a task-delete command.

## Understand coordination and artifacts

Ricky uses expiring, fenced leases so only one session can mutate a task at a time. Readers can
inspect it concurrently. If a process stops, lease expiry lets a later session recover without
trusting a stale writer.

Each profile has a separate task store and confined artifact workspace under
`<user_data_dir>/profiles/<name>/tasks/`. Always supply the owning profile for an exact CLI task
operation. Tools may access a task only when its profile is in the issued session scope.
