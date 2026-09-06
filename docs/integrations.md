# Connect external services

Ricky conditionally loads an integration only when its required credential and account
configuration are present. Use integrations from `ricky chat`, not `ricky ask`.

External integrations do not choose a model provider for you. Before you expose work data, start
chat with a provider approved for that data. For example:

```bash
ricky chat --provider claude_code
```

## Search the Web

Use Web search for current public facts, cited research, or comparisons. Do not include secrets,
private messages, private file content, or unnecessary personal data in a query.

Set the Brave Search key:

```toml
# <user_data_dir>/profiles/<name>/.secrets.toml
brave_search_api_key = "..."
```

Then ask Ricky naturally:

```text
Search the Web for the current Python 3.14 release status. Use quick effort and cite sources.
```

Brave is the only implemented Web search provider. The effort levels are:

| Effort | Use it for |
|---|---|
| `quick` | One narrow factual lookup |
| `standard` | A claim that benefits from independent corroboration |
| `deep` | A broad comparison or explicit research request |

Ricky can restrict freshness to a day, week, month, or year. Results contain bounded excerpts and
source URLs, not complete pages. Ricky labels result content as untrusted and should treat it as
evidence, not instructions. Search is read-only and writes no files.

## Connect Slack

Ricky acts as you through a Slack user token.

1. Create or update a Slack app using the repository's
   [Slack app manifest](https://github.com/alxjmyr/ricky.sh/blob/main/.designs/assets/slack-app-manifest.yaml).
2. Install or reinstall the app in your workspace.
3. Add the resulting user OAuth token to the owning profile's `.secrets.toml`:

   ```toml
   slack_user_token = "xoxp-..."
   ```

4. Verify the token and authenticated identity:

   ```bash
   ricky config slack
   ```

   Ricky checks every enabled profile that declares Slack settings or a Slack token and labels each
   result with the owning profile.

The manifest defines the required user-token scopes.

### Use Slack

Ask Ricky to:

- List channels, direct messages, group direct messages, or unread conversations.
- Find a user by name or email.
- Search messages with Slack search syntax.
- Read recent messages or a complete thread.
- Send a root message or thread reply.
- Mark a conversation read.
- Download a Slack file.

Use unread listing for “what is new?” Slack search does not distinguish read messages from unread
messages.

For a real mention, Ricky must use the `<@U...>` identifier returned by user lookup. Plain
`@name` text does not notify the person. Sends accept exact channel names, exact handles, or
explicit IDs; partial-name matching is for discovery only.

Reads run automatically by default. Sending, marking read, and downloading require permission.
Marking read cannot be reversed by Ricky. If a send becomes ambiguous after a network failure,
inspect Slack before you retry.

## Connect Google accounts

Gmail and Google Calendar share profile-qualified Google accounts. Each profile has its own OAuth
token store.

Download a Desktop OAuth client JSON file from your Google Cloud project. Create the account in an
existing profile with its expected email address:

```bash
ricky config google add personal --profile personal --email you@example.com \
  --client-json /path/to/client_secret.json
```

The command writes the account identity to the profile's `ricky.toml` and imports the OAuth client
into its private `.secrets.toml`, preserving existing settings. It refuses an existing account name
in that profile. The same name can exist in another profile. The JSON file is read locally and is
not changed; keep it private. The command does not contact Google or start consent.

Authorize the new account and check both services:

```bash
ricky config google auth personal/personal
ricky config google
ricky config gmail
ricky config gcal
```

Ricky verifies that the authorized email matches the configured email before it stores the refresh
token. Tokens live below the owning profile and use owner-only file permissions on POSIX systems.

The consent flow requests identity, Gmail modify, Calendar read-only, and Calendar events scopes.
Granular consent is supported, so one service can remain unavailable when you do not grant its
scope.

### Authorize Google on a remote host

Start authorization on the remote host with a fixed callback port:

```bash
ricky config google auth personal/personal --no-browser --callback-port 8765
```

Keep that command running. On your local machine, forward the same local port to the remote
loopback address:

```bash
ssh -N -o ExitOnForwardFailure=yes \
  -L 8765:127.0.0.1:8765 \
  USER@REMOTE_HOST
```

Open the consent URL printed by Ricky in your local browser. Google's redirect to
`127.0.0.1:8765` travels through the tunnel to Ricky's callback server.

## Use Gmail

Refer to an account by its qualified key, such as `personal/home` or `work/company`, not by its
email address. Chat sessions receive the exact accessible account ids and configured email
identities as model context. Tool and workflow calls still require the exact qualified id.

Ricky can read:

- Gmail search results using Gmail query syntax
- Complete messages and chronological threads
- Labels
- Existing Gmail drafts

Ricky can also:

- Create a real Gmail draft.
- Send a new message or threaded reply to explicit recipients.
- Add or remove exact labels.
- Create a label.
- Archive by removing `INBOX` or mark read by removing `UNREAD`.
- Move a read message or thread to Trash.
- Download an attachment.

Every change requires permission. Draft and send previews include the account, exact recipients,
complete body, reply target, and attachments. Ricky does not permit direct mutation of the `SENT`
or `DRAFT` labels.

Trash can normally be reversed in Gmail for a limited time, but Ricky treats the operation as a
destructive action. If a mutation becomes ambiguous, inspect Gmail before retrying.

## Use Google Calendar

Ricky can read:

- Visible calendars and their raw IDs
- Events in a time window
- One exact event
- Busy blocks across explicit calendars

Ricky can create and update events, respond to invitations, and delete events. Every change
requires permission and can notify attendees or the organizer.

Use calendar and event IDs returned by an earlier read. Event listing defaults to the next seven
days. Availability reports busy blocks; free time is the remainder of the interval you requested.

Use ISO dates or date-times. For all-day events, the end date is exclusive. Supply an IANA timezone
when the account's calendar timezone is not the intended one.

For recurring events, an expanded instance ID targets one occurrence. A series ID targets the
entire series. Confirm the target shown in the permission preview, especially before update or
deletion.

## Send and download attachments

Slack sends and Gmail drafts or sends can attach:

- An ordinary local file addressed by a project-relative, `~` home-relative, or absolute host path
- A durable-task artifact referenced by task ID, artifact path, and owning profile
- A browser download referenced by the complete logical object returned by `browser_download`

Use logical durable-task and browser-download references. Do not construct an artifact's physical
storage path. A browser reference binds its opaque ID, owner, plain filename, media type, size, and
SHA-256 digest; its owner must remain in the active profile scope.

Default outbound limits are:

| Service | Maximum files | Maximum per file | Maximum total |
|---|---:|---:|---:|
| Slack | 10 | 20 MB | 50 MB |
| Gmail | 10 | 20 MB | 20 MB |

The permission preview lists attachment sources before a draft or send. Ricky loads the exact
bytes once for that attempted action, so later file changes cannot silently alter the reviewed
effect. Directories and non-files are rejected.

Ordinary files can come from outside the active project. A generic path cannot attach Ricky-owned
private state or configuration, including `.secrets.toml`, `ricky.toml`, `.ricky/`, tokens,
databases, sessions, or other files below `user_data_dir`. Files in the configured Gmail and Slack
download directories remain attachable, and durable-task artifacts from accessible profiles remain
available through their logical references.

Downloads are local mutations and require permission. Slack downloads use the profile that owns
the selected credential, then fall back to the profile that owns the Slack configuration and the
installation root. Gmail downloads use the profile that owns the selected Google account, under
`<user_data_dir>/profiles/<name>/downloads/gmail`. Downloads remain attachable only while their
owning profile is in the active scope. Browser downloads from Ricky-owned sessions use
`<user_data_dir>/profiles/<name>/downloads/browser` and follow the same logical-reference rule.
