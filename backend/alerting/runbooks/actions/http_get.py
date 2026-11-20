from .network import local_path, request


class HealthCheck:
    name = "http_get"
    default_approval = "none"
    params_schema = {"url":"string"}

    async def execute(self,params,ctx):
        result = await request("GET",local_path(params["url"]),ctx)
        healthy = result.output.get("healthy") is True or result.output.get("status") in ("healthy","ok","up")
        if result.output.get("healthy") is False or result.output.get("status") in ("down","unhealthy"):
            result.ok = False
        result.resolves_alert = result.ok and (result.resolves_alert or healthy)
        return result
