"""The smallest complete workflow: define it, run it, export it.

Nothing here touches a Dify server or a model, so it runs anywhere:

    python examples/01_hello_workflow.py
"""

from dify_client.workflow import Workflow, select, text_input


def build() -> Workflow:
    wf = Workflow("greeter", description="Greet someone in a chosen style.")

    start = wf.start(
        [
            text_input("name", label="Name"),
            select("style", ["formal", "casual"], label="Style"),
        ]
    )

    # A template node runs locally — no model, no credentials, no cost.
    greeting = wf.template(
        "{% if style == 'formal' %}Good evening, {{ name }}."
        "{% else %}Hey {{ name }}!{% endif %}",
        variables={"name": start["name"], "style": start["style"]},
        title="Greeting",
        id="greeting",
    )

    answer = wf.answer(greeting.output)
    wf.connect(start, greeting, answer)
    return wf


def main() -> None:
    wf = build()

    for style in ("formal", "casual"):
        result = wf.run({"name": "Dify", "style": style}, raise_on_error=True)
        print(f"{style:8} -> {result['answer']}")

    # Indexing a node gives a reference that renders either way Dify needs it.
    greeting = wf.nodes[1]
    print(f"\nreference as a template : {greeting.output}")
    print(f"reference as a selector : {greeting.output.selector}")

    path = "greeter.yml"
    wf.to_yaml(path)
    print(f"\nwrote {path} ({wf.mode} app) — import it from the Dify console")


if __name__ == "__main__":
    main()
