"""Shared GitHub auth for the remote CLIs (``pubstore-publish`` / ``pubstore-sweep``).

Both CLIs talk to the store repo over the GitHub API and must mint credentials
identically: App-installation auth from env (so the job needs no GitHub-Actions
tooling), with a plain token as the local/manual fallback. This module holds that
single resolution path so neither CLI duplicates it.
"""

from __future__ import annotations


def github_token(
    *,
    token: str | None = None,
    app_id: str | None = None,
    private_key: str | None = None,
    installation_id: str | None = None,
) -> str:
    """Resolve a raw bearer token from the same credentials as :func:`github_repo`.

    App credentials mint (and cache-refresh) an *installation* access token; otherwise
    the plain ``token`` is returned verbatim. The precedence and ``SystemExit`` failure
    modes match :func:`github_repo` exactly — this is its string-token twin, for the one
    caller (``pubstore-blacklist``) that also needs to ``git clone`` the store over HTTPS
    (the GitHub API alone can't reach deletion history).
    """
    if app_id and private_key and installation_id:
        from github import Auth, GithubIntegration

        # Mint the installation access token via GithubIntegration: reading `.token` off a
        # bare AppInstallationAuth raises ("withRequester must be called first") because
        # PyGithub only wires that up when the auth is handed to a Github client.
        integration = GithubIntegration(auth=Auth.AppAuth(int(app_id), private_key))
        return integration.get_access_token(int(installation_id)).token
    if token:
        return token
    raise SystemExit(
        "no credentials: set PUBBOT_APP_ID + PUBBOT_PRIVATE_KEY + "
        "PUBBOT_INSTALLATION_ID, or pass --token / set GITHUB_TOKEN"
    )


def github_repo(
    repo_name: str,
    *,
    token: str | None = None,
    app_id: str | None = None,
    private_key: str | None = None,
    installation_id: str | None = None,
):
    """Resolve a ``github.Repository.Repository`` for ``repo_name`` (``OWNER/REPO``).

    App credentials (all three of ``app_id`` / ``private_key`` / ``installation_id``)
    take precedence; otherwise a plain ``token``. Raises ``SystemExit`` with an
    actionable message if neither is fully supplied. ``github`` is imported lazily so
    callers (and their pure loops) import without PyGithub installed.
    """
    try:
        from github import Auth, Github
    except ModuleNotFoundError as exc:
        raise SystemExit(
            "this command needs PyGithub: install the extra with "
            "`pip install publication-store[publish]`"
        ) from exc

    if app_id and private_key and installation_id:
        auth = Auth.AppAuth(int(app_id), private_key).get_installation_auth(int(installation_id))
    elif token:
        auth = Auth.Token(token)
    else:
        raise SystemExit(
            "no credentials: set PUBBOT_APP_ID + PUBBOT_PRIVATE_KEY + "
            "PUBBOT_INSTALLATION_ID, or pass --token / set GITHUB_TOKEN"
        )
    return Github(auth=auth).get_repo(repo_name)
