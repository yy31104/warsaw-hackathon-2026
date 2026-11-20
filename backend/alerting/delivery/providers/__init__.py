from .email import Email
from .telecom import Telecom
from .ui import UI


def registry(client):
    providers = [Email(), Telecom("phone", client), Telecom("sms", client), UI()]
    return {p.channel.value: p for p in providers}
