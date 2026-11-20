from .network import request


class Ticket:
    name = "open_ticket"
    default_approval = "none"
    params_schema = {"title": "string"}

    async def execute(self, params, ctx):
        return await request("POST", "/tickets", ctx, params)
