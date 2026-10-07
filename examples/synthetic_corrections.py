"""Temporary invented sensor data; no user's library is opened or exported."""
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory


def main():
    for name in ("ENGRAM_EXPORT_DIR", "ENGRAM_INDEX_PATH"):
        os.environ.pop(name, None)
    os.environ["ENGRAM_USAGE_TELEMETRY"] = "0"
    from engram.mcp.tools import ToolContext, call_tool

    with TemporaryDirectory(prefix="engram-synthetic-") as temporary:
        context = ToolContext.open(data_dir=Path(temporary), offline=True)
        try:
            old = call_tool(context, "remember", {
                "title": "Synthetic prism", "body": "synthetic-prism channel=amber",
            })
            call_tool(context, "remember", {
                "title": "Synthetic prism correction", "body": "synthetic-prism channel=cyan",
                "corrections": [{"target_id": old["record_id"], "scope": "whole-record",
                                 "source": "invented sensor fixture"}],
            })
            result = call_tool(context, "recall", {"query": "amber", "view": "current"})
            print(json.dumps({"fixture": "invented sensor", "current_excerpt": result["results"][0]["excerpt"],
                              "original_body": call_tool(context, "get", {"record_id": old["record_id"]})["body"]}, indent=2))
        finally:
            context.repository.connection.close()


if __name__ == "__main__":
    main()
