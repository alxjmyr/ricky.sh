"""Small workflow design fixture shared by inspector and browser tests."""

from pathlib import Path

SPEC = """version = 2
name = "email-triage"
description = "Classify incoming mail, draft a digest, and review before reporting."

[args.emails]
type = "string_list"
description = "Email texts to classify."

[schemas.classification]
type = "object"
required = ["category", "reason"]
[schemas.classification.properties.category]
type = "string"
values = ["billing", "support", "other"]
[schemas.classification.properties.reason]
type = "string"

[schemas.digest]
type = "object"
required = ["body"]
[schemas.digest.properties.body]
type = "string"

[[steps]]
id = "classify-emails"
kind = "foreach"
collection = { ref = "trigger.emails" }
item_key = { ref = "item.source" }
outputs = { category = { ref = "item.steps.classify.output.category" } }

[[steps.body]]
id = "classify"
kind = "agent"
instruction_file = "classify.md"
inputs = { email = { ref = "item.source" } }
result_schema = "classification"
skill = "mail-guidance"

[[steps]]
id = "context"
kind = "model"
instruction = "Explain the categories concisely."
inputs = { audience = "Inbox owner" }
result_schema = "digest"

[[steps]]
id = "draft"
kind = "model"
needs = ["classify-emails", "context"]
instruction = "Write a digest using the classified emails and category context."
result_schema = "digest"
[steps.inputs]
classifications = { ref = "steps.classify-emails.output" }
context = { ref = "steps.context.output.body" }

[[steps]]
id = "review"
kind = "approval"
mode = "confirm"
needs = ["draft"]
prompt = "Approve this digest?"
proposal = { ref = "steps.draft.output.body" }

[[steps]]
id = "report"
kind = "message"
needs = ["review"]
when = { ref = "steps.review.output.approved", is_true = true }
message = { format = "Digest: {body}", values = { body = { ref = "steps.draft.output.body" } } }
"""
INSTRUCTION = "Classify the email as billing, support, or other. Explain your decision."
GUIDANCE = "Requests for payment belong in billing. Product problems belong in support."


def write_design(root: Path) -> Path:
    bundle = root / "workflows" / "email-triage"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / "workflow.toml").write_text(SPEC)
    (bundle / "classify.md").write_text(INSTRUCTION)
    skill = root / "skills" / "mail-guidance"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(
        "---\nname: mail-guidance\ndescription: Classify mail.\n---\n\n" + GUIDANCE
    )
    return bundle
