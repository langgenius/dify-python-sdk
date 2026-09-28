"""Canvas positions for generated workflows.

Dify's editor draws nodes where the DSL says, so a generated workflow needs
coordinates or every node lands on top of the others at the origin. Nodes are
laid out left to right by their distance from the start node.
"""

from __future__ import annotations

from collections import defaultdict, deque

COLUMN_WIDTH = 304
ROW_HEIGHT = 168
ORIGIN_X = 80
ORIGIN_Y = 282


def assign_positions(
    node_ids: list[str],
    edges: list[tuple[str, str]],
) -> dict[str, dict[str, int]]:
    """Return an ``{id: {"x": .., "y": ..}}`` map for the given graph."""
    successors: dict[str, list[str]] = defaultdict(list)
    indegree: dict[str, int] = {node_id: 0 for node_id in node_ids}
    for source, target in edges:
        successors[source].append(target)
        if target in indegree:
            indegree[target] += 1

    depth = {node_id: 0 for node_id in node_ids}
    queue = deque(node_id for node_id in node_ids if indegree[node_id] == 0)
    seen = set(queue)
    while queue:
        current = queue.popleft()
        for nxt in successors[current]:
            if nxt not in depth:
                continue
            depth[nxt] = max(depth[nxt], depth[current] + 1)
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)

    # Cycles (loops, iterations) leave nodes unvisited; place them after the
    # deepest node that was reachable so they still land somewhere sensible.
    rows: dict[int, int] = defaultdict(int)
    positions: dict[str, dict[str, int]] = {}
    for node_id in node_ids:
        column = depth[node_id]
        row = rows[column]
        rows[column] += 1
        positions[node_id] = {
            "x": ORIGIN_X + column * COLUMN_WIDTH,
            "y": ORIGIN_Y + row * ROW_HEIGHT,
        }
    return positions
