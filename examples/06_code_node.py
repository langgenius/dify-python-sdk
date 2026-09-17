"""Running a code node without Dify's sandbox service.

A code node normally posts its program to `dify-sandbox`, so in a local test it
fails with a connection error. Two ways round that, neither needing Docker:

    python examples/06_code_node.py
"""

from dify_client.workflow import (
    LocalSandbox,
    SandboxUnavailable,
    StubCode,
    Workflow,
    paragraph,
)

SOURCE = """
def main(text):
    words = [w.strip('.,!?').lower() for w in text.split()]
    longest = max(words, key=len) if words else ''
    return {'words': len(words), 'longest': longest}
"""


def build() -> Workflow:
    wf = Workflow("word-stats", description="Count words and find the longest.")
    start = wf.start([paragraph("text", label="Text")])
    stats = wf.code(
        SOURCE,
        variables={"text": start["text"]},
        outputs={"words": "number", "longest": "string"},
        title="Word stats",
        id="stats",
    )
    answer = wf.answer("{{#stats.words#}} words, longest: {{#stats.longest#}}")
    wf.connect(start, stats, answer)
    return wf


TEXT = "The migration failed under production load on Friday afternoon."


def main() -> None:
    wf = build()

    # 1. A stub: the node is not what is under test, so do not run it.
    result = wf.run(
        {"text": TEXT}, code=StubCode({"words": 9, "longest": "production"})
    )
    print(f"StubCode     -> {result['answer']}")

    # 2. A local sandbox: your actual code runs, confined by the OS. No Docker.
    try:
        result = wf.run({"text": TEXT}, code=LocalSandbox(), raise_on_error=True)
        print(f"LocalSandbox -> {result['answer']}")
        print(f"                node outputs: {result.node('stats').outputs}")
    except SandboxUnavailable as error:
        print(f"LocalSandbox -> unavailable here: {error}")

    # Your project's packages are hidden, because Dify's sandbox has never
    # heard of them. Name the ones your deployment does have.
    imports = Workflow("imports")
    start = imports.start([paragraph("text")])
    probe = imports.code(
        "def main(text):\n"
        "    import sys\n"
        "    names = []\n"
        "    for mod in ('json', 'httpx', 'pandas'):\n"
        "        try:\n"
        "            __import__(mod)\n"
        "            names.append(mod)\n"
        "        except Exception:\n"
        "            pass\n"
        "    return {'r': ','.join(names)}",
        variables={"text": start["text"]},
        outputs={"r": "string"},
        id="probe",
    )
    imports.connect(start, probe, imports.answer(probe["r"]))
    print("\nwhat a code node can import:")
    for label, sandbox in (
        ("default          ", LocalSandbox()),
        ("packages=[httpx] ", LocalSandbox(packages=["httpx"])),
    ):
        got = imports.run({"text": "x"}, code=sandbox, raise_on_error=True)
        print(f"  {label} -> {got.node('probe')['r']}")

    # The confinement is real: this node is refused the network.
    reaching_out = Workflow("phone-home")
    start = reaching_out.start([paragraph("text")])
    node = reaching_out.code(
        "import urllib.request\n"
        "def main(text):\n"
        "    urllib.request.urlopen('https://example.com', timeout=5)\n"
        "    return {'r': 'reached the internet'}",
        variables={"text": start["text"]},
        outputs={"r": "string"},
        id="out",
    )
    reaching_out.connect(start, node, reaching_out.answer(node["r"]))
    blocked = reaching_out.run({"text": "x"}, code=LocalSandbox())
    print(
        f"\nnetwork from a code node -> {'allowed' if blocked.succeeded else 'blocked'}"
    )

    print(
        "\nno Docker was involved. If you need the exact confinement, point a "
        "dify-sandbox you already run at\n"
        "run(credentials={'code': {'execution_endpoint': ..., "
        "'execution_api_key': ...}})."
    )


if __name__ == "__main__":
    main()
