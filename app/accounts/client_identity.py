from accounts.models import APIToken, CLIToken


def serialize_client_identity(token):
    user = token.user
    if isinstance(token, CLIToken):
        token_type = "cli"
        scopes = list(token.scopes or [])
    elif isinstance(token, APIToken):
        token_type = "sync"
        scopes = ["sync"]
    else:
        raise TypeError("Unsupported access token type.")

    return {
        "ok": True,
        "account": {
            "id": str(user.pk),
            "email": user.email,
            "display_name": user.display_name,
        },
        "token": {
            "type": token_type,
            "scopes": scopes,
        },
        # Workspace management is a reserved client-context boundary. The
        # current server still scopes documents directly to the token owner.
        "workspace": None,
    }
