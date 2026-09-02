import logging
import os
from typing import Optional
from urllib.parse import urljoin
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
import requests
from core.config import load_credentials

# Configure logging module
logger = logging.getLogger(__name__)

# Global singleton session instance
_session_instance: Optional["WorldQuantSession"] = None


class WorldQuantSession:
    """
    Manages authentication and persistent HTTP session state
    for interacting with the WorldQuant Brain API endpoints.
    """

    BASE_URL = "https://api.worldquantbrain.com"

    def __init__(
        self, username: Optional[str] = None, password: Optional[str] = None
    ):
        """
        Initializes the session instance. If credentials are not explicitly provided,
        they will be loaded from environment variables using load_credentials().
        """
        if not username or not password:
            username, password = load_credentials()

        self.username = username
        self.password = password
        self.session = requests.Session()
        self._authenticate()

    def _authenticate(self) -> None:
        """
        Authenticates against the WorldQuant Brain API endpoint.
        Handles both standard Basic Auth and Persona 2FA / Biometric verification flow.
        """
        logger.info("Authenticating with WorldQuant Brain API...")

        auth_endpoint = urljoin(self.BASE_URL, "/authentication")

        # Set basic authorization credentials on the session
        self.session.auth = (self.username, self.password)

        try:
            # Send initial authentication POST request
            response = self.session.post(auth_endpoint)

            # Case 1: Handle Persona / Biometrics 2FA Challenge (HTTP 401 with WWW-Authenticate header or inquiry body)
            if response.status_code == requests.codes.unauthorized:
                www_auth_header = response.headers.get("WWW-Authenticate", "")

                if "persona" in www_auth_header.lower() or "inquiry" in response.text:
                    relative_location = response.headers.get("Location", "")
                    
                    if not relative_location and "inquiry" in response.text:
                        try:
                            inquiry_code = response.json().get("inquiry")
                            if inquiry_code:
                                relative_location = f"/authentication/persona?inquiry={inquiry_code}"
                        except Exception:
                            pass

                    persona_url = urljoin(response.url, relative_location)

                    print("\n" + "=" * 70)
                    print("⚠️  PERSONA BIOMETRIC / 2FA AUTHENTICATION REQUIRED")
                    print("=" * 70)
                    print("Execution PAUSED. Please open the following URL in your browser to complete verification:\n")
                    print(f"👉 {persona_url}\n")
                    print("After the browser shows 'Success', return here and press ENTER to continue.")
                    print("=" * 70)

                    input("Press ENTER here after completing authentication in your browser...")

                    # Send follow-up POST request after browser verification is complete
                    response = self.session.post(persona_url)

                else:
                    raise Exception(
                        f"Authentication failed (HTTP 401): Invalid username or password. Details: {response.text}"
                    )

            # Case 2: Check for other non-successful status codes
            if response.status_code not in (
                requests.codes.ok,
                requests.codes.created,
                requests.codes.no_content,
            ):
                raise Exception(
                    f"Authentication failed with status code {response.status_code}: {response.text}"
                )

            logger.info("Successfully authenticated with WorldQuant Brain!")

        except Exception as e:
            logger.error(f"Authentication error: {e}")
            raise e

    def get(self, endpoint: str, **kwargs) -> requests.Response:
        """
        Helper method to execute authenticated GET requests.
        """
        url = urljoin(self.BASE_URL, endpoint)
        return self.session.get(url, **kwargs)

    def post(self, endpoint: str, **kwargs) -> requests.Response:
        """
        Helper method to execute authenticated POST requests.
        """
        url = urljoin(self.BASE_URL, endpoint)
        return self.session.post(url, **kwargs)


def get_session(
    username: Optional[str] = None, password: Optional[str] = None
) -> WorldQuantSession:
    """
    Returns a singleton WorldQuantSession instance. Reuses existing active session if available.
    """
    global _session_instance
    if _session_instance is None:
        _session_instance = WorldQuantSession(
            username=username, password=password
        )
    return _session_instance
