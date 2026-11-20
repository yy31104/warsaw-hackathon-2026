from alerting.contracts import ActionResult


class Escalate:
    name = "escalate"
    default_approval = "required"
    params_schema = {"team": "string"}

    async def execute(self, params, ctx):
        return ActionResult(ok=True, output={"escalate_to": params["team"]})
