"""The little of Twilio's REST API the phone line uses, its request signatures, and the TwiML that connects a call's
audio to Wireface. Plain HTTPS with the account SID and auth token (no SDK).

Every request Twilio makes to Wireface's phone server carries ``X-Twilio-Signature``: an HMAC-SHA1, keyed with the
auth token, of the full URL and the POSTed parameters. ``valid_signature`` checks it, so nobody else can pretend to be
Twilio and start a call session.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
from urllib.parse import urlsplit, urlunsplit
from xml.sax.saxutils import quoteattr

import aiohttp

log = logging.getLogger(__name__)

API = "https://api.twilio.com/2010-04-01"


class TwilioError(Exception):
    def __init__(self, message: str, status: int = 0, code: int | None = None):
        super().__init__(message)
        self.status = status
        self.code = code


def valid_signature(token: str, url: str, params: dict, signature: str | None) -> bool:
    """Does ``signature`` (the X-Twilio-Signature header) prove Twilio sent this request to ``url``?"""
    if not token or not signature:
        return False
    for candidate in _url_variants(url):
        data = candidate + "".join(k + str(params[k]) for k in sorted(params))
        digest = base64.b64encode(hmac.new(token.encode(), data.encode("utf-8"), hashlib.sha1).digest()).decode()
        if hmac.compare_digest(digest, signature):
            return True
    return False


def _url_variants(url: str) -> list[str]:
    """Twilio signs the URL as it called it, which may or may not include the default port."""
    parts = urlsplit(url)
    out = [url]
    default = {"https": 443, "http": 80}.get(parts.scheme)
    if parts.port is None and default:
        out.append(urlunsplit(parts._replace(netloc=f"{parts.hostname}:{default}")))
    elif parts.port == default:
        out.append(urlunsplit(parts._replace(netloc=parts.hostname)))
    return out


def stream_twiml(ws_url: str, params: dict[str, str]) -> str:
    """TwiML that connects the call's audio to ``ws_url`` (a Media Stream), then hangs up when the stream ends."""
    q = lambda v: quoteattr(str(v), {'"': "&quot;"})             # always double quotes, every quote escaped
    inner = "".join(f"<Parameter name={q(k)} value={q(v)}/>" for k, v in params.items())
    return (f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url={q(ws_url)}>{inner}</Stream>'
            f"</Connect><Hangup/></Response>")


def say_twiml(text: str) -> str:
    """TwiML that says ``text`` and hangs up (calls Wireface isn't answering)."""
    from xml.sax.saxutils import escape
    return f'<?xml version="1.0" encoding="UTF-8"?><Response><Say>{escape(text)}</Say><Hangup/></Response>'


class Twilio:
    def __init__(self, account_sid: str, auth_token: str, timeout: float = 20):
        self.sid = account_sid
        self.token = auth_token
        self.timeout = aiohttp.ClientTimeout(total=timeout)

    async def _call(self, method: str, path: str, data: dict | None = None, params: dict | None = None) -> dict:
        url = f"{API}/Accounts/{self.sid}{path}"
        try:
            async with aiohttp.ClientSession(timeout=self.timeout, auth=aiohttp.BasicAuth(self.sid, self.token)) as s:
                async with s.request(method, url, data=data, params=params) as r:
                    body = await r.json(content_type=None)
                    if r.status >= 400:
                        message = (body or {}).get("message") or f"HTTP {r.status}"
                        raise TwilioError(f"Twilio: {message}", r.status, (body or {}).get("code"))
                    return body or {}
        except (aiohttp.ClientError, TimeoutError) as e:
            raise TwilioError(f"can't reach Twilio: {e or type(e).__name__}") from None

    async def account(self) -> dict:
        """The account (to check the SID and token work)."""
        return await self._call("GET", ".json")

    async def send_sms(self, to: str, from_: str, body: str) -> dict:
        return await self._call("POST", "/Messages.json", {"To": to, "From": from_, "Body": body})

    async def inbound_sms(self, to: str, since: str) -> list[dict]:
        """Texts received on ``to`` since the date ``since`` (YYYY-MM-DD), newest first."""
        data = await self._call("GET", "/Messages.json", params={"To": to, "DateSent>": since, "PageSize": 100})
        return [m for m in data.get("messages", []) if m.get("direction") == "inbound"]

    async def call(self, to: str, from_: str, twiml: str, status_url: str | None = None, timeout_s: int = 30) -> dict:
        data = {"To": to, "From": from_, "Twiml": twiml, "Timeout": str(timeout_s)}
        if status_url:
            data.update(StatusCallback=status_url, StatusCallbackMethod="POST")
        return await self._call("POST", "/Calls.json", data)

    async def hang_up(self, call_sid: str) -> dict:
        return await self._call("POST", f"/Calls/{call_sid}.json", {"Status": "completed"})

    async def number(self, phone_number: str) -> dict | None:
        """The account's own number record (to point its calls at Wireface)."""
        data = await self._call("GET", "/IncomingPhoneNumbers.json", params={"PhoneNumber": phone_number})
        found = data.get("incoming_phone_numbers") or []
        return found[0] if found else None

    async def point_number(self, number_sid: str, voice_url: str | None, clear_sms_url: bool) -> dict:
        """Send the number's incoming calls to ``voice_url``; and with ``clear_sms_url``, stop Twilio's own demo reply
        to texts (Wireface reads texts from the message log instead)."""
        data = {}
        if voice_url:
            data.update(VoiceUrl=voice_url, VoiceMethod="POST")
        if clear_sms_url:
            data["SmsUrl"] = ""
        return await self._call("POST", f"/IncomingPhoneNumbers/{number_sid}.json", data)
