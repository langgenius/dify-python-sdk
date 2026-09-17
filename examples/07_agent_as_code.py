"""Keeping a Dify Agent in version control.

An Agent's configuration — its *soul* — is defined by models that live in the
Dify server and are not published as a package. Workflow nodes come from
``graphon``, so this SDK can type-check them; there is nothing equivalent for
Agents yet.

So the route is **export first**: configure the Agent once in Dify, export it,
commit the YAML, and from then on edit it here. The soul below was checked
against a running Dify's own ``AgentPackage`` model — inventing one by hand is
how you learn that ``model`` needs ``plugin_id`` and ``model_provider``, from
an import error.

    python examples/07_agent_as_code.py
"""

import os

from dify_client import Agent, DifyManagement

# Stands in for `DifyManagement().apps.export(APP_ID)` so this example runs with
# nothing configured. The shape is what Dify's export returns.
EXPORTED = """
app:
  name: support-triage
  mode: agent
  icon_type: emoji
  icon: "\U0001f9ed"
  icon_background: '#FFEAD5'
  description: Triage inbound support messages.
  use_icon_as_answer_icon: false
kind: app
version: 0.7.0
agent:
  package_ref: agent_1
agent_packages:
  agent_1:
    schema_version: 1
    metadata:
      name: support-triage
      description: Triage inbound support messages.
      role: Support engineer
    soul:
      schema_version: 1
      prompt:
        system_prompt: Read the message and decide whether it needs paging.
      model:
        plugin_id: langgenius/openai
        model_provider: langgenius/openai/openai
        model: gpt-4o-mini
        credential_ref: null
      tools:
        dify_tools: []
        cli_tools: []
"""


def main() -> None:
    app_id = os.environ.get("DIFY_AGENT_APP_ID")
    if app_id and os.environ.get("DIFY_CONSOLE_TOKEN"):
        print(f"exporting agent app {app_id} from Dify…")
        source = DifyManagement().apps.export(app_id)
    else:
        print("using a stored export (set DIFY_CONSOLE_TOKEN and DIFY_AGENT_APP_ID")
        print("to pull one from your own Dify instead)\n")
        source = EXPORTED

    agent = Agent.from_yaml(source)
    print(f"read: {agent!r}")
    print(f"system prompt : {agent.soul['prompt']['system_prompt']}")
    print(f"model         : {agent.soul['model']['model']}")

    # The soul is plain data. Editing it is editing a dict — no schema here to
    # validate against, which is exactly why it came from an export.
    agent.soul["prompt"]["system_prompt"] = (
        "Read the message and decide whether it needs paging. "
        "When in doubt, do not page."
    )
    agent.soul["model"]["model"] = "gpt-4o"
    agent.role = "On-call triage"

    path = "support-triage.yml"
    agent.to_yaml(path)
    print(f"\nwrote {path} — commit it, and diff the next change")

    # Credentials are stripped on the way out, so the file is safe to commit.
    text = agent.to_yaml()
    assert "credential_ref: null" in text

    print("\nto deploy it back:")
    print("    console = DifyManagement()")
    print("    result = console.apps.deploy(agent)   # -> Deployment")
    print("    result.raise_for_stage()              # or inspect result.stage")
    print("\nDify's importer only creates new Agent apps — it will not overwrite")
    print("one — so keep the id it returns rather than expecting a stable target.")

    os.remove(path)


if __name__ == "__main__":
    main()
