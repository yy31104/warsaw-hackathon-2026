from .network import local_path, request


class Webhook:
    name = "call_webhook"
    default_approval = "required"
    params_schema = {"url": "string"}

    async def execute(self, params, ctx):
        return await request("POST", local_path(params["url"]), ctx, params.get("body", {}))
