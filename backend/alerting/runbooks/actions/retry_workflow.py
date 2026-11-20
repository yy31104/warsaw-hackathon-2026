from .network import request, segment


class Retry:
    name = "retry_workflow"
    default_approval = "none"
    params_schema = {"workflowId":"string"}

    async def execute(self,params,ctx):
        return await request("POST","/workflows/"+segment(params,"workflowId")+"/retry",ctx)
