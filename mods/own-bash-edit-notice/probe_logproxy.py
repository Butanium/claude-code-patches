"""Pass-through proxy to api.anthropic.com that logs /v1/messages request bodies.

Headers (including the CLI's own credential) are forwarded unchanged and never logged.
Usage: uv run --with httpx --with starlette --with uvicorn logproxy.py PORT OUT.jsonl
"""
import json
import sys

import httpx
from starlette.applications import Starlette
from starlette.responses import Response, StreamingResponse
from starlette.routing import Route

HOP = {"host", "connection", "keep-alive", "transfer-encoding", "content-length", "accept-encoding"}
RESP_STRIP = {"connection", "keep-alive", "transfer-encoding", "content-encoding", "content-length"}
PORT, OUT = int(sys.argv[1]), sys.argv[2]
client = httpx.AsyncClient(base_url="https://api.anthropic.com", timeout=httpx.Timeout(600.0, connect=30.0))


async def forward(request):
    body = await request.body()
    if request.method == "POST" and "/messages" in request.url.path:
        try:
            with open(OUT, "a") as f:
                f.write(json.dumps({"path": request.url.path, "body": json.loads(body)}) + "\n")
        except Exception as e:  # noqa: BLE001
            print("log fail", e, file=sys.stderr)
    headers = {k: v for k, v in request.headers.items() if k.lower() not in HOP}
    headers["accept-encoding"] = "identity"
    req = client.build_request(request.method, request.url.path, params=request.url.query, headers=headers, content=body)
    up = await client.send(req, stream=True)
    print(f"{request.method} {request.url.path} -> {up.status_code}", file=sys.stderr, flush=True)
    if up.status_code >= 400:
        raw = await up.aread()
        print(f"   {raw[:400]!r}", file=sys.stderr, flush=True)
        return Response(raw, status_code=up.status_code, media_type="application/json")
    out = {k: v for k, v in up.headers.items() if k.lower() not in RESP_STRIP}
    return StreamingResponse(up.aiter_bytes(), status_code=up.status_code, headers=out)


app = Starlette(routes=[Route("/{path:path}", forward, methods=["GET", "POST", "PUT", "DELETE", "HEAD"])])

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
