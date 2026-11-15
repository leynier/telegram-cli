"""OAuth 2.1 authorization for the MCP server, approved with a clitg pairing code."""

from __future__ import annotations

import html
from typing import Any
from urllib.parse import urlparse

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from clitg.errors import ClitgError
from clitg.mcp_store import McpStore

SCOPE = "clitg"
PAIR_PATH = "/pair"
_SECURITY_HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class ClitgOAuthProvider(
    OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]
):
    """Authorization server storing hashed grants for one clitg profile."""

    def __init__(self, store: McpStore, profile: str, issuer_url: str, resource_url: str) -> None:
        self.store = store
        self.profile = profile
        self.issuer_url = issuer_url.rstrip("/")
        self.resource_url = resource_url

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        info = self.store.get_client(self.profile, client_id)
        return None if info is None else OAuthClientInformationFull.model_validate(info)

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        if client_info.client_id is None:
            raise RegistrationError("invalid_client_metadata", "client_id is required")
        try:
            self.store.register_client(
                self.profile, client_info.client_id, client_info.model_dump(mode="json")
            )
        except ClitgError as exc:
            raise RegistrationError("invalid_client_metadata", str(exc)) from exc

    async def authorize(
        self, client: OAuthClientInformationFull, params: AuthorizationParams
    ) -> str:
        request_id = self.store.save_auth_request(
            self.profile, str(client.client_id), params.model_dump(mode="json")
        )
        return f"{self.issuer_url}{PAIR_PATH}?request={request_id}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        data = self.store.load_auth_code(self.profile, str(client.client_id), authorization_code)
        if data is None:
            return None
        return AuthorizationCode(code=authorization_code, client_id=str(client.client_id), **data)

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        if not self.store.delete_auth_code(self.profile, authorization_code.code):
            raise TokenError("invalid_grant", "Authorization code was already used")
        return self._issue(client, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: str
    ) -> RefreshToken | None:
        record = self.store.load_token(self.profile, refresh_token, ("refresh",))
        if record is None or record["client_id"] != client.client_id:
            return None
        return RefreshToken(
            token=refresh_token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=record["resource"],
        )

    async def exchange_refresh_token(
        self,
        client: OAuthClientInformationFull,
        refresh_token: RefreshToken,
        scopes: list[str],
    ) -> OAuthToken:
        record = self.store.load_token(self.profile, refresh_token.token, ("refresh",))
        if record is None:
            raise TokenError("invalid_grant", "Refresh token is no longer valid")
        self.store.revoke_family(self.profile, record["family"])
        return self._issue(client, scopes or refresh_token.scopes, refresh_token.resource)

    async def load_access_token(self, token: str) -> AccessToken | None:
        record = self.store.load_token(self.profile, token, ("access", "static"))
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource_url if record["kind"] == "static" else record["resource"],
        )

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        record = self.store.load_token(self.profile, token.token, ("access", "refresh"))
        if record is not None:
            self.store.revoke_family(self.profile, record["family"])

    def _issue(
        self, client: OAuthClientInformationFull, scopes: list[str], resource: str | None
    ) -> OAuthToken:
        grant = self.store.issue_token_pair(
            self.profile, str(client.client_id), scopes, resource or self.resource_url
        )
        return OAuthToken(
            access_token=grant["access_token"],
            token_type="Bearer",
            expires_in=grant["expires_in"],
            scope=" ".join(scopes) or None,
            refresh_token=grant["refresh_token"],
        )

    # Pairing page

    async def pair(self, request: Request) -> Response:
        """Render the pairing form or approve, deny, or retry one authorization request."""

        if request.method == "GET":
            request_id = request.query_params.get("request", "")
            return self._page(request_id, self.store.get_auth_request(self.profile, request_id))
        form = await request.form()
        request_id = str(form.get("request", ""))
        pending = self.store.get_auth_request(self.profile, request_id)
        if pending is None:
            return self._page(request_id, None)
        params = pending["params"]
        if form.get("action") == "deny":
            self.store.delete_auth_request(self.profile, request_id)
            return self._redirect(params, error="access_denied")
        try:
            approved = self.store.consume_pairing_code(self.profile, str(form.get("code", "")))
        except ClitgError:
            self.store.delete_auth_request(self.profile, request_id)
            return self._message("Too many failed attempts. Try again later.", 429)
        if not approved:
            remaining = self.store.fail_auth_request(self.profile, request_id)
            if remaining == 0:
                return self._message("Too many failed attempts. Start the connection again.", 403)
            return self._page(
                request_id, pending, error=f"Invalid code. {remaining} attempts left."
            )
        self.store.delete_auth_request(self.profile, request_id)
        code, _ = self.store.save_auth_code(
            self.profile,
            pending["client_id"],
            {
                "scopes": params.get("scopes") or [SCOPE],
                "code_challenge": params["code_challenge"],
                "redirect_uri": params["redirect_uri"],
                "redirect_uri_provided_explicitly": params["redirect_uri_provided_explicitly"],
                "resource": params.get("resource"),
            },
        )
        return self._redirect(params, code=code)

    def _redirect(self, params: dict[str, Any], **values: str) -> Response:
        location = construct_redirect_uri(
            params["redirect_uri"], **values, state=params.get("state"), iss=self.issuer_url
        )
        return RedirectResponse(location, status_code=302, headers=_SECURITY_HEADERS)

    def _page(
        self, request_id: str, pending: dict[str, Any] | None, *, error: str | None = None
    ) -> Response:
        if pending is None:
            return self._message("This authorization request expired. Start again.", 400)
        client = self.store.get_client(self.profile, pending["client_id"]) or {}
        name = html.escape(str(client.get("client_name") or "Unnamed MCP client"))
        redirect_host = html.escape(urlparse(str(pending["params"]["redirect_uri"])).netloc)
        profile = html.escape(self.profile)
        notice = f'<p class="error">{html.escape(error)}</p>' if error else ""
        body = f"""
<h1>Connect to Telegram</h1>
<p><strong>{name}</strong> wants to use the clitg profile <strong>{profile}</strong>.
It will return to <strong>{redirect_host}</strong>.</p>
<p>Approve only if you started this connection yourself. Enter the pairing code shown by
<code>clitg mcp pair</code> or sent to your Saved Messages.</p>
{notice}
<form method="post" action="{PAIR_PATH}">
<input type="hidden" name="request" value="{html.escape(request_id)}">
<label for="code">Pairing code</label>
<input id="code" name="code" autocomplete="one-time-code" autocapitalize="characters"
 spellcheck="false" required autofocus>
<button name="action" value="approve" type="submit">Approve</button>
<button name="action" value="deny" type="submit" formnovalidate class="secondary">Deny</button>
</form>"""
        return self._html(body, 400 if error else 200)

    def _message(self, text: str, status: int) -> Response:
        return self._html(f"<h1>Connect to Telegram</h1><p>{html.escape(text)}</p>", status)

    @staticmethod
    def _html(body: str, status: int) -> Response:
        document = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>clitg pairing</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;
color:#1f2328;background:#fff;line-height:1.5}}
input,button{{font:inherit;padding:.6rem;margin:.4rem 0;width:100%;box-sizing:border-box}}
input#code{{letter-spacing:.2em;text-transform:uppercase}}
button{{background:#1f6feb;color:#fff;border:0;border-radius:.4rem;cursor:pointer}}
button.secondary{{background:#eaeef2;color:#1f2328}}
.error{{color:#cf222e}}
@media (prefers-color-scheme:dark){{body{{color:#e6edf3;background:#0d1117}}
button.secondary{{background:#30363d;color:#e6edf3}}}}
</style></head><body>{body}</body></html>"""
        return HTMLResponse(document, status_code=status, headers=_SECURITY_HEADERS)


class StaticTokenVerifier:
    """Verify static bearer tokens when OAuth cannot be offered on a plain HTTP address."""

    def __init__(self, store: McpStore, profile: str, resource_url: str) -> None:
        self.store = store
        self.profile = profile
        self.resource_url = resource_url

    async def verify_token(self, token: str) -> AccessToken | None:
        record = self.store.load_token(self.profile, token, ("static",))
        if record is None:
            return None
        return AccessToken(
            token=token,
            client_id=record["client_id"],
            scopes=record["scopes"],
            expires_at=record["expires_at"],
            resource=self.resource_url,
        )
