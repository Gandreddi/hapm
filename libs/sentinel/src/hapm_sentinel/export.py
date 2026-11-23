"""`sentinel export`: JSON and GraphML are always complete; HTML is capped at `export.html_node_limit` nodes."""

import html
import json
from collections import Counter
from typing import Any
from xml.sax.saxutils import quoteattr

from hapm_sentinel.model import Edge, Node

COLORS = {
    "Repository": "#1f3864",
    "Service": "#2e75b6",
    "ApiEndpoint": "#c55a11",
    "Topic": "#7030a0",
    "Table": "#548235",
    "Module": "#5b9bd5",
    "Class": "#70ad47",
    "DataModel": "#a9d18e",
    "Function": "#a5a5a5",
    "Test": "#ffc000",
    "ExternalLibrary": "#bfbfbf",
    "Ticket": "#ff6699",
}


def to_json(nodes: list[Node], edges: list[Edge]) -> str:
    return json.dumps(
        {"nodes": [n.as_dict() for n in nodes], "edges": [e.as_dict() for e in edges]}, indent=1, default=str
    )


def to_graphml(nodes: list[Node], edges: list[Edge]) -> str:
    out = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<graphml xmlns="http://graphml.graphdrawing.org/xmlns">',
        '<key id="type" for="node" attr.name="type" attr.type="string"/>',
        '<key id="label" for="node" attr.name="label" attr.type="string"/>',
        '<key id="repo" for="node" attr.name="repo" attr.type="string"/>',
        '<key id="relation" for="edge" attr.name="relation" attr.type="string"/>',
        '<key id="confidence" for="edge" attr.name="confidence" attr.type="string"/>',
        '<graph id="sentinel" edgedefault="directed">',
    ]
    for n in nodes:
        out.append(
            f'<node id={quoteattr(n.id)}><data key="type">{html.escape(n.type)}</data><data key="label">{html.escape(n.label)}</data>'
            f'<data key="repo">{html.escape(n.repo)}</data></node>'
        )
    for i, e in enumerate(edges):
        out.append(
            f'<edge id="e{i}" source={quoteattr(e.source)} target={quoteattr(e.target)}><data key="relation">{e.relation}</data>'
            f'<data key="confidence">{e.confidence}</data></edge>'
        )
    out += ["</graph>", "</graphml>"]
    return "\n".join(out)


def select_for_html(nodes: list[Node], edges: list[Edge], limit: int) -> tuple[list[Node], list[Edge], int]:
    """Keep every tier 1 node first, then the highest-degree nodes, up to `limit`."""
    if len(nodes) <= limit:
        return nodes, edges, 0
    degree = Counter([e.source for e in edges] + [e.target for e in edges])
    ordered = sorted(nodes, key=lambda n: (n.tier != 1, -degree[n.id], n.id))
    kept = ordered[:limit]
    ids = {n.id for n in kept}
    return kept, [e for e in edges if e.source in ids and e.target in ids], len(nodes) - len(kept)


def to_html(nodes: list[Node], edges: list[Edge], limit: int, title: str = "Sentinel graph") -> str:
    shown, shown_edges, left_out = select_for_html(nodes, edges, limit)
    data: dict[str, Any] = {
        "nodes": [
            {
                "id": n.id,
                "label": n.label,
                "group": n.type,
                "title": f"{n.type} in {n.repo}",
                "color": COLORS.get(n.type, "#888"),
            }
            for n in shown
        ],
        "edges": [
            {
                "from": e.source,
                "to": e.target,
                "label": e.relation,
                "arrows": "to",
                "dashes": e.confidence != "EXTRACTED",
            }
            for e in shown_edges
        ],
    }
    note = (
        f"Showing {len(shown)} of {len(nodes)} nodes; {left_out} left out (limit {limit}). JSON and GraphML exports are complete."
        if left_out
        else f"Showing all {len(nodes)} nodes."
    )
    payload = json.dumps(data).replace("</", "<\\/")
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/vis-network/9.1.9/standalone/umd/vis-network.min.js"></script>
<style>body{{margin:0;font-family:system-ui,sans-serif;background:#fff;color:#111}}#bar{{padding:8px 16px;border-bottom:1px solid #ddd}}#g{{height:calc(100vh - 44px)}}</style>
</head><body><div id="bar"><strong>{html.escape(title)}</strong> &middot; {html.escape(note)} Dashed edges are INFERRED or AMBIGUOUS.</div><div id="g"></div>
<script>const d={payload};new vis.Network(document.getElementById("g"),{{nodes:new vis.DataSet(d.nodes),edges:new vis.DataSet(d.edges)}},{{physics:{{stabilization:true}},edges:{{font:{{size:9}}}}}});</script>
</body></html>"""
