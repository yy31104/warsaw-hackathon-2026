from .network import request, segment


class Restart:
    name = "restart_service"
    default_approval = "required"
    params_schema = {"service": "string"}

    async def execute(self, params, ctx):
        return await request("POST", "/services/" + segment(params, "service") + "/restart", ctx)
