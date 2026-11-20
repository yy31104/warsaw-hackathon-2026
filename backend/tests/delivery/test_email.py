import asyncio
from email import message_from_bytes

from alerting.contracts import RenderedMessage
from alerting.delivery.providers.email import Email


async def test_smtp_measurement_headers(monkeypatch):
    """Exercise smtplib over a real socket without adding mail to a shared inbox."""
    received = []

    async def smtp(reader, writer):
        writer.write(b"220 localhost ESMTP\r\n")
        try:
            while line := await reader.readline():
                cmd = line.split()[0].upper()
                if cmd == b"DATA":
                    writer.write(b"354 send message\r\n")
                    await writer.drain()
                    data = await reader.readuntil(b"\r\n.\r\n")
                    received.append(message_from_bytes(data[:-3]))
                elif cmd == b"QUIT":
                    writer.write(b"221 bye\r\n")
                    await writer.drain()
                    return
                writer.write(b"250 OK\r\n")
                await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(smtp, "127.0.0.1", 0)
    async with server:
        monkeypatch.setenv("SMTP_HOST", "127.0.0.1")
        monkeypatch.setenv("SMTP_PORT", str(server.sockets[0].getsockname()[1]))
        message = RenderedMessage(
            title="Test alert", body="Details", short="Short", meta={"gen_sent_at": 12345}
        )
        result = await Email().send(message, "tester@example.com", "stable-key")
    assert result.outcome == "delivered"
    (mail,) = received
    assert (mail["X-Notification-Id"], mail["X-Gen-Sent-At"]) == ("stable-key", "12345")
