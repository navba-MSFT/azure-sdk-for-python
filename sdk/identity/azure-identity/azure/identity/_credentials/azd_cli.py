# ------------------------------------
# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.
# ------------------------------------

from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Dict, List, Optional

from azure.core.credentials import AccessToken
from azure.core.exceptions import ClientAuthenticationError

from .. import CredentialUnavailableError
from .._internal import resolve_tenant
from .._internal.decorators import log_get_token

CLI_NOT_FOUND = 'Azure Developer CLI could not be found. '\
                 'Please visit https://aka.ms/azure-dev for installation instructions and then,'\
                 'once installed, authenticate to your Azure account using \'azd auth login\'.'
COMMAND_LINE = "azd auth token --output json --scope {}"
EXECUTABLE_NAME = "azd"
NOT_LOGGED_IN = "Please run 'azd auth login' from a command prompt to authenticate before using this credential."


class AzureDeveloperCliCredential:
    """Authenticates by requesting a token from the Azure Developer CLI.

    Azure Developer CLI is a command-line interface tool that allows developers to create, manage, and deploy
    resources in Azure. It's built on top of the Azure CLI and provides additional functionality specific
    to Azure developers. It allows users to authenticate as a user and/or a service principal against
    `Azure Active Directory (Azure AD) <"https://learn.microsoft.com/azure/active-directory/fundamentals/">`__.
    The AzureDeveloperCliCredential authenticates in a development environment and acquires a token on behalf of
    the logged-in user or service principal in Azure Developer CLI. It acts as the Azure Developer CLI logged-in user
    or service principal and executes an Azure CLI command underneath to authenticate the application against
    Azure Active Directory.

    To use this credential, the developer needs to authenticate locally in Azure Developer CLI using one of the
    commands below:

      * Run "azd auth login" in Azure Developer CLI to authenticate interactively as a user.
      * Run "azd auth login --client-id 'client_id' --client-secret 'client_secret' --tenant-id 'tenant_id'"
        to authenticate as a service principal.

    You may need to repeat this process after a certain time period, depending on the refresh token validity in your
    organization. Generally, the refresh token validity period is a few weeks to a few months.
    AzureDeveloperCliCredential will prompt you to sign in again.

    :keyword str tenant_id: Optional tenant to include in the token request.
    :keyword List[str] additionally_allowed_tenants: Specifies tenants in addition to the specified "tenant_id"
        for which the credential may acquire tokens. Add the wildcard value "*" to allow the credential to
        acquire tokens for any tenant the application can access.
    :keyword int process_timeout: Seconds to wait for the Azure Developer CLI process to respond. Defaults
        to 10 seconds.

    .. admonition:: Example:

        .. literalinclude:: ../samples/credential_creation_code_snippets.py
            :start-after: [START azure_developer_cli_credential]
            :end-before: [END azure_developer_cli_credential]
            :language: python
            :dedent: 4
            :caption: Create an AzureDeveloperCliCredential.
    """

    def __init__(
        self,
        *,
        tenant_id: str = "",
        additionally_allowed_tenants: Optional[List[str]] = None,
        process_timeout: int = 10
    ) -> None:

        self.tenant_id = tenant_id
        self._additionally_allowed_tenants = additionally_allowed_tenants or []
        self._process_timeout = process_timeout

    def __enter__(self) -> "AzureDeveloperCliCredential":
        return self

    def __exit__(self, *args: Any) -> None:
        pass

    def close(self) -> None:
        """Calling this method is unnecessary."""

    @log_get_token("AzureDeveloperCliCredential")
    def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        """Request an access token for `scopes`.

        This method is called automatically by Azure SDK clients. Applications calling this method directly must
        also handle token caching because this credential doesn't cache the tokens it acquires.

        :param str scopes: desired scope for the access token. This credential allows only one scope per request.
            For more information about scopes, see
            https://learn.microsoft.com/azure/active-directory/develop/scopes-oidc.
        :keyword str tenant_id: optional tenant to include in the token request.

        :rtype: :class:`azure.core.credentials.AccessToken`

        :raises ~azure.identity.CredentialUnavailableError: the credential was unable to invoke
          the Azure Developer CLI.
        :raises ~azure.core.exceptions.ClientAuthenticationError: the credential invoked
          the Azure Developer CLI but didn't receive an access token.
        """

        if not scopes:
            raise ValueError("Missing scope in request. \n")

        commandString = " --scope ".join(scopes)
        command = COMMAND_LINE.format(commandString)
        tenant = resolve_tenant(
            default_tenant=self.tenant_id, additionally_allowed_tenants=self._additionally_allowed_tenants, **kwargs
        )
        if tenant:
            command += " --tenant-id " + tenant
        output = _run_command(command, self._process_timeout)

        token = parse_token(output)
        if not token:
            sanitized_output = sanitize_output(output)
            raise ClientAuthenticationError(
                message="Unexpected output from Azure CLI: '{}'. \n"
                        "To mitigate this issue, please refer to the troubleshooting guidelines here at "
                        "https://aka.ms/azsdk/python/identity/azdevclicredential/troubleshoot.".format(sanitized_output)
            )

        return token


def parse_token(output):
    """Parse to an AccessToken.

    In particular, convert the "expiresOn" value to epoch seconds. This value is a naive local datetime as returned by
    datetime.fromtimestamp.
    """
    try:
        token = json.loads(output)
        dt = datetime.strptime(token["expiresOn"], "%Y-%m-%dT%H:%M:%SZ")
        expires_on = dt.timestamp()

        return AccessToken(token["token"], int(expires_on))
    except (KeyError, ValueError):
        return None


def get_safe_working_dir():
    """Invoke 'azd' from a directory controlled by the OS, not the executing program's directory"""

    if sys.platform.startswith("win"):
        path = os.environ.get("SYSTEMROOT")
        if not path:
            raise CredentialUnavailableError(
                message="Azure Developer CLI credential" + " expects a 'SystemRoot' environment variable"
            )
        return path

    return "/bin"


def sanitize_output(output):
    """Redact tokens from CLI output to prevent error messages revealing them"""
    return re.sub(r"\"token\": \"(.*?)(\"|$)", "****", output)


def _run_command(command: str, timeout: int) -> str:
    # Ensure executable exists in PATH first. This avoids a subprocess call that would fail anyway.
    if shutil.which(EXECUTABLE_NAME) is None:
        raise CredentialUnavailableError(message=CLI_NOT_FOUND)

    if sys.platform.startswith("win"):
        args = ["cmd", "/c", command]
    else:
        args = ["/bin/sh", "-c", command]
    try:
        working_directory = get_safe_working_dir()

        kwargs: Dict[str, Any] = {
            "stderr": subprocess.PIPE,
            "cwd": working_directory,
            "universal_newlines": True,
            "env": dict(os.environ, NO_COLOR="true"),
            "timeout": timeout,
        }

        return subprocess.check_output(args, **kwargs)
    except subprocess.CalledProcessError as ex:
        # non-zero return from shell
        # Fallback check in case the executable is not found while executing subprocess.
        if ex.returncode == 127 or ex.stderr.startswith("'azd' is not recognized"):
            raise CredentialUnavailableError(message=CLI_NOT_FOUND)
        if "not logged in, run `azd auth login` to login" in ex.stderr:
            raise CredentialUnavailableError(message=NOT_LOGGED_IN)

        # return code is from the CLI -> propagate its output
        if ex.stderr:
            message = sanitize_output(ex.stderr)
        else:
            message = "Failed to invoke Azure Developer CLI"
        raise ClientAuthenticationError(message=message)
    except OSError as ex:
        # failed to execute 'cmd' or '/bin/sh'
        error = CredentialUnavailableError(message="Failed to execute '{}'".format(args[0]))
        raise error from ex
    except Exception as ex:  # pylint:disable=broad-except
        # could be a timeout, for example
        error = CredentialUnavailableError(message="Failed to invoke the Azure Developer CLI")
        raise error from ex
